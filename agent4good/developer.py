"""Version 1 developer contract. Replay is a read-only saved-record projection."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, HTTPException
from fastapi.routing import APIRoute
from fastapi.openapi.utils import get_openapi


def worker_readiness(db):
    if db.distributed:
        count = db.one(
            "SELECT COUNT(*) AS n FROM workers WHERE last_seen>EXTRACT(EPOCH FROM clock_timestamp())-30"
        )["n"]
        return {"online": count > 0, "active_workers": count}
    last = db.settings().get("worker_last_seen")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        online = 0 <= age < 30
    except (ValueError, TypeError):
        online = False
    return {"online": online, "last_seen": last}


def replay(db, credentials, task_id, offset=0, limit=100):
    # No Engine, provider, policy approval or registry invocation enters this path.
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND owner_id='owner'", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        execution = conn.execute("SELECT * FROM executions WHERE task_id=?", (task_id,)).fetchone()
        steps = conn.execute(
            "SELECT action_id,tool,revision,depends_on,status,observation FROM plan_steps WHERE task_id=? ORDER BY revision,action_id LIMIT ? OFFSET ?",
            (task_id, limit + 1, offset),
        ).fetchall()
        receipts = conn.execute(
            "SELECT call_id,tool,status,result,attempts FROM tool_runs WHERE task_id=? ORDER BY call_id LIMIT ? OFFSET ?",
            (task_id, limit + 1, offset),
        ).fetchall()
        doc = {
            "api_version": "1",
            "mode": "recorded",
            "external_effects": False,
            "task": {k: task[k] for k in ("id", "title", "status", "steps", "result", "error")},
            "execution": dict(execution) if execution else None,
            "steps": [dict(r) for r in steps[:limit]],
            "receipts": [dict(r) for r in receipts[:limit]],
            "next_offset": offset + limit if max(len(steps), len(receipts)) > limit else None,
            "warning": "Saved observations only. No tools or models ran. Started receipts remain uncertain.",
        }
    # Bound even historical malformed receipts; credentials are removed before truncation.
    doc = credentials.redact(doc)
    for rows, field in [(doc["steps"], "observation"), (doc["receipts"], "result")]:
        for row in rows:
            value = row.get(field)
            if isinstance(value, str) and len(value) > 20000:
                row[field] = value[:20000]
                row["truncated"] = True
    return doc


def install_api(app, db, registry, auth):
    # Share endpoint functions and dependencies, not HTTP redirects or duplicated policy logic.
    supported = (
        "/api/login",
        "/api/logout",
        "/api/session",
        "/api/readiness",
        "/api/tasks",
        "/api/tasks/{task_id}",
        "/api/tasks/{task_id}/run",
        "/api/tasks/{task_id}/cancel",
        "/api/missions",
        "/api/missions/{mission_id}",
        "/api/missions/{mission_id}/plan",
        "/api/missions/{mission_id}/review",
        "/api/missions/{mission_id}/control/{action}",
        "/api/approvals",
        "/api/approvals/{approval_id}/decision",
        "/api/timeline",
    )
    for route in list(app.routes):
        if isinstance(route, APIRoute) and route.path in supported:
            app.add_api_route(
                route.path.replace("/api/", "/api/v1/", 1),
                route.endpoint,
                methods=list(route.methods),
                dependencies=route.dependencies,
                status_code=route.status_code,
                response_model=route.response_model,
                name="v1_" + route.name,
            )

    @app.get("/api/v1/tasks/{task_id}/debug", dependencies=[Depends(auth)])
    @app.get("/api/v1/tasks/{task_id}/replay", dependencies=[Depends(auth)])
    def recorded(task_id: str, mode: Literal["recorded"] = "recorded", offset: int = 0, limit: int = 100):
        if offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, "Use a nonnegative offset and a limit from 1 to 100")
        return replay(db, registry.credentials, task_id, offset, limit)

    @app.get("/api/v1/health", dependencies=[Depends(auth)])
    def health():
        from .model_router import ModelRouter
        from fastapi.responses import JSONResponse

        db.one("SELECT 1 AS ok")
        if db.distributed:
            from .artifacts import ObjectStore

            ObjectStore(app.state.settings).health()
        worker = worker_readiness(db)
        model = ModelRouter(app.state.settings, registry.credentials, db).readiness("planning")["available"]
        ready = worker["online"] and model
        return JSONResponse(
            {
                "api_version": "1",
                "ready": ready,
                "database": True,
                "worker": worker,
                "model_configured": model,
                "action": None
                if ready
                else "Start a worker and configure the planning model; inspect /api/v1/readiness",
            },
            status_code=200 if ready else 503,
        )

    @app.get("/api/v1/openapi.json", dependencies=[Depends(auth)], include_in_schema=False)
    def schema():
        return get_openapi(
            title="Agent4Good developer API",
            version="1.0.0",
            routes=[r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/v1/")],
        )
