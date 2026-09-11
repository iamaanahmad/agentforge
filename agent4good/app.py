import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog import AGENTS
from .config import Settings
from .db import Database, now, uid
from .tools import ToolRegistry


class Login(BaseModel):
    password: str = Field(max_length=512)


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=20000)
    agent: str = "strategist"
    start: bool = False


class NoteInput(BaseModel):
    content: str = Field(max_length=20000)


class DecisionInput(BaseModel):
    decision: Literal["approve", "reject"]


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    goal: str = Field(max_length=4000)
    autonomy: Literal["manual", "supervised", "autonomous"]


class ScheduleInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=20000)
    agent: str = "strategist"
    interval_minutes: int = Field(1440, ge=15, le=525600)


class SchedulePatch(BaseModel):
    enabled: bool


def create_app(settings=None):
    settings = settings or Settings()
    db = Database(settings.data_dir / "agent4good.sqlite3")
    registry = ToolRegistry(settings)
    app = FastAPI(title="Agent4Good", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.settings = db, settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    static = Path(__file__).parent / "static"

    def token_hash(token):
        return hmac.new(settings.session_secret.encode(), token.encode(), hashlib.sha256).hexdigest()

    def auth(request: Request):
        token = request.cookies.get("a4g_session", "")
        session = (
            db.one(
                "SELECT * FROM sessions WHERE token_hash=? AND expires>?", (token_hash(token), time.time())
            )
            if token
            else None
        )
        if not session:
            raise HTTPException(401, "Sign in to your workspace")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
                raise HTTPException(403, "Session verification failed. Refresh and try again.")
        return session

    def valid_agent(agent):
        if agent not in {a["id"] for a in AGENTS}:
            raise HTTPException(422, "Choose an available agent")

    def task_or_404(task_id):
        row = db.task(task_id)
        if not row:
            raise HTTPException(404, "Task not found")
        return row

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != settings.public_origin.rstrip("/"):
                return JSONResponse({"detail": "Cross-origin writes are blocked"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Use application/json"}, status_code=415)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 128000:
                    return JSONResponse({"detail": "Request exceeds 128 KB"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/healthz")
    def health():
        db.one("SELECT 1 AS ok")
        return {"status": "ok"}

    @app.post("/api/login")
    def login(payload: Login, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        key = hashlib.sha256(address.encode()).hexdigest()
        ts = time.time()
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
            if row and row["resets"] > ts and row["attempts"] >= 10:
                raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.")
            if not row or row["resets"] <= ts:
                conn.execute("INSERT OR REPLACE INTO rate_limits VALUES (?,1,?)", (key, ts + 900))
            else:
                conn.execute("UPDATE rate_limits SET attempts=attempts+1 WHERE key=?", (key,))
        if not hmac.compare_digest(
            hashlib.sha256(payload.password.encode()).digest(),
            hashlib.sha256(settings.admin_password.encode()).digest(),
        ):
            raise HTTPException(401, "Incorrect password")
        db.execute("DELETE FROM rate_limits WHERE key=?", (key,))
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (token_hash(token), csrf, ts + 43200))
        response.set_cookie(
            "a4g_session",
            token,
            max_age=43200,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        return {"ok": True, "csrf_token": csrf}

    @app.get("/api/session")
    def session(session=Depends(auth)):
        return {"csrf_token": session["csrf"]}

    @app.post("/api/logout")
    def logout(response: Response, session=Depends(auth)):
        db.execute("DELETE FROM sessions WHERE token_hash=?", (session["token_hash"],))
        response.delete_cookie("a4g_session", path="/")
        return {"ok": True}

    def worker_status():
        last = db.settings().get("worker_last_seen")
        online = bool(
            last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 30
        )
        return {"online": online, "last_seen": last}

    @app.get("/api/overview", dependencies=[Depends(auth)])
    def overview():
        counts = {r["status"]: r["n"] for r in db.all("SELECT status,COUNT(*) n FROM tasks GROUP BY status")}
        project = db.settings()
        return {
            "project": {k: project[k] for k in ("name", "goal", "autonomy")},
            "counts": {
                "tasks": sum(counts.values()),
                "running": counts.get("running", 0),
                "approvals": db.one("SELECT COUNT(*) n FROM approvals WHERE status='pending'")["n"],
                "completed": counts.get("done", 0),
            },
            "worker": worker_status(),
            "provider": {"configured": bool(settings.openai_api_key), "model": settings.model},
            "recent_events": db.all("SELECT * FROM events ORDER BY id DESC LIMIT 15"),
        }

    @app.get("/api/readiness", dependencies=[Depends(auth)])
    def readiness():
        return {
            "database": True,
            "worker": worker_status(),
            "model_configured": bool(settings.openai_api_key),
            "secure_cookies": settings.secure_cookies,
            "max_steps": settings.max_steps,
            "max_daily_runs": settings.max_daily_runs,
            "deployment": "single-owner, single-worker",
        }

    @app.get("/api/agents", dependencies=[Depends(auth)])
    def agents():
        return AGENTS

    @app.get("/api/tasks", dependencies=[Depends(auth)])
    def tasks():
        return [
            db.public_task(row) for row in db.all("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 500")
        ]

    @app.post("/api/tasks", status_code=201, dependencies=[Depends(auth)])
    def create_task(payload: TaskInput):
        valid_agent(payload.agent)
        if not payload.title.strip() or not payload.prompt.strip():
            raise HTTPException(422, "Title and instructions cannot be blank")
        if payload.start and not settings.openai_api_key:
            raise HTTPException(409, "Configure OpenAI before running a task. You can still save a draft.")
        task_id = db.create_task(payload.title.strip(), payload.prompt.strip(), payload.agent, payload.start)
        return db.public_task(db.task(task_id))

    @app.get("/api/tasks/{task_id}", dependencies=[Depends(auth)])
    def get_task(task_id: str):
        task = db.public_task(task_or_404(task_id))
        return {
            **task,
            "events": db.all("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,)),
            "approvals": db.approval_list(task_id),
            "artifacts": db.all("SELECT id,name,created_at FROM artifacts WHERE task_id=?", (task_id,)),
        }

    @app.post("/api/tasks/{task_id}/run", dependencies=[Depends(auth)])
    def run_task(task_id: str):
        task_or_404(task_id)
        if not settings.openai_api_key:
            raise HTTPException(409, "Configure A4G_OPENAI_API_KEY on the server before running agents")
        if not db.execute(
            "UPDATE tasks SET status='queued',updated_at=? WHERE id=? AND status='draft'", (now(), task_id)
        ):
            raise HTTPException(409, "Only a draft can be started. Create a new task for follow-up work.")
        db.event(task_id, "queued", "Owner started this task")
        return db.public_task(db.task(task_id))

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(auth)])
    def cancel_task(task_id: str):
        task_or_404(task_id)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "UPDATE tasks SET status='cancelled',updated_at=? WHERE id=? AND status IN ('draft','queued','running','waiting_approval')",
                (now(), task_id),
            ).rowcount:
                raise HTTPException(409, "This task is already closed")
            conn.execute(
                "UPDATE approvals SET status='rejected',decided_at=? WHERE task_id=? AND status='pending'",
                (now(), task_id),
            )
        db.event(
            task_id,
            "cancelled",
            "Owner stopped this task. An external request already in flight may still finish.",
        )
        return db.public_task(db.task(task_id))

    @app.get("/api/approvals", dependencies=[Depends(auth)])
    def approvals():
        return db.approval_list()

    @app.post("/api/approvals/{approval_id}/decision", dependencies=[Depends(auth)])
    def decision(approval_id: str, payload: DecisionInput):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            approval = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not approval:
                raise HTTPException(404, "Decision not found")
            task = conn.execute("SELECT status FROM tasks WHERE id=?", (approval["task_id"],)).fetchone()
            if approval["status"] != "pending" or task["status"] != "waiting_approval":
                raise HTTPException(409, "This decision is no longer pending")
            approved = payload.decision == "approve"
            conn.execute(
                "UPDATE approvals SET status=?,decided_at=? WHERE id=?",
                ("approved" if approved else "rejected", now(), approval_id),
            )
            conn.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE id=?",
                ("queued" if approved else "cancelled", now(), approval["task_id"]),
            )
        db.event(
            approval["task_id"],
            "approved" if approved else "rejected",
            "Owner " + payload.decision + "d exact action: " + approval["tool"],
        )
        return next(row for row in db.approval_list(approval["task_id"]) if row["id"] == approval_id)

    @app.get("/api/memory", dependencies=[Depends(auth)])
    def memory():
        return db.all("SELECT * FROM memory ORDER BY key")

    @app.put("/api/memory/{key}", dependencies=[Depends(auth)])
    def save_memory(key: str, payload: NoteInput):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key):
            raise HTTPException(422, "Use letters, numbers, underscores and hyphens for the note key")
        db.execute(
            "INSERT INTO memory VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET content=excluded.content,updated_at=excluded.updated_at",
            (key, payload.content, now()),
        )
        db.event(None, "memory_updated", f"Owner updated memory: {key}")
        return db.one("SELECT * FROM memory WHERE key=?", (key,))

    @app.get("/api/schedules", dependencies=[Depends(auth)])
    def schedules():
        return [
            {**r, "enabled": bool(r["enabled"])}
            for r in db.all("SELECT * FROM schedules ORDER BY created_at DESC")
        ]

    @app.post("/api/schedules", status_code=201, dependencies=[Depends(auth)])
    def add_schedule(payload: ScheduleInput):
        valid_agent(payload.agent)
        schedule_id = uid("schedule")
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=payload.interval_minutes)).isoformat()
        db.execute(
            "INSERT INTO schedules VALUES (?,?,?,?,?,1,?,?)",
            (
                schedule_id,
                payload.name,
                payload.prompt,
                payload.agent,
                payload.interval_minutes,
                next_run,
                now(),
            ),
        )
        db.event(None, "schedule_created", f"Recurring task configured: {payload.name}")
        return db.one("SELECT * FROM schedules WHERE id=?", (schedule_id,))

    @app.patch("/api/schedules/{schedule_id}", dependencies=[Depends(auth)])
    def patch_schedule(schedule_id: str, payload: SchedulePatch):
        if not db.execute("UPDATE schedules SET enabled=? WHERE id=?", (int(payload.enabled), schedule_id)):
            raise HTTPException(404, "Schedule not found")
        db.event(None, "schedule_updated", "Schedule resumed" if payload.enabled else "Schedule paused")
        return db.one("SELECT * FROM schedules WHERE id=?", (schedule_id,))

    @app.get("/api/integrations", dependencies=[Depends(auth)])
    def integrations():
        return registry.integrations()

    @app.get("/api/settings", dependencies=[Depends(auth)])
    def project():
        values = db.settings()
        return {k: values[k] for k in ("name", "goal", "autonomy")}

    @app.patch("/api/settings", dependencies=[Depends(auth)])
    def update_project(payload: ProjectInput):
        with db.connect() as conn:
            for key, value in payload.model_dump().items():
                conn.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
        db.event(None, "settings_updated", f"Owner set autonomy to {payload.autonomy}")
        return payload

    @app.get("/api/events", dependencies=[Depends(auth)])
    def events():
        return db.all("SELECT * FROM events ORDER BY id DESC LIMIT 200")

    @app.get("/api/artifacts/{artifact_id}", dependencies=[Depends(auth)])
    def artifact(artifact_id: str):
        row = db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if not row:
            raise HTTPException(404, "Artifact not found")
        # Always attachment + plain text: generated HTML must never execute on the application origin.
        return Response(
            row["content"],
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{row["name"]}"'},
        )

    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
