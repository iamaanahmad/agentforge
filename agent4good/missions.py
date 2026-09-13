"""Bounded mission DAGs, inherited authority and deterministic evidence checks.

All writes share the database control transaction. Objective and criteria never
change during replanning. Completed tasks and receipts are never replayed.
"""

import json
import math
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .catalog import AGENTS
from .db import now, uid

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS missions (id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, verification TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS mission_nodes (task_id TEXT PRIMARY KEY REFERENCES tasks(id), mission_id TEXT NOT NULL REFERENCES missions(id), node_key TEXT NOT NULL, dependencies TEXT NOT NULL, planner INTEGER NOT NULL DEFAULT 0, UNIQUE(mission_id,node_key))",
    "CREATE TABLE IF NOT EXISTS mission_plans (mission_id TEXT NOT NULL REFERENCES missions(id), revision INTEGER NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(mission_id,revision))",
    "CREATE TABLE IF NOT EXISTS mission_reviews (mission_id TEXT NOT NULL REFERENCES missions(id), criterion_id TEXT NOT NULL, evidence TEXT NOT NULL, accepted INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(mission_id,criterion_id))",
]
TABLES = ["missions", "mission_nodes", "mission_plans", "mission_reviews"]
CLOSED = {"done", "cancelled", "expired"}
TASK_CLOSED = {"done", "failed", "cancelled"}
MEMBERS = (
    "SELECT n.task_id FROM worker_nodes n JOIN mission_nodes m ON m.task_id=n.root_id WHERE m.mission_id=?"
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)


class Criterion(Input):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    description: str = Field(min_length=1, max_length=1000)
    kind: Literal["artifact", "receipt", "metric", "owner"] = "owner"
    name: str = Field(default="", max_length=160)
    contains: str = Field(default="", max_length=2000)
    arguments: dict[str, str] = Field(default_factory=dict, max_length=20)
    field: str = Field(default="", max_length=160)
    target: float | int | None = None
    unit: str = Field(default="", max_length=60)

    @model_validator(mode="after")
    def valid(self):
        from .tools import SPECS

        if self.kind == "artifact" and (not self.name.strip() or not self.contains.strip()):
            raise ValueError("Artifact checks need an exact filename and required text")
        if self.kind in {"receipt", "metric"} and self.name not in SPECS:
            raise ValueError("Receipt checks need an implemented tool")
        if self.kind == "metric" and (
            not self.field or self.target is None or not math.isfinite(self.target) or not self.unit
        ):
            raise ValueError("Metric checks need a result field, finite target and unit")
        return self


class Budget(Input):
    tasks: int = Field(default=16, ge=2, le=100)
    steps: int = Field(default=60, ge=1, le=1000)
    tool_calls: int = Field(default=60, ge=1, le=1000)
    model_tokens: int = Field(default=1000000, ge=1000, le=10000000)
    model_cost_usd: float | int | None = Field(default=None, ge=0, le=1000, allow_inf_nan=False)


class MissionInput(Input):
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=10000)
    criteria: list[Criterion] = Field(min_length=1, max_length=20)
    constraints: str = Field(default="", max_length=4000)
    budget: Budget = Field(default_factory=Budget)
    deadline: str
    tools: list[str] = Field(
        default_factory=lambda: ["artifact_write", "memory_read", "memory_search"], max_length=50
    )
    agents: list[str] = Field(
        default_factory=lambda: ["strategist", "research_analyst"], min_length=1, max_length=9
    )
    writes: bool = False
    priority: int = Field(default=0, ge=-10, le=10)

    @model_validator(mode="after")
    def valid(self):
        from .tools import SPECS

        deadline = datetime.fromisoformat(self.deadline)
        if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
            raise ValueError("Use a future deadline with a timezone")
        self.deadline = deadline.astimezone(timezone.utc).isoformat()
        if not self.title.strip() or not self.objective.strip():
            raise ValueError("Title and objective cannot be blank")
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("Criterion IDs must be unique")
        if not set(self.tools) <= set(SPECS) - {"mission_plan", "mission_status"}:
            raise ValueError("Unknown or reserved mission tools")
        if not set(self.agents) <= {a["id"] for a in AGENTS}:
            raise ValueError("Unknown mission agent")
        return self


class Step(Input):
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$")
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=10000)
    agent: str
    tools: list[str] = Field(max_length=50)
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    priority: int = Field(default=0, ge=-10, le=10)


class Plan(Input):
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)
    steps: list[Step] = Field(min_length=1, max_length=100)
    retire: list[str] = Field(default_factory=list, max_length=100)


def initialize(conn):
    for sql in SCHEMA:
        conn.execute(sql)


def mission_for(conn, task_id):
    return conn.execute(
        "SELECT m.*,n.planner,n.node_key FROM missions m JOIN mission_nodes n ON m.id=n.mission_id JOIN worker_nodes w ON w.root_id=n.task_id WHERE w.task_id=?",
        (task_id,),
    ).fetchone()


def usage(conn, mission_id):
    tasks = conn.execute(
        f"SELECT COUNT(*),COALESCE(SUM(steps),0) FROM tasks WHERE id IN ({MEMBERS})", (mission_id,)
    ).fetchone()
    calls = conn.execute(
        f"SELECT COALESCE(SUM(attempts),0) FROM tool_runs WHERE task_id IN ({MEMBERS})", (mission_id,)
    ).fetchone()[0]
    model = conn.execute(
        f"SELECT COALESCE(SUM(reserved_tokens),0),COALESCE(SUM(CASE WHEN reserved_micro_usd>0 THEN reserved_micro_usd ELSE 0 END),0) FROM model_calls WHERE task_id IN ({MEMBERS})",
        (mission_id,),
    ).fetchone()
    return {
        "tasks": tasks[0],
        "steps": tasks[1],
        "tool_calls": calls,
        "model_tokens": model[0],
        "model_cost_usd": model[1] / 1000000,
    }


def guard(conn, task_id, tool=None):
    m = mission_for(conn, task_id)
    if not m:
        return None
    spec = json.loads(m["spec"])
    if m["status"] != "running" or now() >= spec["deadline"]:
        raise ValueError("Mission is paused, closed, or past its deadline")
    task = conn.execute("SELECT agent FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task["agent"] not in spec["agents"]:
        raise ValueError("Agent outside mission permissions")
    member = conn.execute(
        "SELECT dependencies FROM mission_nodes WHERE task_id=(SELECT root_id FROM worker_nodes WHERE task_id=?)",
        (task_id,),
    ).fetchone()
    if member:
        for dependency in json.loads(member["dependencies"]):
            dep = conn.execute(
                "SELECT t.status FROM tasks t JOIN mission_nodes n ON n.task_id=t.id WHERE n.mission_id=? AND n.node_key=?",
                (m["id"], dependency),
            ).fetchone()
            if not dep or dep[0] != "done":
                raise ValueError("Mission dependencies are not complete")
    if tool:
        from .tools import MUTATING

        if tool in {"mission_plan", "mission_status"}:
            if (
                not m["planner"]
                or conn.execute(
                    "SELECT 1 FROM mission_nodes WHERE task_id=? AND planner=1", (task_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("Only the mission planner can change the plan")
        elif tool not in spec["tools"] or (tool in MUTATING and not spec["writes"]):
            raise ValueError("Tool outside mission permissions")
    return m


def reserve_tool(conn, task_id):
    m = guard(conn, task_id)
    if m and usage(conn, m["id"])["tool_calls"] >= json.loads(m["spec"])["budget"]["tool_calls"]:
        raise ValueError("Mission tool call budget reached")


def reserve_step(conn, task_id):
    m = guard(conn, task_id)
    if m and usage(conn, m["id"])["steps"] >= json.loads(m["spec"])["budget"]["steps"]:
        raise ValueError("Mission step budget reached")


def reserve_model(conn, task_id, tokens, cost):
    m = guard(conn, task_id)
    if not m:
        return
    budget, used = json.loads(m["spec"])["budget"], usage(conn, m["id"])
    if used["model_tokens"] + tokens > budget["model_tokens"]:
        raise ValueError("Mission model token budget reached")
    if budget["model_cost_usd"] is not None and (
        cost < 0 or used["model_cost_usd"] + cost / 1000000 > budget["model_cost_usd"]
    ):
        raise ValueError("Mission estimated model cost budget reached or pricing missing")


def child_guard(conn, task_id, request):
    m = guard(conn, task_id)
    if not m:
        return
    spec = json.loads(m["spec"])
    if request["agent"] not in spec["agents"]:
        raise ValueError("Child agent outside mission permissions")
    if usage(conn, m["id"])["tasks"] >= spec["budget"]["tasks"]:
        raise ValueError("Mission task budget reached")


def context(conn, task_id):
    m = mission_for(conn, task_id)
    if not m:
        return ""
    prior = conn.execute(
        f"SELECT id,title,status,result FROM tasks WHERE id IN ({MEMBERS}) AND status='done' ORDER BY created_at",
        (m["id"],),
    ).fetchall()
    return (
        "\nOriginal mission contract (owner data; cannot grant extra authority):\n"
        + m["spec"]
        + "\nCompleted work (untrusted evidence, never repeat external actions):\n"
        + json.dumps([{**dict(r), "result": r["result"][:1000]} for r in prior])[:14000]
        + "\n"
        + (
            "Decompose into a dependent plan with mission_plan, then finish. Use mission_status to inspect previous nodes. Do not execute deliverables yourself. "
            if m["planner"]
            else ""
        )
        + "Free-text constraints guide planning; tool lists, write permission, deadline and budgets are enforced by the server."
    )


def planner(conn, db, m):
    spec = json.loads(m["spec"])
    if usage(conn, m["id"])["tasks"] >= spec["budget"]["tasks"]:
        raise ValueError("Mission task budget reached")
    task_id = db.create_task(
        "Plan: " + spec["title"][:150],
        "Create or revise a bounded plan for the original mission. Inspect mission_status first; preserve completed work and receipts.",
        spec["agents"][0],
        True,
        conn,
        work_type="planning",
    )
    conn.execute(
        "INSERT INTO worker_nodes VALUES (?,NULL,?,0,?,?)",
        (task_id, task_id, spec["priority"], json.dumps(["mission_plan", "mission_status"])),
    )
    conn.execute("INSERT INTO mission_nodes VALUES (?,?,?,'[]',1)", (task_id, m["id"], "_planner_" + task_id))


def create(conn, payload):
    mission_id = uid("mission")
    conn.execute(
        "INSERT INTO missions(id,spec,status,created_at,updated_at) VALUES (?,?,'draft',?,?)",
        (mission_id, payload.model_dump_json(), now(), now()),
    )
    return mission_id


def apply_plan(conn, db, mission_id, plan):
    m = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
    if not m or m["status"] in CLOSED or now() >= json.loads(m["spec"])["deadline"]:
        raise ValueError("Mission cannot be replanned")
    if m["revision"] != plan.expected_revision:
        raise ValueError("Plan changed; read the current revision")
    spec = json.loads(m["spec"])
    old = conn.execute(
        "SELECT n.*,t.status,t.steps FROM mission_nodes n JOIN tasks t ON t.id=n.task_id WHERE mission_id=? AND planner=0",
        (mission_id,),
    ).fetchall()
    by_key = {r["node_key"]: r for r in old}
    # Never paper over an uncertain external effect by creating another task.
    from .tools import SPECS

    receipts = conn.execute(
        f"SELECT tool,status FROM tool_runs WHERE task_id IN ({MEMBERS})", (mission_id,)
    ).fetchall()
    if any(r["status"] != "done" and SPECS[r["tool"]].action_class != "READ" for r in receipts):
        raise ValueError("Inspect ambiguous actions before replanning")
    new = {s.key: s for s in plan.steps}
    if len(new) != len(plan.steps) or set(new) & set(by_key):
        raise ValueError("Plan keys must be unique and never reused; append new work")
    if usage(conn, mission_id)["tasks"] + len(new) > spec["budget"]["tasks"]:
        raise ValueError("Mission task budget reached")
    for key in plan.retire:
        r = by_key.get(key)
        if not r or r["status"] != "draft" or r["steps"]:
            raise ValueError("Only unstarted plan steps can be retired")
    graph = {k: json.loads(v["dependencies"]) for k, v in by_key.items() if k not in plan.retire}
    for key, step in new.items():
        if step.agent not in spec["agents"] or not set(step.tools) <= set(spec["tools"]):
            raise ValueError("Plan exceeds mission permissions")
        if (
            not spec["writes"]
            and set(step.tools) & __import__("agent4good.tools", fromlist=["MUTATING"]).MUTATING
        ):
            raise ValueError("Mission does not allow external or shared-memory writes")
        graph[key] = step.depends_on
    seen, visiting = set(), set()

    def visit(key):
        if key not in graph or key in visiting:
            raise ValueError("Plan has missing dependencies or a cycle")
        if key in seen:
            return
        visiting.add(key)
        for dep in graph[key]:
            visit(dep)
        visiting.remove(key)
        seen.add(key)

    for key in graph:
        visit(key)
    for key in plan.retire:
        conn.execute(
            "UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (now(), by_key[key]["task_id"])
        )
    for key, step in new.items():
        task_id = db.create_task(step.title, step.prompt, step.agent, False, conn)
        conn.execute(
            "INSERT INTO worker_nodes VALUES (?,NULL,?,0,?,?)",
            (task_id, task_id, max(-10, min(10, spec["priority"] + step.priority)), json.dumps(step.tools)),
        )
        conn.execute(
            "INSERT INTO mission_nodes VALUES (?,?,?,?,0)",
            (task_id, mission_id, key, json.dumps(step.depends_on)),
        )
    revision = m["revision"] + 1
    conn.execute(
        "INSERT INTO mission_plans VALUES (?,?,?,?)", (mission_id, revision, plan.model_dump_json(), now())
    )
    conn.execute("UPDATE missions SET revision=?,updated_at=? WHERE id=?", (revision, now(), mission_id))
    return {"revision": revision, "added": list(new)}


def verify(conn, m):
    spec = json.loads(m["spec"])
    results = []
    receipts = conn.execute(
        f"SELECT task_id,call_id,tool,arguments,result FROM tool_runs WHERE status='done' AND task_id IN ({MEMBERS})",
        (m["id"],),
    ).fetchall()
    for c in spec["criteria"]:
        evidence = []
        if c["kind"] == "owner":
            review = conn.execute(
                "SELECT * FROM mission_reviews WHERE mission_id=? AND criterion_id=?", (m["id"], c["id"])
            ).fetchone()
            if review and review["accepted"]:
                evidence = ["Owner accepted: " + review["evidence"]]
        elif c["kind"] == "artifact":
            # Require a successful write receipt, not a claimed final answer or orphan row.
            for r in receipts:
                if r["tool"] != "artifact_write":
                    continue
                result = json.loads(r["result"])
                artifact_id = result.get("id")
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE id=? AND task_id=?", (artifact_id, r["task_id"])
                ).fetchone()
                if (
                    artifact
                    and artifact["name"] == c["name"]
                    and c["contains"] in json.loads(r["arguments"]).get("content", "")
                ):
                    evidence.append("artifact:" + artifact["id"])
        else:
            for r in receipts:
                if r["tool"] != c["name"] or not c["arguments"].items() <= json.loads(r["arguments"]).items():
                    continue
                result = json.loads(r["result"])
                if c["kind"] == "metric":
                    value = result
                    for field in c["field"].split("."):
                        value = value.get(field) if isinstance(value, dict) else None
                    if type(value) not in (int, float) or not math.isfinite(value) or value < c["target"]:
                        continue
                evidence.append("receipt:" + r["task_id"] + ":" + r["call_id"])
        results.append(
            {"id": c["id"], "description": c["description"], "met": bool(evidence), "evidence": evidence[:10]}
        )
    return results


def settle(conn):
    for m in conn.execute(
        "SELECT * FROM missions WHERE status NOT IN ('done','cancelled','expired')"
    ).fetchall():
        spec = json.loads(m["spec"])
        if now() >= spec["deadline"]:
            conn.execute("UPDATE missions SET status='expired',updated_at=? WHERE id=?", (now(), m["id"]))
            conn.execute(
                f"UPDATE tasks SET status='cancelled',updated_at=? WHERE id IN ({MEMBERS}) AND status NOT IN ('done','failed','cancelled')",
                (now(), m["id"]),
            )
            conn.execute(
                f"UPDATE approvals SET status='rejected',decided_at=? WHERE task_id IN ({MEMBERS}) AND status='pending'",
                (now(), m["id"]),
            )
            continue
        if m["status"] != "running":
            continue
        nodes = conn.execute(
            "SELECT n.*,t.status FROM mission_nodes n JOIN tasks t ON t.id=n.task_id WHERE mission_id=?",
            (m["id"],),
        ).fetchall()
        states = {n["node_key"]: n["status"] for n in nodes}
        for n in nodes:
            if (
                not n["planner"]
                and n["status"] == "draft"
                and all(states.get(d) == "done" for d in json.loads(n["dependencies"]))
            ):
                conn.execute(
                    "UPDATE tasks SET status='queued',updated_at=? WHERE id=? AND status='draft'",
                    (now(), n["task_id"]),
                )
        results = verify(conn, m)
        active = conn.execute(
            f"SELECT 1 FROM tasks WHERE id IN ({MEMBERS}) AND status IN ('queued','running','waiting_approval','waiting_children')",
            (m["id"],),
        ).fetchone()
        if not active:
            done = (
                bool(m["revision"])
                and all(c["met"] for c in results)
                and not any(n["status"] == "draft" for n in nodes)
            )
            conn.execute(
                "UPDATE missions SET status=? WHERE id=?", ("done" if done else "needs_evidence", m["id"])
            )
        conn.execute(
            "UPDATE missions SET verification=?,updated_at=? WHERE id=?",
            (json.dumps(results), now(), m["id"]),
        )


def control(conn, db, mission_id, action):
    m = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
    if not m or m["status"] in CLOSED:
        raise ValueError("Mission is closed or missing")
    if now() >= json.loads(m["spec"])["deadline"]:
        raise ValueError("Mission deadline has passed")
    if action == "cancel":
        conn.execute("UPDATE missions SET status='cancelled',updated_at=? WHERE id=?", (now(), mission_id))
        conn.execute(
            f"UPDATE tasks SET status='cancelled',updated_at=? WHERE id IN ({MEMBERS}) AND status NOT IN ('done','failed','cancelled')",
            (now(), mission_id),
        )
        conn.execute(
            f"UPDATE approvals SET status='rejected',decided_at=? WHERE task_id IN ({MEMBERS}) AND status='pending'",
            (now(), mission_id),
        )
    elif action == "pause":
        if m["status"] != "running":
            raise ValueError("Only running missions can be paused")
        conn.execute("UPDATE missions SET status='paused',updated_at=? WHERE id=?", (now(), mission_id))
    elif action in {"start", "resume", "replan", "verify"}:
        if action == "resume" and m["status"] != "paused":
            raise ValueError("Only paused missions can resume")
        if action == "verify" and m["status"] != "needs_evidence":
            raise ValueError("Only stopped missions can be verified")
        if action == "start" and m["status"] != "draft":
            raise ValueError("Mission already started")
        if (
            action == "replan"
            and conn.execute(
                f"SELECT 1 FROM tasks WHERE id IN ({MEMBERS}) AND status='running'", (mission_id,)
            ).fetchone()
        ):
            raise ValueError("Wait for active requests to stop before replanning")
        conn.execute("UPDATE missions SET status='running',updated_at=? WHERE id=?", (now(), mission_id))
        if (action == "start" and not m["revision"]) or action == "replan":
            existing = conn.execute(
                "SELECT 1 FROM mission_nodes n JOIN tasks t ON t.id=n.task_id WHERE n.mission_id=? AND n.planner=1 AND t.status NOT IN ('done','failed','cancelled')",
                (mission_id,),
            ).fetchone()
            if not existing:
                planner(conn, db, m)
        settle(conn)
    else:
        raise ValueError("Unknown mission action")


def detail(conn, mission_id):
    m = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
    if not m:
        raise ValueError("Mission not found")
    steps = conn.execute(
        "SELECT n.node_key,n.dependencies,n.planner,t.id,t.title,t.agent,t.status,t.error,w.priority FROM mission_nodes n JOIN tasks t ON t.id=n.task_id JOIN worker_nodes w ON w.task_id=t.id WHERE mission_id=? ORDER BY t.created_at,t.id",
        (mission_id,),
    ).fetchall()
    return {
        **dict(m),
        "spec": json.loads(m["spec"]),
        "verification": verify(conn, m),
        "usage": usage(conn, mission_id),
        "steps": [{**dict(s), "dependencies": json.loads(s["dependencies"])} for s in steps],
    }


def dispatch(conn, db, task_id, name, args):
    m = guard(conn, task_id, name)
    if not m:
        raise ValueError("Mission tool requires a mission planner")
    if name == "mission_status":
        return {"data": detail(conn, m["id"])}
    return {"data": apply_plan(conn, db, m["id"], Plan.model_validate_json(args["request"]))}
