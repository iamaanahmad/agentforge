import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from .credentials import CredentialError
from .webhooks import authenticate_webhook
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .memory import MemoryInput, MemoryStore, MemoryError
from .catalog import AGENTS
from . import missions, scheduling
from .quality import QualityInput
from .learning import LearningStore, Correction, verified_document
from .scheduling import ScheduleInput
from .config import Settings
from .model_config import WorkType, task_work
from .model_router import ModelRouter
from .db import Database, now
from .policy import PolicyDocument, PolicyError
from .tools import ToolRegistry


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class MissionReview(StrictInput):
    criterion_id: str = Field(min_length=1, max_length=40)
    evidence: str = Field(min_length=1, max_length=4000)
    accepted: bool


class Login(StrictInput):
    password: str = Field(max_length=512)


class TaskInput(StrictInput):
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=20000)
    agent: str = "strategist"
    start: bool = False
    work_type: WorkType | None = None
    quality: QualityInput | None = None


class WebhookTask(StrictInput):
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=20000)
    agent: str = "strategist"


class ChatMessage(StrictInput):
    content: str = Field(min_length=1, max_length=10000)
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    mode: Literal["ask", "work"] = "ask"


class ChatAnswer(StrictInput):
    content: str = Field(min_length=1, max_length=10000)


class NoteInput(StrictInput):
    content: str = Field(max_length=20000)


class DecisionInput(StrictInput):
    decision: Literal["approve", "reject"]


class PolicyInput(StrictInput):
    expected_revision: int = Field(ge=1)
    document: PolicyDocument


class ProjectInput(StrictInput):
    name: str = Field(min_length=1, max_length=80)
    goal: str = Field(max_length=4000)
    autonomy: Literal["manual", "supervised", "autonomous"]


class ScheduleEvent(StrictInput):
    event_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$")


class SchedulePatch(StrictInput):
    enabled: bool


def create_app(settings=None):
    settings = settings or Settings()
    db = Database.from_settings(settings)
    registry = ToolRegistry(settings, db)
    app = FastAPI(title="Agent4Good", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.settings = db, settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    static = Path(__file__).parent / "static"

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Invalid request fields or values"}, status_code=422)

    import psycopg
    from botocore.exceptions import BotoCoreError, ClientError
    from .postgres import QueueFull

    async def storage_failure(request, exc):
        return JSONResponse(
            {"detail": "Storage unavailable; retry after the service recovers"}, status_code=503
        )

    for error_type in (psycopg.Error, BotoCoreError, ClientError):
        app.add_exception_handler(error_type, storage_failure)

    @app.exception_handler(scheduling.ScheduleError)
    async def schedule_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(MemoryError)
    async def memory_conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(QueueFull)
    async def queue_full(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(CredentialError)
    async def credential_failure(request, exc):
        return JSONResponse({"detail": "Credential access unavailable"}, status_code=503)

    def token_hash(token):
        return hmac.new(
            settings.session_secret.encode(),
            (settings.tenant_id + "\0" + settings.environment + "\0" + token).encode(),
            hashlib.sha256,
        ).hexdigest()

    async def auth(request: Request):
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
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            body = (await request.body()).decode("utf-8", errors="replace")
            if registry.credentials.redact(body) != body:
                raise HTTPException(400, "Credentials do not belong in request content")
        return session

    def valid_agent(agent):
        if agent not in {a["id"] for a in AGENTS}:
            raise HTTPException(422, "Choose an available agent")

    def task_or_404(task_id):
        row = db.task(task_id)
        if not row or row["owner_id"] != "owner":
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
        if db.distributed:
            from .artifacts import ObjectStore

            ObjectStore(settings).health()
        return {"status": "ok"}

    @app.get("/api/infrastructure", dependencies=[Depends(auth)])
    def infrastructure():
        return {
            "backend": "postgresql" if db.distributed else "sqlite",
            "metrics": db.metrics() if db.distributed else {},
        }

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

    @app.post("/api/webhooks/tasks", status_code=201)
    async def webhook_task(request: Request):
        raw = await request.body()
        address = request.client.host if request.client else "unknown"
        rate_key = "webhook:" + hashlib.sha256(address.encode()).hexdigest()
        ts = time.time()
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rate = conn.execute("SELECT * FROM rate_limits WHERE key=?", (rate_key,)).fetchone()
            if rate and rate["resets"] > ts and rate["attempts"] >= 60:
                raise HTTPException(429, "Webhook rate limit reached")
            if not rate or rate["resets"] <= ts:
                conn.execute("INSERT OR REPLACE INTO rate_limits VALUES (?,1,?)", (rate_key, ts + 60))
            else:
                conn.execute("UPDATE rate_limits SET attempts=attempts+1 WHERE key=?", (rate_key,))
        delivery_id = authenticate_webhook(registry.credentials, request.headers, raw)
        try:
            payload = WebhookTask.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422, "Invalid webhook task") from None
        valid_agent(payload.agent)
        if registry.credentials.redact(payload.model_dump()) != payload.model_dump():
            raise HTTPException(400, "Credentials do not belong in request content")
        if not payload.title.strip() or not payload.prompt.strip():
            raise HTTPException(422, "Title and instructions cannot be blank")
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM webhook_receipts WHERE delivery_id=?", (delivery_id,)).fetchone():
                raise HTTPException(409, "Webhook delivery already received")
            # Even a signed event cannot start paid model work or approve an action.
            task_id = db.create_task(payload.title, payload.prompt, payload.agent, False, conn)
            conn.execute("INSERT INTO webhook_receipts VALUES (?,?,?)", (delivery_id, time.time(), task_id))
            conn.execute(
                "INSERT INTO events(task_id,kind,message,created_at) VALUES (?,?,?,?)",
                (task_id, "webhook_received", "Signed webhook created a draft", now()),
            )
        return {"task_id": task_id, "status": "draft"}

    @app.get("/api/session")
    def session(session=Depends(auth)):
        return {"csrf_token": session["csrf"]}

    @app.post("/api/logout")
    def logout(response: Response, session=Depends(auth)):
        db.execute("DELETE FROM sessions WHERE token_hash=?", (session["token_hash"],))
        response.delete_cookie("a4g_session", path="/")
        return {"ok": True}

    def worker_status():
        from .developer import worker_readiness

        return worker_readiness(db)

    @app.get("/api/overview", dependencies=[Depends(auth)])
    def overview():
        counts = {
            r["status"]: r["n"]
            for r in db.all("SELECT status,COUNT(*) n FROM tasks WHERE owner_id='owner' GROUP BY status")
        }
        project = db.settings()
        return {
            "project": {k: project[k] for k in ("name", "goal", "autonomy")},
            "counts": {
                "tasks": sum(counts.values()),
                "running": counts.get("running", 0),
                "approvals": db.one(
                    "SELECT COUNT(*) n FROM approvals WHERE status='pending' AND task_id IN (SELECT id FROM tasks WHERE owner_id='owner')"
                )["n"],
                "completed": counts.get("done", 0),
            },
            "worker": worker_status(),
            "provider": ModelRouter(settings, registry.credentials, db).readiness("planning"),
            "recent_events": db.all(
                "SELECT * FROM events WHERE task_id IS NULL OR task_id IN (SELECT id FROM tasks WHERE owner_id='owner') ORDER BY id DESC LIMIT 15"
            ),
        }

    @app.get("/api/readiness", dependencies=[Depends(auth)])
    def readiness():
        return {
            "database": True,
            "worker": worker_status(),
            "model_configured": ModelRouter(settings, registry.credentials, db).readiness("planning")[
                "available"
            ],
            "model_routes": ModelRouter(settings, registry.credentials, db).routes(),
            "secure_cookies": settings.secure_cookies,
            "max_steps": settings.max_steps,
            "max_daily_runs": settings.max_daily_runs,
            "deployment": "single-owner, single-worker",
            "credential_storage": "encrypted-vault" if settings.credential_key_file else "legacy-environment",
            "tenant": settings.tenant_id,
            "environment": settings.environment,
            "multi_user_available": False,
        }

    @app.get("/api/agents", dependencies=[Depends(auth)])
    def agents():
        return AGENTS

    def mission_safe(payload):
        if registry.credentials.redact(payload) != payload:
            raise HTTPException(400, "Credentials do not belong in mission content")

    @app.get("/api/missions", dependencies=[Depends(auth)])
    def list_missions():
        with db.connect() as conn:
            return [
                missions.detail(conn, r["id"])
                for r in conn.execute("SELECT id FROM missions ORDER BY created_at DESC LIMIT 100").fetchall()
            ]

    @app.post("/api/missions", status_code=201, dependencies=[Depends(auth)])
    def create_mission(payload: missions.MissionInput):
        mission_safe(payload.model_dump())
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            mission_id = missions.create(conn, payload)
            return missions.detail(conn, mission_id)

    @app.get("/api/missions/{mission_id}", dependencies=[Depends(auth)])
    def get_mission(mission_id: str):
        with db.connect() as conn:
            try:
                return missions.detail(conn, mission_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc

    @app.post("/api/missions/{mission_id}/plan", dependencies=[Depends(auth)])
    def plan_mission(mission_id: str, payload: missions.Plan):
        mission_safe(payload.model_dump())
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                missions.apply_plan(conn, db, mission_id, payload)
                missions.settle(conn)
                return missions.detail(conn, mission_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.post("/api/missions/{mission_id}/review", dependencies=[Depends(auth)])
    def review_mission(mission_id: str, payload: MissionReview):
        mission_safe(payload.model_dump())
        if not payload.evidence.strip():
            raise HTTPException(422, "Give the evidence behind your decision")
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            m = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            if not m or m["status"] in missions.CLOSED:
                raise HTTPException(409, "Mission is closed or missing")
            criteria = json.loads(m["spec"])["criteria"]
            if not any(c["id"] == payload.criterion_id and c["kind"] == "owner" for c in criteria):
                raise HTTPException(409, "Only owner-review criteria accept owner evidence")
            conn.execute(
                "INSERT INTO mission_reviews VALUES (?,?,?,?,?) ON CONFLICT(mission_id,criterion_id) DO UPDATE SET evidence=excluded.evidence,accepted=excluded.accepted,created_at=excluded.created_at",
                (mission_id, payload.criterion_id, payload.evidence, int(payload.accepted), now()),
            )
            return missions.detail(conn, mission_id)

    @app.post("/api/missions/{mission_id}/control/{action}", dependencies=[Depends(auth)])
    def control_mission(mission_id: str, action: str):
        if (
            action in {"start", "resume", "replan"}
            and not ModelRouter(settings, registry.credentials, db).readiness("planning")["available"]
        ):
            raise HTTPException(409, "Configure the planning model before starting missions")
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                missions.control(conn, db, mission_id, action)
                return missions.detail(conn, mission_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.get("/api/tasks", dependencies=[Depends(auth)])
    def tasks():
        return [
            db.public_task(row)
            for row in db.all("SELECT * FROM tasks WHERE owner_id='owner' ORDER BY created_at DESC LIMIT 500")
        ]

    @app.post("/api/tasks", status_code=201, dependencies=[Depends(auth)])
    def create_task(payload: TaskInput):
        valid_agent(payload.agent)
        if registry.credentials.redact(payload.model_dump()) != payload.model_dump():
            raise HTTPException(400, "Credentials do not belong in request content")
        if not payload.title.strip() or not payload.prompt.strip():
            raise HTTPException(422, "Title and instructions cannot be blank")
        route = ModelRouter(settings, registry.credentials, db).readiness(task_work(payload.model_dump()))
        if payload.start and not route["available"]:
            raise HTTPException(
                409,
                "Configure the selected model provider and tool support before running. You can still save a draft.",
            )
        task_id = db.create_task(
            payload.title.strip(),
            payload.prompt.strip(),
            payload.agent,
            payload.start,
            work_type=payload.work_type,
            quality=payload.quality.model_dump() if payload.quality else None,
        )
        return db.public_task(db.task(task_id))

    @app.get("/api/chats", dependencies=[Depends(auth)])
    def chats():
        return db.all("""SELECT t.id,t.title,
            COALESCE((SELECT r.status FROM conversation_turns c JOIN tasks r ON r.id=c.task_id
                WHERE c.root_id=t.id ORDER BY c.created_at DESC,c.task_id DESC LIMIT 1),t.status) AS status,
            t.updated_at,
            COALESCE((SELECT MAX(c.created_at) FROM conversation_turns c WHERE c.root_id=t.id),t.created_at) AS last_message_at
            FROM tasks t WHERE t.owner_id='owner'
            AND NOT EXISTS (SELECT 1 FROM conversation_turns c WHERE c.task_id=t.id)
            ORDER BY last_message_at DESC,t.id LIMIT 500""")

    @app.get("/api/tasks/{task_id}/chat", dependencies=[Depends(auth)])
    def chat_history(task_id: str):
        from .conversations import transcript

        task_or_404(task_id)
        return registry.credentials.redact(transcript(db, task_id))

    @app.post("/api/tasks/{task_id}/chat", status_code=201, dependencies=[Depends(auth)])
    def chat_message(task_id: str, payload: ChatMessage):
        from .conversations import follow_up, root_task

        task_or_404(task_id)
        if registry.credentials.redact(payload.content) != payload.content:
            raise HTTPException(400, "Credentials do not belong in chat")
        with db.connect() as conn:
            root = root_task(conn, task_id)
        route = ModelRouter(settings, registry.credentials, db).readiness(task_work(root))
        if not route["available"]:
            raise HTTPException(409, "Configure this task's model in Connections before sending a message")
        try:
            tid = follow_up(db, task_id, payload.request_id, payload.content, payload.mode)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"task_id": tid, "root_id": root["id"]}

    @app.post("/api/tasks/{task_id}/questions/{question_id}/answer", dependencies=[Depends(auth)])
    def answer_question(task_id: str, question_id: str, payload: ChatAnswer):
        from .conversations import answer

        task_or_404(task_id)
        if registry.credentials.redact(payload.content) != payload.content:
            raise HTTPException(400, "Credentials do not belong in chat")
        try:
            answer(db, task_id, question_id, payload.content)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"status": "answered", "task_id": task_id}

    @app.get("/api/tasks/{task_id}", dependencies=[Depends(auth)])
    def get_task(task_id: str):
        task = db.public_task(task_or_404(task_id))
        return {
            **task,
            "events": db.all("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,)),
            "approvals": db.approval_list(task_id),
            "artifacts": db.all("SELECT id,name,created_at FROM artifacts WHERE task_id=?", (task_id,)),
            "workers": db.all(
                "SELECT task_id,parent_id,root_id,depth,priority FROM worker_nodes WHERE parent_id=? OR task_id=?",
                (task_id, task_id),
            ),
            "quality_contract": db.one("SELECT * FROM quality_contracts WHERE task_id=?", (task_id,)),
            "quality_rounds": db.all(
                "SELECT id,attempt,digest,status,created_at FROM quality_rounds WHERE task_id=? ORDER BY attempt",
                (task_id,),
            ),
            "quality_reviews": db.all(
                "SELECT q.* FROM quality_reviews q JOIN quality_rounds r ON r.id=q.round_id WHERE r.task_id=? ORDER BY r.attempt,q.stage",
                (task_id,),
            ),
            "execution": db.one("SELECT * FROM executions WHERE task_id=?", (task_id,)),
            "model_route": db.one("SELECT * FROM model_routes WHERE task_id=?", (task_id,)),
            "model_calls": db.all(
                "SELECT * FROM model_calls WHERE task_id=? ORDER BY created_at", (task_id,)
            ),
            "plan_steps": db.all(
                "SELECT * FROM plan_steps WHERE task_id=? ORDER BY revision,action_id", (task_id,)
            ),
        }

    @app.post("/api/tasks/{task_id}/run", dependencies=[Depends(auth)])
    def run_task(task_id: str):
        task = task_or_404(task_id)
        with db.connect() as conn:
            if missions.mission_for(conn, task_id):
                raise HTTPException(409, "Start mission work from its mission controls")
        if not ModelRouter(settings, registry.credentials, db).readiness(task_work(task))["available"]:
            raise HTTPException(
                409, "Configure the selected model provider and tool support before running agents"
            )
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
                "UPDATE tasks SET status='cancelled',updated_at=? WHERE id=? AND status IN ('draft','queued','running','waiting_approval','waiting_children','waiting_input')",
                (now(), task_id),
            ).rowcount:
                raise HTTPException(409, "This task is already closed")
            conn.execute(
                "UPDATE approvals SET status='rejected',decided_at=? WHERE task_id=? AND status='pending'",
                (now(), task_id),
            )
            from .coordination import settle

            settle(conn)
        db.event(
            task_id,
            "cancelled",
            "Owner stopped this task. An external request already in flight may still finish.",
        )
        return db.public_task(db.task(task_id))

    @app.get("/api/policy", dependencies=[Depends(auth)])
    def get_policy():
        with db.connect() as conn:
            revision, document = ToolRegistry(settings, db).policy.current(conn)
        return {"revision": revision, "document": document.model_dump()}

    @app.put("/api/policy", dependencies=[Depends(auth)])
    def put_policy(payload: PolicyInput):
        try:
            revision = ToolRegistry(settings, db).policy.replace(
                payload.document.model_dump(), payload.expected_revision
            )
        except PolicyError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"revision": revision, "document": payload.document.model_dump()}

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
            task = conn.execute(
                "SELECT * FROM tasks WHERE id=? AND owner_id='owner'", (approval["task_id"],)
            ).fetchone()
            if not task:
                raise HTTPException(404, "Decision not found")
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
            from .coordination import settle

            settle(conn)
            from .timeline import emit

            emit(
                conn,
                approval["task_id"],
                "approved" if approved else "rejected",
                "Owner decided exact action",
                approval_id=approval_id,
                step_id=approval["call_id"],
                tool=approval["tool"],
                status="approved" if approved else "rejected",
            )

        return next(row for row in db.approval_list(approval["task_id"]) if row["id"] == approval_id)

    @app.get("/api/memory", dependencies=[Depends(auth)])
    def memory():
        store = MemoryStore(db)
        return [
            note
            for row in db.all("SELECT key FROM memory ORDER BY key")
            if (note := store.read_key(row["key"])).get("found") is not False
        ]

    @app.put("/api/memory/{key}", dependencies=[Depends(auth)])
    def save_memory(key: str, payload: NoteInput):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key):
            raise HTTPException(422, "Use letters, numbers, underscores and hyphens for the note key")
        note = MemoryStore(db).legacy_save(key, registry.credentials.redact(payload.content))
        db.event(None, "memory_updated", f"Owner updated memory: {key}")
        return note

    @app.get("/api/memory-records", dependencies=[Depends(auth)])
    def memory_records(task_id: str | None = None, history: bool = False, offset: int = 0):
        if task_id and (not db.task(task_id) or db.task(task_id)["owner_id"] != "owner"):
            raise HTTPException(404, "Task not found")
        return registry.credentials.redact(
            MemoryStore(db).inspect(task_id=task_id, include_history=history, offset=max(0, offset))
        )

    @app.post("/api/memory-records", dependencies=[Depends(auth)])
    def store_memory(payload: MemoryInput, task_id: str | None = None):
        if task_id and (not db.task(task_id) or db.task(task_id)["owner_id"] != "owner"):
            raise HTTPException(404, "Task not found")
        data = registry.credentials.redact(payload.model_dump())
        doc = MemoryStore(db).save(data, task_id=task_id)
        db.event(task_id, "memory_updated", "Owner saved memory record " + doc["id"])
        return doc

    @app.get("/api/tasks/{task_id}/memory", dependencies=[Depends(auth)])
    def task_memory(task_id: str, query: str = ""):
        task = db.task(task_id)
        if not task or task["owner_id"] != "owner":
            raise HTTPException(404, "Task not found")
        return registry.credentials.redact(
            MemoryStore(db).search(task_id, query or task["prompt"], settings.memory_context_budget)
        )

    @app.get("/api/outcomes", dependencies=[Depends(auth)])
    def outcomes(task_id: str | None = None, offset: int = 0):
        if task_id and (not db.task(task_id) or db.task(task_id)["owner_id"] != "owner"):
            raise HTTPException(404, "Task not found")
        return registry.credentials.redact(LearningStore(db).inspect(task_id, offset))

    @app.get("/api/outcomes/{outcome_id}/evidence/{sha256}", dependencies=[Depends(auth)])
    def outcome_evidence(outcome_id: str, sha256: str):
        from .memory import digest

        row = db.one(
            "SELECT e.document FROM learning_evidence e JOIN learning_outcomes o ON o.id=e.outcome_id JOIN tasks t ON t.id=o.task_id WHERE e.outcome_id=? AND e.sha256=? AND o.owner_id='owner' AND t.owner_id='owner'",
            (outcome_id, sha256),
        )
        if not row or digest(row["document"]) != sha256:
            raise HTTPException(404, "Evidence not found")
        return registry.credentials.redact(json.loads(row["document"]))

    @app.post("/api/outcomes/{outcome_id}/notes", dependencies=[Depends(auth)])
    def outcome_note(outcome_id: str, payload: Correction):
        try:
            return LearningStore(db).correct(outcome_id, registry.credentials.redact(payload.model_dump()))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/tasks/{task_id}/learning", dependencies=[Depends(auth)])
    def learning(task_id: str):
        task = db.task(task_id)
        if not task or task["owner_id"] != "owner":
            raise HTTPException(404, "Task not found")
        records = LearningStore(db).search(task_id, task["prompt"], settings.memory_context_budget)
        uses = [
            verified_document(r)
            for r in db.all("SELECT * FROM learning_uses WHERE task_id=? ORDER BY step", (task_id,))
        ]
        return registry.credentials.redact({**records, "uses": [u for u in uses if u]})

    @app.get("/api/schedules", dependencies=[Depends(auth)])
    def schedules():
        return [
            {
                **r,
                "enabled": bool(r["enabled"]),
                "config": json.loads(r["definition"]) if r["definition"] else {"mode": "interval"},
            }
            for r in db.all(
                "SELECT s.*,d.definition,d.cancelled,d.runs FROM schedules s LEFT JOIN schedule_definitions d ON d.schedule_id=s.id ORDER BY s.created_at DESC"
            )
        ]

    @app.post("/api/schedules", status_code=201, dependencies=[Depends(auth)])
    def add_schedule(payload: ScheduleInput):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            schedule_id = scheduling.create(conn, payload)
        db.event(None, "schedule_created", f"Schedule configured: {payload.name}")
        return db.one("SELECT * FROM schedules WHERE id=?", (schedule_id,))

    @app.patch("/api/schedules/{schedule_id}", dependencies=[Depends(auth)])
    def patch_schedule(schedule_id: str, payload: SchedulePatch):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scheduling.control(conn, schedule_id, enabled=payload.enabled)
        return db.one("SELECT * FROM schedules WHERE id=?", (schedule_id,))

    @app.post("/api/schedules/{schedule_id}/cancel", dependencies=[Depends(auth)])
    def cancel_schedule(schedule_id: str):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scheduling.control(conn, schedule_id, cancel=True)
        return {"cancelled": schedule_id}

    @app.post("/api/schedules/{schedule_id}/events", dependencies=[Depends(auth)])
    def schedule_event(schedule_id: str, payload: ScheduleEvent):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            accepted = scheduling.event(conn, schedule_id, payload.event_id)
        return {"accepted": accepted}

    @app.get("/api/schedules/{schedule_id}", dependencies=[Depends(auth)])
    def schedule_detail(schedule_id: str):
        row = db.one("SELECT * FROM schedules WHERE id=?", (schedule_id,))
        if not row:
            raise HTTPException(404, "Schedule not found")
        return {
            **row,
            "definition": db.one("SELECT * FROM schedule_definitions WHERE schedule_id=?", (schedule_id,)),
            "occurrences": db.all(
                "SELECT * FROM schedule_occurrences WHERE schedule_id=? ORDER BY created_at DESC LIMIT 100",
                (schedule_id,),
            ),
        }

    @app.get("/api/integrations", dependencies=[Depends(auth)])
    def integrations():
        return registry.integrations()

    @app.get("/api/tools", dependencies=[Depends(auth)])
    def tools_catalog():
        return registry.catalog()

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

    @app.get("/api/timeline", dependencies=[Depends(auth)])
    def timeline(
        after: int = 0,
        limit: int = 100,
        task_id: str | None = None,
        agent: str | None = None,
        kind: str | None = None,
        through: int | None = None,
    ):
        from .timeline import read

        if after < 0 or not 1 <= limit <= 500 or (through is not None and through < 0):
            raise HTTPException(422, "Invalid cursor or page size")
        try:
            return read(
                db,
                registry.credentials,
                after=after,
                limit=limit,
                task_id=task_id,
                agent=agent,
                kind=kind,
                through=through,
            )
        except LookupError as exc:
            raise HTTPException(404, "Task not found") from exc

    @app.get("/api/events", dependencies=[Depends(auth)])
    def events():
        return db.all(
            "SELECT * FROM events WHERE task_id IS NULL OR task_id IN (SELECT id FROM tasks WHERE owner_id='owner') ORDER BY id DESC LIMIT 200"
        )

    @app.get("/api/artifacts/{artifact_id}", dependencies=[Depends(auth)])
    def artifact(artifact_id: str, decode_browser: bool = False):
        row = db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if not row:
            raise HTTPException(404, "Artifact not found")
        task_or_404(row["task_id"])
        # Always attachment + plain text: generated HTML must never execute on the application origin.
        from .artifacts import read_artifact

        content = read_artifact(db, settings, row)
        name, media_type = row["name"], "text/plain"
        if decode_browser:
            if not re.fullmatch(r"(?:screenshot-[0-9]+\.png|download-[0-9]+\.bin)\.b64", name):
                raise HTTPException(400, "Not a browser artifact")
            try:
                content = base64.b64decode(content, validate=True)
            except (ValueError, binascii.Error):
                raise HTTPException(400, "Invalid browser artifact") from None
            if len(content) > 500000:
                raise HTTPException(400, "Browser artifact exceeds limit")
            name, media_type = name[:-4], "application/octet-stream"
        return Response(
            content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    from .developer import install_api

    install_api(app, db, registry, auth)

    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/compare/autogpt")
    def autogpt_comparison():
        return FileResponse(static / "compare" / "autogpt.html")

    legal = Path(__file__).parent / "legal"

    @app.get("/terms")
    def terms():
        return FileResponse(legal / "terms.html")

    @app.get("/privacy")
    def privacy():
        return FileResponse(legal / "privacy.html")

    @app.get("/legal.css")
    def legal_styles():
        return FileResponse(legal / "legal.css", media_type="text/css")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
