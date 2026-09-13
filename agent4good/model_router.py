"""Durable, owner-selected routing. There is deliberately no automatic fallback."""

import json
import math
from .db import now, uid
from .model_config import ModelProfile, WORK_TYPES, resolve_profile, task_work
from .provider import ADAPTERS, ProviderError, check_cancelled


class ModelRouter:
    def __init__(self, settings, credentials, db):
        self.settings, self.credentials, self.db = settings, credentials, db

    def readiness(self, work_type):
        profile = resolve_profile(self.settings, work_type)
        configured = self.credentials.configured(ADAPTERS[profile.provider].credential_name)
        live = self.db.one(
            "SELECT 1 FROM model_calls c JOIN model_routes r ON c.task_id=r.task_id "
            "WHERE c.status='completed' AND json_extract(r.profile,'$.provider')=? "
            "AND json_extract(r.profile,'$.model')=? LIMIT 1",
            (profile.provider, profile.model),
        )
        return {
            "provider": profile.provider,
            "model": profile.model,
            "configured": configured,
            "available": configured and profile.tools,
            "verification": "live_call_recorded" if live else "not_live_verified",
            "capabilities": {
                "text": True,
                "tools": profile.tools,
                "structured_output": profile.structured_output,
                "vision": False,
                "streaming": False,
            },
            "reason": None
            if configured and profile.tools
            else (
                "Model profile does not support runtime tools" if configured else "Missing server credential"
            ),
        }

    def routes(self):
        return {work: self.readiness(work) for work in WORK_TYPES}

    def pin(self, task_id, items):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            self.db.require_owner(task)
            if task["status"] != "running":
                raise ProviderError("cancelled", "Task is no longer running")
            row = conn.execute("SELECT profile FROM model_routes WHERE task_id=?", (task_id,)).fetchone()
            if row:
                return ModelProfile.model_validate_json(row["profile"])
            profile = resolve_profile(self.settings, task_work(task))
            # Migrated executions have no reliable model identity. Preserve legacy OpenAI routing,
            # and refuse any configured rerouting until a new task is created.
            resumed = len(items) > 1 or any(
                i.get("type") in {"function_call", "function_call_output", "reasoning"} for i in items
            )
            if resumed:
                legacy = ModelProfile(
                    model=self.settings.model, max_output_tokens=self.settings.max_output_tokens
                )
                if profile != legacy:
                    raise ProviderError(
                        "route", "Legacy transcript has no pinned route; create a new task to change models"
                    )
            conn.execute("INSERT INTO model_routes VALUES (?,?)", (task_id, profile.model_dump_json()))
            conn.execute(
                "INSERT INTO events(task_id,kind,message,created_at) VALUES (?,?,?,?)",
                (
                    task_id,
                    "model_route",
                    f"{task_work(task)}: {profile.provider}/{profile.model}; pinned, no fallback",
                    now(),
                ),
            )
            return profile

    def reserve(self, task_id, profile, instructions, items, tools, schema):
        # Conservative byte-based reservation, not provider tokenization or a billing quote.
        # Includes protocol headroom; retain all reservations even after timeout/crash.
        input_reserve = (
            len(json.dumps([instructions, items, tools, schema], ensure_ascii=False).encode()) + 8192
        )
        tokens = input_reserve + profile.max_output_tokens
        cost = 0
        if self.settings.max_task_model_cost_usd is not None:
            if profile.input_usd_per_million is None or profile.output_usd_per_million is None:
                raise ProviderError(
                    "budget", "A model cost limit requires owner-configured input and output prices"
                )
            cost = math.ceil(
                input_reserve * profile.input_usd_per_million
                + profile.max_output_tokens * profile.output_usd_per_million
            )
        call_id = uid("model")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task["status"] != "running":
                raise ProviderError("cancelled", "Task is no longer running")
            used = conn.execute(
                "SELECT COALESCE(SUM(reserved_tokens),0),COALESCE(SUM(reserved_micro_usd),0) FROM model_calls WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if used[0] + tokens > self.settings.max_task_model_reserved_tokens:
                raise ProviderError("budget", "Task model token reservation limit reached")
            if self.settings.max_task_model_cost_usd is not None:
                # An old unpriced call could have incurred unknown cost. Fail closed.
                if conn.execute(
                    "SELECT 1 FROM model_calls WHERE task_id=? AND reserved_micro_usd=-1", (task_id,)
                ).fetchone():
                    raise ProviderError(
                        "budget", "Existing model costs are unknown; start a new budgeted task"
                    )
                if used[1] + cost > int(self.settings.max_task_model_cost_usd * 1000000):
                    raise ProviderError("budget", "Task estimated model cost limit reached")
            else:
                cost = -1
            conn.execute(
                "INSERT INTO model_calls VALUES (?,?,?,?,?,?,?)",
                (call_id, task_id, tokens, cost, "reserved", None, now()),
            )
        return call_id

    def respond_task(self, task_id, instructions, items, tools, *, schema=None):
        def cancelled():
            return self.db.task(task_id)["status"] != "running"

        check_cancelled(cancelled)
        profile = self.pin(task_id, items)
        adapter = ADAPTERS[profile.provider](self.settings, self.credentials, profile)
        adapter.validate(instructions, items, tools, schema)
        # Lease before reserving, so missing or revoked credentials never spend the budget.
        self.credentials.get(adapter.credential_name, "model", task_id)
        call_id = self.reserve(task_id, profile, instructions, items, tools, schema)
        try:
            result = adapter.respond(instructions, items, tools, schema=schema, cancelled=cancelled)
        except Exception:
            self.db.execute("UPDATE model_calls SET status='failed_or_unknown' WHERE id=?", (call_id,))
            raise
        self.db.execute(
            "UPDATE model_calls SET status='completed',usage=? WHERE id=?",
            (json.dumps(result["usage"]), call_id),
        )
        return result
