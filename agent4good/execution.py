"""Versioned execution journal. SQLite transactions checkpoint each observed plan revision."""

import fcntl
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from .db import now


@contextmanager
def task_lock(db, task_id):
    if db.distributed:
        with db.task_lock(task_id) as acquired:
            yield acquired
        return
    # Hash untrusted IDs; never use them as filesystem paths.
    path = Path(db.path).parent / ("execution-" + hashlib.sha256(task_id.encode()).hexdigest() + ".lock")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class ExecutionJournal:
    def __init__(self, db, settings=None):
        self.db = db
        self.settings = settings

    def checkpoint(self, task_id, items, pending, final_text=None):
        from .tools import SPECS

        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["status"] != "running":
                return
            conn.execute(
                "INSERT OR IGNORE INTO executions(task_id,started_at) VALUES (?,?)", (task_id, now())
            )
            revision = (
                conn.execute("SELECT revision FROM executions WHERE task_id=?", (task_id,)).fetchone()[0] + 1
            )
            previous = None
            for call in pending:
                name, args = call["name"], json.loads(call["arguments"])
                if name not in SPECS:
                    raise ValueError("Unknown tool")
                fingerprint = hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()
                # Writes with identical content denote the same action within one task.
                # Repeated reads remain distinct observations.
                key = (
                    fingerprint
                    if SPECS[name].action_class != "READ"
                    and not (name == "worker_context" and args.get("operation") == "read")
                    and name != "worker_wait"
                    else call["call_id"]
                )
                existing = conn.execute(
                    "SELECT * FROM plan_steps WHERE task_id=? AND action_key=?", (task_id, key)
                ).fetchone()
                if existing and (existing["tool"] != name or existing["fingerprint"] != fingerprint):
                    raise ValueError("Plan action identity changed")
                action_id = existing["action_id"] if existing else call["call_id"]
                if not existing:
                    # First call ID preserves legacy approvals during migration.
                    collision = conn.execute(
                        "SELECT 1 FROM plan_steps WHERE task_id=? AND action_id=?", (task_id, action_id)
                    ).fetchone()
                    if collision:
                        raise ValueError("Provider reused a call ID for a different action")
                    conn.execute(
                        "INSERT INTO plan_steps(task_id,action_id,action_key,fingerprint,tool,revision,depends_on) VALUES (?,?,?,?,?,?,?)",
                        (task_id, action_id, key, fingerprint, name, revision, previous),
                    )
                call["action_id"] = action_id
                previous = action_id
            conn.execute(
                "UPDATE tasks SET items=?,pending=?,updated_at=? WHERE id=?",
                (
                    json.dumps([{k: v for k, v in item.items() if k != "action_id"} for item in items]),
                    json.dumps(pending),
                    now(),
                    task_id,
                ),
            )
            conn.execute(
                "UPDATE executions SET revision=?,final_text=?,phase=? WHERE task_id=?",
                (revision, final_text, "verifying" if final_text is not None else "executing", task_id),
            )

            from .memory import runtime_record

            observations = [i for i in items if i.get("type") == "function_call_output"]
            snapshot = (
                task["title"]
                + "\n"
                + task["prompt"][:1000]
                + "\n"
                + json.dumps(
                    {
                        "revision": revision,
                        "pending_tools": [c["name"] for c in pending],
                        "last_observation": observations[-1].get("output", "")[:2000] if observations else "",
                    }
                )
            )
            runtime_record(conn, task_id, "working", snapshot)

    def before(self, task_id, call):
        step = self.db.one(
            "SELECT * FROM plan_steps WHERE task_id=? AND action_id=?", (task_id, call["action_id"])
        )
        if step["depends_on"]:
            dependency = self.db.one(
                "SELECT status FROM plan_steps WHERE task_id=? AND action_id=?", (task_id, step["depends_on"])
            )
            if dependency["status"] not in {"done", "observed_failure"}:
                raise RuntimeError("Plan dependency is incomplete")

    def observe(self, task_id, call, result, failed=False):
        self.db.execute(
            "UPDATE plan_steps SET status=?,observation=? WHERE task_id=? AND action_id=?",
            ("observed_failure" if failed else "done", json.dumps(result), task_id, call["action_id"]),
        )

    def finish(self, task_id, result):
        if not result.strip():
            raise RuntimeError("Model returned no final result")
        from .quality import gate, snapshot

        candidate = snapshot(self.db, self.settings, task_id, result)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task["status"] != "running":
                return
            if conn.execute(
                "SELECT 1 FROM worker_nodes n JOIN tasks t ON t.id=n.task_id WHERE n.parent_id=? AND t.status NOT IN ('done','failed','cancelled')",
                (task_id,),
            ).fetchone():
                raise RuntimeError("Child workers are still active; use worker_wait before completing")
            if json.loads(task["pending"]):
                raise RuntimeError("Verification found pending actions")
            if conn.execute(
                "SELECT 1 FROM tool_runs WHERE task_id=? AND status!='done' AND tool NOT IN ('memory_read','memory_search','web_fetch','web_search','github_read_file','github_list_issues')",
                (task_id,),
            ).fetchone():
                raise RuntimeError("Verification found an ambiguous write; inspect before retrying")
            if conn.execute(
                "SELECT 1 FROM plan_steps WHERE task_id=? AND status NOT IN ('done','observed_failure')",
                (task_id,),
            ).fetchone():
                raise RuntimeError("Verification found incomplete plan steps")
            if not gate(conn, self.db, self.settings, task, result, candidate):
                return
            independently_verified = candidate is not None
            conn.execute(
                "UPDATE tasks SET status='done',result=?,error='',updated_at=? WHERE id=?",
                (result, now(), task_id),
            )
            conn.execute(
                "UPDATE executions SET phase='verified',verification=? WHERE task_id=?",
                (
                    json.dumps(
                        {
                            "version": 1,
                            "check": "all plan steps observed; no pending or ambiguous writes",
                            "scope": "independent configured outcome checks"
                            if independently_verified
                            else "execution integrity, not independent outcome evaluation",
                        }
                    ),
                    task_id,
                ),
            )
            from .memory import runtime_record

            runtime_record(
                conn,
                task_id,
                "episodic",
                task["title"]
                + (
                    "\nIndependent configured quality checks passed.\n"
                    if independently_verified
                    else "\nExecution completed; outcome is not independently verified.\n"
                )
                + result,
            )
        self.db.event(task_id, "completed", "Execution checks passed; review result and saved evidence")
