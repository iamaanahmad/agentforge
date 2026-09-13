"""Bounded persistent triggers. Dispatch and occurrence receipts share one transaction."""

import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .catalog import AGENTS
from .db import now, uid

TABLES = ["schedule_definitions", "schedule_events", "schedule_occurrences"]


def initialize(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schedule_definitions (schedule_id TEXT PRIMARY KEY REFERENCES schedules(id), definition TEXT NOT NULL, creator TEXT REFERENCES tasks(id), runs INTEGER NOT NULL DEFAULT 0, condition_true INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schedule_events (schedule_id TEXT NOT NULL REFERENCES schedules(id), event_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(schedule_id,event_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schedule_occurrences (schedule_id TEXT NOT NULL REFERENCES schedules(id), occurrence TEXT NOT NULL, task_id TEXT REFERENCES tasks(id), status TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(schedule_id,occurrence))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS schedule_task ON schedule_occurrences(task_id)")
    for row in conn.execute(
        "SELECT s.* FROM schedules s LEFT JOIN schedule_definitions d ON d.schedule_id=s.id WHERE d.schedule_id IS NULL"
    ).fetchall():
        legacy(conn, row)


def instant(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Supply an explicit UTC offset")
    return dt.astimezone(timezone.utc)


class ScheduleError(ValueError):
    pass


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=100)
    status: Literal["done", "failed", "cancelled"] = "done"


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=20000)
    agent: str = "strategist"
    mode: Literal["interval", "once", "recurring", "deadline", "event", "condition"] = "interval"
    interval_minutes: int = Field(1440, ge=15, le=525600)
    at: str | None = None
    timezone: str = "UTC"
    local_time: str = Field("09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    priority: int = Field(0, ge=-10, le=10)
    deadline: str | None = None
    missed: Literal["coalesce", "skip"] = "coalesce"
    grace_seconds: int = Field(60, ge=1, le=86400)
    overlap: Literal["allow", "forbid"] = "forbid"
    max_runs: int = Field(100, ge=1, le=1000)
    condition: Condition | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.agent not in {a["id"] for a in AGENTS}:
            raise ValueError("Unknown agent")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone") from None
        if self.mode in {"once", "deadline"} and not self.at:
            raise ValueError("This trigger requires at")
        for field in ("at", "deadline"):
            if getattr(self, field):
                setattr(self, field, instant(getattr(self, field)).isoformat())
        if (self.mode == "condition") != (self.condition is not None):
            raise ValueError("Only condition triggers require a condition")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("Duplicate dependencies")
        return self


def calendar_next(spec, after):
    zone = ZoneInfo(spec.timezone)
    date = after.astimezone(zone).date()
    hour, minute = map(int, spec.local_time.split(":"))
    for offset in range(370):
        day = date + timedelta(days=offset)
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
        utc = local.astimezone(timezone.utc)
        # Skip nonexistent spring-forward times; choose only first fall-back fold.
        if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if utc > after:
            return utc.isoformat()
    raise ScheduleError("No calendar occurrence found")


def create(conn, spec, creator=None):
    ts = datetime.now(timezone.utc)
    if conn.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1").fetchone()[0] >= 100:
        raise ScheduleError("Active schedule limit reached")
    refs = spec.depends_on + ([spec.condition.task_id] if spec.condition else [])
    for tid in refs:
        if not conn.execute("SELECT 1 FROM tasks WHERE id=? AND owner_id='owner'", (tid,)).fetchone():
            raise ScheduleError("Dependency is outside the owner workspace")
    if creator:
        from .missions import mission_for
        from .coordination import ancestors

        if mission_for(conn, creator) or ancestors(conn, creator) or origins(conn, creator):
            raise ScheduleError("Only independent root tasks may schedule future work")
        if (
            conn.execute("SELECT COUNT(*) FROM schedule_definitions WHERE creator=?", (creator,)).fetchone()[
                0
            ]
            >= 10
        ):
            raise ScheduleError("Creator schedule limit reached")
        if spec.max_runs > 100:
            raise ScheduleError("Agent schedules allow at most 100 runs")
    next_at = spec.at or (ts + timedelta(minutes=spec.interval_minutes)).isoformat()
    if spec.mode == "recurring":
        next_at = calendar_next(spec, ts)
    if spec.mode in {"event", "condition"}:
        next_at = ts.isoformat()
    sid = uid("schedule")
    conn.execute(
        "INSERT INTO schedules VALUES (?,?,?,?,?,1,?,?)",
        (sid, spec.name, spec.prompt, spec.agent, spec.interval_minutes, next_at, now()),
    )
    conn.execute(
        "INSERT INTO schedule_definitions(schedule_id,definition,creator) VALUES (?,?,?)",
        (sid, spec.model_dump_json(), creator),
    )
    return sid


def origins(conn, task_id):
    row = conn.execute(
        "SELECT t.* FROM tasks t JOIN schedule_definitions d ON d.creator=t.id JOIN schedule_occurrences o ON o.schedule_id=d.schedule_id WHERE o.task_id=?",
        (task_id,),
    ).fetchone()
    if row:
        return [row]
    node = conn.execute("SELECT root_id FROM worker_nodes WHERE task_id=?", (task_id,)).fetchone()
    if node and node["root_id"] != task_id:
        return origins(conn, node["root_id"])
    return []


def guard(conn, task_id):
    row = conn.execute(
        "SELECT d.cancelled FROM schedule_definitions d JOIN schedule_occurrences o ON o.schedule_id=d.schedule_id WHERE o.task_id=?",
        (task_id,),
    ).fetchone()
    if row and row["cancelled"]:
        raise ScheduleError("Schedule cancelled")
    for source in origins(conn, task_id):
        if source["status"] in {"failed", "cancelled"} or source["owner_id"] != "owner":
            raise ScheduleError("Schedule creator is closed or inaccessible")


def control(conn, sid, enabled=None, cancel=False, creator=None):
    row = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    definition = conn.execute("SELECT * FROM schedule_definitions WHERE schedule_id=?", (sid,)).fetchone()
    if not row or (creator and (not definition or definition["creator"] != creator)):
        raise ScheduleError("Schedule not found in this scope")
    if definition and definition["cancelled"]:
        raise ScheduleError("Schedule is cancelled; create a new schedule")
    if not cancel and enabled and not row["enabled"]:
        if conn.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1").fetchone()[0] >= 100:
            raise ScheduleError("Active schedule limit reached")
    if cancel and not definition:
        legacy(conn, row)
    conn.execute("UPDATE schedules SET enabled=? WHERE id=?", (0 if cancel else int(enabled), sid))
    if cancel:
        conn.execute("UPDATE schedule_definitions SET cancelled=1 WHERE schedule_id=?", (sid,))
        from .coordination import settle

        for task in conn.execute(
            "SELECT task_id FROM schedule_occurrences WHERE schedule_id=? AND task_id IS NOT NULL", (sid,)
        ).fetchall():
            conn.execute(
                "UPDATE tasks SET status='cancelled',updated_at=? WHERE id=? AND status NOT IN ('done','failed','cancelled')",
                (now(), task["task_id"]),
            )
            conn.execute(
                "UPDATE approvals SET status='rejected',decided_at=? WHERE task_id=? AND status='pending'",
                (now(), task["task_id"]),
            )
        settle(conn)


def event(conn, sid, event_id):
    row = conn.execute(
        "SELECT d.* FROM schedule_definitions d JOIN schedules s ON s.id=d.schedule_id WHERE s.id=? AND s.enabled=1",
        (sid,),
    ).fetchone()
    if not row or row["cancelled"] or json.loads(row["definition"])["mode"] != "event":
        raise ScheduleError("Active event schedule not found")
    old = conn.execute(
        "SELECT 1 FROM schedule_events WHERE schedule_id=? AND event_id=?", (sid, event_id)
    ).fetchone()
    if old:
        return False
    if conn.execute("SELECT COUNT(*) FROM schedule_events WHERE schedule_id=?", (sid,)).fetchone()[0] >= 1000:
        raise ScheduleError("Event budget reached")
    conn.execute("INSERT INTO schedule_events VALUES (?,?,?)", (sid, event_id, now()))
    return True


def legacy(conn, row):
    spec = ScheduleInput(
        name=row["name"],
        prompt=row["prompt"],
        agent=row["agent"],
        interval_minutes=row["interval_minutes"],
        overlap="allow",
    )
    conn.execute(
        "INSERT OR IGNORE INTO schedule_definitions(schedule_id,definition) VALUES (?,?)",
        (row["id"], json.dumps({**spec.model_dump(), "_legacy": True})),
    )
    return spec


def dispatch(db, settings, registry, current=None):
    ts = current or datetime.now(timezone.utc)
    count = 0
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        autonomous = (
            conn.execute("SELECT value FROM settings WHERE key='autonomy'").fetchone()[0] == "autonomous"
        )
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at,id LIMIT 100",
            (ts.isoformat(),),
        ).fetchall()
        for row in rows:
            definition = conn.execute(
                "SELECT * FROM schedule_definitions WHERE schedule_id=?", (row["id"],)
            ).fetchone()
            data = json.loads(definition["definition"]) if definition else {}
            spec = (
                ScheduleInput.model_validate({k: v for k, v in data.items() if k != "_legacy"})
                if definition
                else legacy(conn, row)
            )
            if definition and (
                definition["cancelled"] or (not data.get("_legacy") and definition["runs"] >= spec.max_runs)
            ):
                conn.execute("UPDATE schedules SET enabled=0 WHERE id=?", (row["id"],))
                continue
            creator = definition["creator"] if definition else None
            due = instant(row["next_run_at"])
            occurrence = row["next_run_at"]
            expired = bool(spec.deadline and ts > instant(spec.deadline))
            if spec.mode == "event" and not expired:
                ev = conn.execute(
                    "SELECT e.* FROM schedule_events e WHERE e.schedule_id=? AND NOT EXISTS (SELECT 1 FROM schedule_occurrences o WHERE o.schedule_id=e.schedule_id AND o.occurrence=e.event_id) ORDER BY e.created_at,e.event_id LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if not ev:
                    continue
                occurrence, due = ev["event_id"], instant(ev["created_at"])
            if spec.mode == "condition" and not expired:
                conn.execute(
                    "UPDATE schedules SET next_run_at=? WHERE id=?",
                    ((ts + timedelta(minutes=spec.interval_minutes)).isoformat(), row["id"]),
                )
                target = conn.execute(
                    "SELECT status FROM tasks WHERE id=?", (spec.condition.task_id,)
                ).fetchone()
                truth = bool(target and target["status"] == spec.condition.status)
                if not truth:
                    conn.execute(
                        "UPDATE schedule_definitions SET condition_true=0 WHERE schedule_id=?", (row["id"],)
                    )
                    continue
                if definition and definition["condition_true"]:
                    continue
            deps = [
                conn.execute("SELECT status FROM tasks WHERE id=?", (t,)).fetchone() for t in spec.depends_on
            ]
            expired = spec.deadline and ts > instant(spec.deadline)
            failed = any(not d or d["status"] in {"failed", "cancelled"} for d in deps)
            missed = spec.missed == "skip" and (ts - due).total_seconds() > spec.grace_seconds
            outcome = (
                "expired" if expired else "dependency_failed" if failed else "missed" if missed else None
            )
            if not outcome and any(d["status"] != "done" for d in deps):
                continue
            if creator:
                source = conn.execute("SELECT * FROM tasks WHERE id=?", (creator,)).fetchone()
                if not source or source["status"] in {"failed", "cancelled"}:
                    outcome = "creator_closed"
                else:
                    from .tools import SPECS
                    from .policy import PolicyError

                    try:
                        decision = registry.policy.evaluate(
                            conn, source, SPECS["schedule_create"], {"request": spec.model_dump_json()}
                        )
                        if decision.effect != "allow" or not any(
                            r.effect == "allow" and r.scope.tool == "schedule_create" for r in decision.rules
                        ):
                            outcome = "policy_denied"
                    except PolicyError:
                        outcome = "policy_denied"
            if (
                not outcome
                and spec.overlap == "forbid"
                and conn.execute(
                    "SELECT 1 FROM schedule_occurrences o JOIN tasks t ON t.id=o.task_id WHERE o.schedule_id=? AND t.status NOT IN ('done','failed','cancelled')",
                    (row["id"],),
                ).fetchone()
            ):
                continue
            if (
                not outcome
                and conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('draft','queued')").fetchone()[
                    0
                ]
                >= settings.max_queued_tasks
            ):
                continue
            exists = conn.execute(
                "SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND occurrence=?",
                (row["id"], occurrence),
            ).fetchone()
            if not exists:
                task_id = None
                if not outcome and creator:
                    conn.execute("SAVEPOINT schedule_budget")
                    try:
                        registry.policy.reserve(conn, decision)
                    except PolicyError:
                        conn.execute("ROLLBACK TO SAVEPOINT schedule_budget")
                        outcome = "policy_denied"
                    finally:
                        conn.execute("RELEASE SAVEPOINT schedule_budget")
                if not outcome:
                    task_id = db.create_task(spec.name, spec.prompt, spec.agent, autonomous, conn)
                    from .coordination import node

                    node(conn, task_id)
                    conn.execute(
                        "UPDATE worker_nodes SET priority=? WHERE task_id=?", (spec.priority, task_id)
                    )
                    if creator:
                        allowed = node(conn, creator)["tools"]
                        conn.execute("UPDATE worker_nodes SET tools=? WHERE task_id=?", (allowed, task_id))
                    count += 1
                conn.execute(
                    "INSERT INTO schedule_occurrences VALUES (?,?,?,?,?)",
                    (row["id"], occurrence, task_id, outcome or "dispatched", ts.isoformat()),
                )
                conn.execute("UPDATE schedule_definitions SET runs=runs+1 WHERE schedule_id=?", (row["id"],))
                conn.execute(
                    "INSERT INTO events(task_id,kind,message,created_at) VALUES (?,?,?,?)",
                    (
                        task_id,
                        "schedule_occurrence",
                        json.dumps(
                            {
                                "schedule_id": row["id"],
                                "occurrence": occurrence,
                                "status": outcome or "dispatched",
                            }
                        ),
                        ts.isoformat(),
                    ),
                )
            if (
                spec.mode in {"once", "deadline"}
                or expired
                or failed
                or outcome in {"creator_closed", "policy_denied"}
            ):
                conn.execute("UPDATE schedules SET enabled=0 WHERE id=?", (row["id"],))
            elif spec.mode == "condition":
                conn.execute(
                    "UPDATE schedule_definitions SET condition_true=1 WHERE schedule_id=?", (row["id"],)
                )
            elif spec.mode == "recurring":
                conn.execute(
                    "UPDATE schedules SET next_run_at=? WHERE id=?", (calendar_next(spec, ts), row["id"])
                )
            elif spec.mode == "interval":
                conn.execute(
                    "UPDATE schedules SET next_run_at=? WHERE id=?",
                    ((ts + timedelta(minutes=spec.interval_minutes)).isoformat(), row["id"]),
                )
    return count


def tool_dispatch(conn, db, settings, task_id, name, args):
    if name == "schedule_cancel":
        control(conn, args["schedule_id"], cancel=True, creator=task_id)
        return {"data": {"cancelled": args["schedule_id"]}}
    spec = ScheduleInput.model_validate_json(args["request"])
    from .policy import PolicyEngine
    from .tools import SPECS

    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    decision = PolicyEngine(db, settings).evaluate(conn, task, SPECS[name], args)
    if decision.effect != "allow" or not any(
        r.effect == "allow" and r.scope.tool == name for r in decision.rules
    ):
        raise ScheduleError("Agent scheduling requires an explicit schedule_create allow scope")
    return {"data": {"schedule_id": create(conn, spec, creator=task_id)}}


def settle(conn):
    rows = conn.execute(
        "SELECT s.id FROM schedules s JOIN schedule_definitions d ON d.schedule_id=s.id JOIN tasks t ON t.id=d.creator WHERE d.cancelled=0 AND t.status IN ('failed','cancelled')"
    ).fetchall()
    for row in rows:
        conn.execute("UPDATE schedules SET enabled=0 WHERE id=?", (row["id"],))
        conn.execute("UPDATE schedule_definitions SET cancelled=1 WHERE schedule_id=?", (row["id"],))
        conn.execute(
            "UPDATE tasks SET status='cancelled',updated_at=? WHERE id IN (SELECT task_id FROM schedule_occurrences WHERE schedule_id=?) AND status NOT IN ('done','failed','cancelled')",
            (now(), row["id"]),
        )
        conn.execute(
            "UPDATE approvals SET status='rejected',decided_at=? WHERE status='pending' AND task_id IN (SELECT task_id FROM schedule_occurrences WHERE schedule_id=?)",
            (now(), row["id"]),
        )
