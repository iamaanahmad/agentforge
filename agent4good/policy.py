"""Owner-configured, deny-first authorization at the trusted invocation boundary."""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import now, uid

CLASSES = ("READ", "WRITE", "EXTERNAL_COMMUNICATION", "FINANCIAL", "DEPLOYMENT", "DESTRUCTIVE", "ADMIN")
Action = Literal["READ", "WRITE", "EXTERNAL_COMMUNICATION", "FINANCIAL", "DEPLOYMENT", "DESTRUCTIVE", "ADMIN"]


class PolicyError(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Limits(StrictModel):
    calls: int | None = Field(None, ge=0)
    spend_microusd: int | None = Field(None, ge=0)
    recipients: int | None = Field(None, ge=0)
    window_seconds: int = Field(86400, ge=1, le=31536000)


class Scope(StrictModel):
    user: str | None = Field(None, min_length=1, max_length=160)
    agent: str | None = Field(None, min_length=1, max_length=160)
    task: str | None = Field(None, min_length=1, max_length=160)
    tool: str | None = Field(None, min_length=1, max_length=160)
    environment: str | None = Field(None, min_length=1, max_length=160)
    action: Action | None = None


class Conditions(StrictModel):
    recipients: list[str] | None = Field(None, max_length=100)
    max_cost_microusd: int | None = Field(None, ge=0)
    argument_equals: dict[str, str] = Field(default_factory=dict)


class Rule(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    scope: Scope = Field(default_factory=Scope)
    effect: Literal["allow", "deny", "approval", "conditional_approval", "escalation"]
    conditions: Conditions | None = None
    limits: Limits | None = None

    @model_validator(mode="after")
    def conditional(self):
        if self.effect == "conditional_approval" and self.conditions is None:
            raise ValueError("Conditional approval requires conditions")
        return self


class PolicyDocument(StrictModel):
    rules: list[Rule] = Field(default_factory=list, max_length=100)
    limits: Limits = Field(default_factory=Limits)
    # Trusted upper-bound reservations, never model-supplied prices.
    tool_costs_microusd: dict[str, int] = Field(default_factory=dict)
    approval_ttl_seconds: int = Field(86400, ge=60, le=604800)

    @model_validator(mode="after")
    def unique(self):
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError("Rule IDs must be unique")
        if any(type(v) is not int or v < 0 for v in self.tool_costs_microusd.values()):
            raise ValueError("Costs must be nonnegative integers")
        return self


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass
class Decision:
    effect: str
    version: str
    scope: dict
    rules: list
    document: PolicyDocument
    cost: int
    recipients: int


class PolicyEngine:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def current(self, conn):
        row = conn.execute("SELECT revision,document FROM action_policy WHERE id=1").fetchone()
        return row["revision"], PolicyDocument.model_validate_json(row["document"])

    def replace(self, document, expected_revision):
        document = PolicyDocument.model_validate(document)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision, _ = self.current(conn)
            if revision != expected_revision:
                raise PolicyError("Policy changed; read it again before saving")
            conn.execute(
                "UPDATE action_policy SET revision=?,document=? WHERE id=1",
                (revision + 1, canonical(document.model_dump())),
            )
            conn.execute(
                "INSERT INTO events(kind,message,created_at) VALUES (?,?,?)",
                ("policy_updated", f"Owner installed policy revision {revision + 1}", now()),
            )
        return revision + 1

    def evaluate(self, conn, task, spec, args):
        from .coordination import enforce, ancestors

        try:
            enforce(conn, task, spec.name)
        except ValueError as exc:
            raise PolicyError(str(exc)) from None
        decision = self._evaluate_one(conn, task, spec, args)
        priority = {"allow": 0, "approval": 1, "conditional_approval": 1, "escalation": 2, "deny": 3}
        for parent in ancestors(conn, task["id"]):
            inherited = self._evaluate_one(conn, parent, spec, args)
            if priority[inherited.effect] > priority[decision.effect]:
                decision.effect = inherited.effect
            known = {r.id for r in decision.rules}
            decision.rules.extend(r for r in inherited.rules if r.id not in known)
        return decision

    def _evaluate_one(self, conn, task, spec, args):
        revision, document = self.current(conn)
        autonomy = conn.execute("SELECT value FROM settings WHERE key='autonomy'").fetchone()[0]
        scope = dict(
            user=task["owner_id"],
            agent=task["agent"],
            task=task["id"],
            tool=spec.name,
            environment=self.settings.environment,
            action=spec.action_class,
        )
        version = hashlib.sha256(
            canonical(
                dict(
                    revision=revision,
                    document=document.model_dump(),
                    autonomy=autonomy,
                    environment=self.settings.environment,
                    origin=self.settings.public_origin,
                    repository=self.settings.github_repo,
                    sender=self.settings.mail_from,
                    action=spec.action_class,
                    permissions=spec.permissions,
                )
            ).encode()
        ).hexdigest()
        if (
            spec.action_class not in CLASSES
            or task["owner_id"] != "owner"
            or autonomy not in {"manual", "supervised", "autonomous"}
        ):
            return Decision("deny", version, scope, [], document, 0, 0)
        # Existing exact-owner requirements are a floor, even under explicit allow.
        effect = (
            "approval"
            if autonomy == "manual"
            or spec.permissions == "exact_owner_approval"
            or spec.action_class in CLASSES[2:]
            else "allow"
        )
        if spec.action_class == "FINANCIAL" and spec.name not in document.tool_costs_microusd:
            effect = "deny"
        cost = document.tool_costs_microusd.get(spec.name, 0)
        recipient = args.get("to") if spec.action_class == "EXTERNAL_COMMUNICATION" else None
        if spec.action_class == "EXTERNAL_COMMUNICATION" and spec.name in {
            "github_create_issue",
            "github_open_pr",
        }:
            recipient = "github:" + self.settings.github_repo
        if spec.action_class == "EXTERNAL_COMMUNICATION" and not recipient:
            effect = "deny"
        rules = []
        priority = {"allow": 0, "approval": 1, "conditional_approval": 1, "escalation": 2, "deny": 3}
        for rule in document.rules:
            if any(scope[k] != v for k, v in rule.scope.model_dump(exclude_none=True).items()):
                continue
            rules.append(rule)
            candidate = rule.effect
            if rule.conditions:
                c = rule.conditions
                passed = (
                    (c.recipients is None or recipient in c.recipients)
                    and (c.max_cost_microusd is None or cost <= c.max_cost_microusd)
                    and all(args.get(k) == v for k, v in c.argument_equals.items())
                )
                if not passed:
                    candidate = "deny"
            if priority[candidate] > priority[effect]:
                effect = candidate
        return Decision(effect, version, scope, rules, document, cost, int(recipient is not None))

    def _signature(self, approval_id, fingerprint, expires):
        return hmac.new(
            self.settings.session_secret.encode(),
            canonical([approval_id, fingerprint, expires]).encode(),
            hashlib.sha256,
        ).hexdigest()

    def _fingerprint(self, decision, tool, args, task_id, call_id):
        return hashlib.sha256(
            canonical([decision.version, decision.scope, tool, args, task_id, call_id]).encode()
        ).hexdigest()

    def bind(self, conn, approval, decision):
        fingerprint = self._fingerprint(
            decision,
            approval["tool"],
            json.loads(approval["arguments"]),
            approval["task_id"],
            approval["call_id"],
        )
        expires = time.time() + decision.document.approval_ttl_seconds
        conn.execute(
            "INSERT INTO policy_approvals VALUES (?,?,?,?,?)",
            (
                approval["id"],
                fingerprint,
                expires,
                self._signature(approval["id"], fingerprint, expires),
                decision.effect,
            ),
        )

    def check(self, conn, decision, task_id, call_id, name, args):
        if decision.effect == "deny":
            raise PolicyError("Action denied by policy")
        if decision.effect == "allow":
            return
        approval = conn.execute(
            "SELECT * FROM approvals WHERE task_id=? AND call_id=?", (task_id, call_id)
        ).fetchone()
        if (
            not approval
            or approval["status"] != "approved"
            or approval["tool"] != name
            or json.loads(approval["arguments"]) != args
        ):
            raise PolicyError("Approval does not match this exact action")
        binding = conn.execute(
            "SELECT * FROM policy_approvals WHERE approval_id=?", (approval["id"],)
        ).fetchone()
        if not binding:
            raise PolicyError("Approval has no policy binding; request a fresh decision")
        expected = self._fingerprint(decision, name, args, task_id, call_id)
        if (
            binding["fingerprint"] != expected
            or binding["expires"] <= time.time()
            or not hmac.compare_digest(
                binding["signature"], self._signature(approval["id"], expected, binding["expires"])
            )
        ):
            raise PolicyError("Approval scope, expiry or policy version changed")

    def migrate_legacy(self, specs):
        # Only IDs captured by the schema migration may receive legacy bindings.
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision, _ = self.current(conn)
            for row in conn.execute(
                "SELECT a.* FROM approvals a JOIN policy_legacy l ON l.approval_id=a.id"
            ).fetchall():
                if revision == 1 and row["tool"] in specs:
                    task = conn.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
                    decision = self.evaluate(conn, task, specs[row["tool"]], json.loads(row["arguments"]))
                    self.bind(conn, row, decision)
                conn.execute("DELETE FROM policy_legacy WHERE approval_id=?", (row["id"],))

    def prepare(self, task_id, call_id, spec, args):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["status"] != "running":
                raise PolicyError("Policy requires an active task")
            decision = self.evaluate(conn, task, spec, args)
            conn.execute(
                "INSERT INTO events(task_id,kind,message,created_at) VALUES (?,?,?,?)",
                (
                    task_id,
                    "policy_decision",
                    canonical({"effect": decision.effect, "version": decision.version}),
                    now(),
                ),
            )
            if decision.effect == "deny":
                return "deny"
            if decision.effect == "allow":
                return "allow"
            approval = conn.execute(
                "SELECT * FROM approvals WHERE task_id=? AND call_id=?", (task_id, call_id)
            ).fetchone()
            if approval:
                self.check(conn, decision, task_id, call_id, spec.name, args)
                return "allow"
            approval_id = uid("approval")
            conn.execute(
                "INSERT INTO approvals(id,task_id,call_id,tool,arguments,created_at) VALUES (?,?,?,?,?,?)",
                (approval_id, task_id, call_id, spec.name, canonical(args), now()),
            )
            approval = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            self.bind(conn, approval, decision)
            conn.execute(
                "UPDATE tasks SET status='waiting_approval',updated_at=? WHERE id=?", (now(), task_id)
            )
            return decision.effect

    def reserve(self, conn, decision):
        limits = [("global", decision.document.limits)] + [
            ("rule:" + r.id, r.limits) for r in decision.rules if r.limits
        ]
        ts = time.time()
        for key, limit in limits:
            row = conn.execute(
                "SELECT COALESCE(SUM(calls),0) calls, COALESCE(SUM(cost),0) cost, "
                "COALESCE(SUM(recipients),0) recipients FROM policy_usage WHERE bucket=? AND created_at>?",
                (key, ts - limit.window_seconds),
            ).fetchone()
            if (
                (limit.calls is not None and row["calls"] + 1 > limit.calls)
                or (limit.spend_microusd is not None and row["cost"] + decision.cost > limit.spend_microusd)
                or (
                    limit.recipients is not None
                    and row["recipients"] + decision.recipients > limit.recipients
                )
            ):
                raise PolicyError("Policy budget or rate limit reached")
            conn.execute(
                "INSERT INTO policy_usage(bucket,created_at,calls,cost,recipients) VALUES (?,?,1,?,?)",
                (key, ts, decision.cost, decision.recipients),
            )
