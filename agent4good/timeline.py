"""Owner audit projection over the existing durable event log.

Cursor order is commit order (SQLite writes / PostgreSQL control lock). Structured
messages use a fixed allowlist; transcripts, tool arguments and reasoning never enter
this projection. Old activity remains readable without backfilling invented history.
"""

import json
from datetime import datetime, timezone

from .db import now

FIELDS = {"step_id", "tool", "approval_id", "model_call_id", "revision", "status", "child_id"}


def emit(conn, task_id, kind, message, **fields):
    payload = {"audit_version": 1, "message": message}
    payload.update({k: v for k, v in fields.items() if k in FIELDS})
    conn.execute(
        "INSERT INTO events(task_id,kind,message,created_at) VALUES (?,?,?,?)",
        (task_id, kind, json.dumps(payload), now()),
    )


def decode(row):
    result = dict(row)
    try:
        value = json.loads(result["message"])
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict) and value.get("audit_version") == 1:
        result["message"] = value.get("message", "")
        result.update({k: v for k, v in value.items() if k in FIELDS})
    return result


def usage(calls, profile):
    """Provider-reported tokens are measured. Prices are always owner estimates."""
    reported, missing, estimated = 0, 0, 0.0
    priced = True
    for call in calls:
        data = json.loads(call["usage"] or "{}")
        if not isinstance(data, dict):
            data = {}
        total = data.get("total_tokens")
        if not isinstance(total, (int, float)) or total < 0:
            missing += 1
        else:
            reported += total
        inp, out = data.get("input_tokens"), data.get("output_tokens")
        pi, po = profile.get("input_usd_per_million"), profile.get("output_usd_per_million")
        if any(not isinstance(v, (int, float)) or v < 0 for v in [inp, out, pi, po]):
            priced = False
        else:
            estimated += (inp * pi + out * po) / 1000000
    return {
        "calls": len(calls),
        "reported_tokens": reported if calls and missing < len(calls) else None,
        "tokens_label": "provider_reported"
        if calls and not missing
        else "partial"
        if calls and missing < len(calls)
        else "unavailable",
        "unreported_calls": missing,
        "estimated_model_usd": round(estimated, 8) if calls and priced else None,
        "cost_label": "estimated" if calls and priced else "unavailable",
        "actual_cost_usd": None,
        "tool_cost_usd": None,
        "reserved_tokens": sum(c["reserved_tokens"] for c in calls),
    }


def read(db, credentials, *, after=0, limit=100, task_id=None, agent=None, kind=None, through=None):
    # One bounded transaction gives events, status and usage a consistent snapshot.
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        where = "t.owner_id='owner'"
        args = []
        if task_id:
            selected = conn.execute("SELECT owner_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not selected or selected["owner_id"] != "owner":
                raise LookupError("Task not found")
            where += " AND (t.id=? OR t.id IN (SELECT task_id FROM worker_nodes WHERE root_id=?))"
            args.extend([task_id, task_id])
        if agent:
            where += " AND t.agent=?"
            args.append(agent)
        # Capture high watermark before paging. Reuse through for a stable export.
        high = conn.execute(
            "SELECT COALESCE(MAX(e.id),0) FROM events e LEFT JOIN tasks t ON t.id=e.task_id WHERE t.owner_id='owner' OR e.task_id IS NULL"
        ).fetchone()[0]
        if through is not None:
            high = min(high, through)
        clause = "(" + where + (" OR e.task_id IS NULL" if not task_id and not agent else "") + ")"
        event_args = args + [after, high]
        if kind:
            clause += " AND e.kind=?"
            # Kind placeholder precedes cursor placeholders below.
            event_args = args + [kind, after, high]
        rows = conn.execute(
            "SELECT e.*,t.agent FROM events e LEFT JOIN tasks t ON t.id=e.task_id WHERE "
            + clause
            + " AND e.id>? AND e.id<=? ORDER BY e.id LIMIT ?",
            (*event_args, limit + 1),
        ).fetchall()
        more = len(rows) > limit
        rows = [decode(r) for r in rows[:limit]]
        # Global feed needs only tasks in this page. Selected run includes its whole tree.
        if task_id:
            task_rows = conn.execute(
                "SELECT t.* FROM tasks t WHERE " + where + " ORDER BY t.created_at,t.id", args
            ).fetchall()
        else:
            ids = sorted({r["task_id"] for r in rows if r["task_id"]})
            task_rows = (
                conn.execute(
                    "SELECT t.* FROM tasks t WHERE t.owner_id='owner' AND t.id IN ("
                    + ",".join("?" for _ in ids)
                    + ")",
                    ids,
                ).fetchall()
                if ids
                else []
            )
        tasks = []
        for t in task_rows:
            ident = t["id"]
            node = conn.execute(
                "SELECT parent_id,root_id FROM worker_nodes WHERE task_id=?", (ident,)
            ).fetchone()
            root = node["root_id"] if node else ident
            mission = conn.execute(
                "SELECT m.id,m.spec FROM missions m JOIN mission_nodes n ON n.mission_id=m.id WHERE n.task_id=?",
                (root,),
            ).fetchone()
            execution = conn.execute(
                "SELECT started_at,phase,revision,recoveries,verification FROM executions WHERE task_id=?",
                (ident,),
            ).fetchone()
            route = conn.execute("SELECT profile FROM model_routes WHERE task_id=?", (ident,)).fetchone()
            profile = json.loads(route["profile"]) if route else {}
            calls = [
                dict(c)
                for c in conn.execute(
                    "SELECT * FROM model_calls WHERE task_id=? ORDER BY created_at,id", (ident,)
                )
            ]
            duration = None
            if execution:
                end = (
                    datetime.fromisoformat(t["updated_at"])
                    if t["status"] in {"done", "failed", "cancelled"}
                    else datetime.now(timezone.utc)
                )
                duration = max(
                    0, round((end - datetime.fromisoformat(execution["started_at"])).total_seconds(), 1)
                )
            tasks.append(
                {
                    "id": ident,
                    "title": t["title"],
                    "goal": t["prompt"],
                    "agent": t["agent"],
                    "status": t["status"],
                    "parent_id": node["parent_id"] if node else None,
                    "root_id": root,
                    "mission_id": mission["id"] if mission else None,
                    "mission_goal": json.loads(mission["spec"]).get("objective") if mission else None,
                    "execution": dict(execution) if execution else None,
                    "duration_seconds": duration,
                    "model": profile.get("model"),
                    "provider": profile.get("provider"),
                    "usage": usage(calls, profile),
                    "result": t["result"],
                    "error": t["error"],
                    "plan": [
                        dict(p)
                        for p in conn.execute(
                            "SELECT action_id,tool,revision,depends_on,status FROM plan_steps WHERE task_id=? ORDER BY revision,action_id",
                            (ident,),
                        )
                    ],
                    "calls": [
                        {"id": c["id"], "status": c["status"], "created_at": c["created_at"]} for c in calls
                    ],
                    "approvals": [
                        dict(a)
                        for a in conn.execute(
                            "SELECT id,call_id,tool,status,created_at,decided_at FROM approvals WHERE task_id=? ORDER BY created_at,id",
                            (ident,),
                        )
                    ],
                    "artifacts": [
                        dict(a)
                        for a in conn.execute(
                            "SELECT id,name,created_at FROM artifacts WHERE task_id=? ORDER BY created_at,id",
                            (ident,),
                        )
                    ],
                }
            )
        by_id = {t["id"]: t for t in tasks}
        for row in rows:
            task = by_id.get(row["task_id"], {})
            row.update({k: task.get(k) for k in ("root_id", "parent_id", "mission_id")})
        response = {
            "version": 1,
            "events": rows,
            "tasks": tasks,
            "has_more": more,
            "next_cursor": rows[-1]["id"] if more and rows else high,
            "through": high,
            "captured_at": now(),
        }
    return credentials.redact(response)
