"""Durable, bounded task trees. All mutations run in the caller's transaction."""

import json
from .db import now, uid
from .catalog import AGENTS

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS worker_nodes (task_id TEXT PRIMARY KEY REFERENCES tasks(id), parent_id TEXT REFERENCES tasks(id), root_id TEXT NOT NULL REFERENCES tasks(id), depth INTEGER NOT NULL, priority INTEGER NOT NULL, tools TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS worker_root ON worker_nodes(root_id)",
    "CREATE TABLE IF NOT EXISTS worker_context (task_id TEXT NOT NULL REFERENCES tasks(id), scope TEXT NOT NULL, revision INTEGER NOT NULL, content TEXT NOT NULL, PRIMARY KEY(task_id,scope))",
    "CREATE TABLE IF NOT EXISTS worker_messages (id TEXT PRIMARY KEY, sender TEXT NOT NULL REFERENCES tasks(id), recipient TEXT NOT NULL REFERENCES tasks(id), content TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS worker_resources (resource TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id))",
]
TERMINAL = {"done", "failed", "cancelled"}
NAMES = {"worker_spawn", "worker_message", "worker_context", "worker_results", "worker_wait"}


def initialize(conn):
    for sql in SCHEMA:
        conn.execute(sql)


def node(conn, task_id):
    from .tools import SPECS

    row = conn.execute("SELECT * FROM worker_nodes WHERE task_id=?", (task_id,)).fetchone()
    if row:
        return row
    conn.execute(
        "INSERT INTO worker_nodes VALUES (?,NULL,?,0,0,?)",
        (task_id, task_id, json.dumps([t for t in SPECS if not t.startswith("mission_")])),
    )
    return conn.execute("SELECT * FROM worker_nodes WHERE task_id=?", (task_id,)).fetchone()


def ancestors(conn, task_id):
    rows = []
    row = conn.execute("SELECT * FROM worker_nodes WHERE task_id=?", (task_id,)).fetchone()
    while row and row["parent_id"]:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (row["parent_id"],)).fetchone()
        if not task or task["owner_id"] != "owner":
            raise ValueError("Invalid parent boundary")
        rows.append(task)
        row = conn.execute("SELECT * FROM worker_nodes WHERE task_id=?", (task["id"],)).fetchone()
        if len(rows) > 8:
            raise ValueError("Invalid worker tree")
    return rows


def enforce(conn, task, name):
    from .missions import guard

    guard(conn, task["id"], name)
    from .scheduling import guard as schedule_guard

    schedule_guard(conn, task["id"])
    row = conn.execute("SELECT * FROM worker_nodes WHERE task_id=?", (task["id"],)).fetchone()
    if row and name not in json.loads(row["tools"]):
        raise ValueError("Tool outside inherited worker permissions")
    for parent in ancestors(conn, task["id"]):
        if parent["status"] in TERMINAL:
            raise ValueError("Parent is closed")


def dispatch(conn, db, settings, task_id, name, args):
    own = node(conn, task_id)
    if name == "worker_spawn":
        request = json.loads(args["request"])
        allowed = {"title", "prompt", "agent", "tools", "priority", "reason"}
        if not isinstance(request, dict) or set(request) - allowed:
            raise ValueError("Invalid delegation request")
        for field in ("title", "prompt", "reason"):
            if not isinstance(request.get(field), str) or not 1 <= len(request[field].strip()) <= 10000:
                raise ValueError("Delegation needs a bounded title, prompt and reason")
        if request.get("agent") not in {a["id"] for a in AGENTS}:
            raise ValueError("Unknown specialist role")
        permissions = request.get("tools")
        if not isinstance(permissions, list) or any(not isinstance(t, str) for t in permissions):
            raise ValueError("Supply an explicit child tool list")
        if not set(permissions) <= set(json.loads(own["tools"])) or "memory_write" in permissions:
            raise ValueError("Child permissions must narrow; project memory writes belong to the parent")
        priority = request.get("priority", 0)
        if type(priority) is not int or not -10 <= priority <= 10:
            raise ValueError("Priority must be an integer from -10 to 10")
        count = conn.execute(
            "SELECT COUNT(*) FROM worker_nodes WHERE root_id=?", (own["root_id"],)
        ).fetchone()[0]
        if own["depth"] >= settings.max_worker_depth or count >= settings.max_worker_tree_size:
            raise ValueError("Worker tree size or depth budget reached")
        if (
            conn.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
            >= settings.max_queued_tasks
        ):
            raise ValueError("Worker queue capacity reached")
        from .missions import child_guard

        child_guard(conn, task_id, request)
        child = db.create_task(request["title"], request["prompt"], request["agent"], True, conn)
        conn.execute(
            "INSERT INTO worker_nodes VALUES (?,?,?,?,?,?)",
            (child, task_id, own["root_id"], own["depth"] + 1, priority, json.dumps(permissions)),
        )
        from .timeline import emit

        emit(conn, task_id, "delegated", "Child agent queued", child_id=child, status="queued")
        return {"data": {"child_id": child}}
    if name == "worker_message":
        recipient = args["recipient"]
        target = conn.execute("SELECT * FROM worker_nodes WHERE task_id=?", (recipient,)).fetchone()
        if not target or not (target["parent_id"] == task_id or own["parent_id"] == recipient):
            raise ValueError("Messages require a direct parent-child relationship")
        if len(args["content"]) > 4000:
            raise ValueError("Message exceeds 4000 characters")
        if (
            conn.execute("SELECT COUNT(*) FROM worker_messages WHERE sender=?", (task_id,)).fetchone()[0]
            >= 100
        ):
            raise ValueError("Message budget reached")
        message = uid("message")
        conn.execute(
            "INSERT INTO worker_messages VALUES (?,?,?,?,?)",
            (message, task_id, recipient, args["content"], now()),
        )
        return {"data": {"message_id": message}}
    if name == "worker_context":
        scope = args["scope"]
        if scope not in {"private", "shared"}:
            raise ValueError("Context scope must be private or shared")
        owner = task_id if scope == "private" else own["root_id"]
        row = conn.execute(
            "SELECT * FROM worker_context WHERE task_id=? AND scope=?", (owner, scope)
        ).fetchone()
        revision = row["revision"] if row else 0
        if args["operation"] == "write":
            if args["revision"] != str(revision):
                raise ValueError("Context changed; read the current revision before writing")
            if len(args["content"]) > 10000:
                raise ValueError("Context exceeds 10000 characters")
            revision += 1
            conn.execute(
                "INSERT INTO worker_context VALUES (?,?,?,?) ON CONFLICT(task_id,scope) DO UPDATE SET revision=excluded.revision,content=excluded.content",
                (owner, scope, revision, args["content"]),
            )
            content = args["content"]
        elif args["operation"] == "read":
            content = row["content"] if row else ""
        else:
            raise ValueError("Context operation must be read or write")
        return {"data": {"revision": revision, "content": content}}
    if name == "worker_results":
        children = conn.execute(
            "SELECT t.id,t.agent,t.status,t.result,t.error FROM tasks t JOIN worker_nodes n ON t.id=n.task_id WHERE n.parent_id=? ORDER BY n.priority DESC,t.created_at,t.id",
            (task_id,),
        ).fetchall()
        messages = conn.execute(
            "SELECT * FROM worker_messages WHERE recipient=? ORDER BY created_at,id", (task_id,)
        ).fetchall()
        return {
            "data": {
                "self": {"task_id": task_id, "parent_id": own["parent_id"], "root_id": own["root_id"]},
                "children": [
                    {**dict(c), "result": c["result"][:750], "error": c["error"][:250]} for c in children
                ],
                "messages": [{**dict(m), "content": m["content"][:1000]} for m in messages][-10:],
            }
        }
    if name == "worker_wait":
        return {"data": {"waiting": True}}
    raise ValueError("Unknown coordination tool")


def settle(conn):
    from .missions import settle as settle_missions

    settle_missions(conn)
    from .scheduling import settle as settle_schedules

    settle_schedules(conn)
    # Cancellation/failure is transitive. A failed child does not cancel its siblings.
    for _ in range(9):
        children = conn.execute(
            "SELECT t.id FROM tasks t JOIN worker_nodes n ON t.id=n.task_id JOIN tasks p ON p.id=n.parent_id WHERE p.status IN ('failed','cancelled') AND t.status NOT IN ('done','failed','cancelled')"
        ).fetchall()
        if not children:
            break
        for child in children:
            conn.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (now(), child["id"]))
            conn.execute(
                "UPDATE approvals SET status='rejected',decided_at=? WHERE task_id=? AND status='pending'",
                (now(), child["id"]),
            )
    waiting = conn.execute("SELECT id FROM tasks WHERE status='waiting_children'").fetchall()
    for parent in waiting:
        active = conn.execute(
            "SELECT 1 FROM worker_nodes n JOIN tasks t ON t.id=n.task_id WHERE n.parent_id=? AND t.status NOT IN ('done','failed','cancelled')",
            (parent["id"],),
        ).fetchone()
        if not active:
            conn.execute("UPDATE tasks SET status='queued',updated_at=? WHERE id=?", (now(), parent["id"]))


def budget_step(conn, task_id, settings):
    from .missions import reserve_step

    reserve_step(conn, task_id)
    own = node(conn, task_id)
    used = conn.execute(
        "SELECT COALESCE(SUM(t.steps),0) FROM tasks t JOIN worker_nodes n ON t.id=n.task_id WHERE n.root_id=?",
        (own["root_id"],),
    ).fetchone()[0]
    if used >= settings.max_worker_tree_steps:
        raise RuntimeError("Worker tree step budget reached")
    conn.execute(
        "UPDATE tasks SET steps=steps+1,updated_at=? WHERE id=? AND status='running'", (now(), task_id)
    )
