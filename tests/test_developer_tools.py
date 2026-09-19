"""Owner SDK/API and offline recovery contract; no external providers."""

import fcntl
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from agent4good.sdk import Client, APIError
from agent4good import maintenance
from agent4good.db import Database, now
from agent4good.tools import ToolRegistry
from test_missions import spec


def sdk(client, settings):
    return Client("http://localhost", transport=client._transport)


def test_v1_sdk_mission_and_schema(client, settings):
    with sdk(client, settings) as api:
        with pytest.raises(APIError) as denied:
            api.create_mission(spec().model_dump())
        assert denied.value.status == 401
        assert api.login(settings.admin_password) == {"authenticated": True}
        mission = api.create_mission(spec().model_dump())
        assert api.mission(mission["id"])["status"] == "draft"
        assert api.cancel_mission(mission["id"])["status"] == "cancelled"
        assert api.approvals() == []
        schema = api.request("GET", "/openapi.json")
        assert "/api/v1/approvals/{approval_id}/decision" in schema["paths"]
        assert "/api/v1/missions" in schema["paths"]
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM sessions")["n"] == 0


def test_v1_auth_csrf_and_invalid_modes(client, settings):
    assert client.get("/api/v1/openapi.json").status_code == 401
    login = client.post("/api/v1/login", json={"password": settings.admin_password})
    assert login.status_code == 200
    assert client.post("/api/v1/missions", json=spec().model_dump()).status_code == 403
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert client.get("/api/v1/tasks/missing/replay?mode=execute").status_code == 422
    assert client.get("/api/v1/tasks/missing/replay").status_code == 404
    assert client.post("/api/v1/tasks/missing/replay", json={}).status_code == 405


def test_replay_is_read_only_and_redacts(app, owner, settings, monkeypatch):
    db = app.state.db
    task = db.create_task("Debug", "Inspect only", "strategist")
    db.execute(
        "INSERT INTO executions(task_id,started_at,final_text) VALUES (?,?,?)",
        (task, now(), settings.admin_password),
    )
    for call, status in [("a", "done"), ("b", "started")]:
        db.execute(
            "INSERT INTO tool_runs(task_id,call_id,tool,arguments,status,result) VALUES (?,?,?,?,?,?)",
            (task, call, "email_send", "{}", status, settings.session_secret),
        )
        db.execute(
            "INSERT INTO plan_steps(task_id,action_id,action_key,fingerprint,tool,revision,status,observation) VALUES (?,?,?,?,?,?,?,?)",
            (task, call, call, call, "email_send", 1, status, settings.admin_password),
        )
    # SQL snapshot comparison catches hidden writes, beyond merely counting mock invocations.
    with db.connect() as conn:
        before = "\n".join(conn.iterdump())

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay invoked tools")

    monkeypatch.setattr(ToolRegistry, "execute", forbidden)
    first = owner.get(f"/api/v1/tasks/{task}/replay?limit=1").json()
    second = owner.get(f"/api/v1/tasks/{task}/replay?limit=1&offset=1").json()
    assert first["mode"] == "recorded" and first["external_effects"] is False
    assert first["next_offset"] == 1 and second["next_offset"] is None
    assert second["receipts"][0]["status"] == "started"
    text = json.dumps([first, second])
    assert settings.admin_password not in text and settings.session_secret not in text
    with db.connect() as conn:
        assert "\n".join(conn.iterdump()) == before
    db.execute("UPDATE tasks SET owner_id='someone-else' WHERE id=?", (task,))
    assert owner.get(f"/api/v1/tasks/{task}/debug").status_code == 404


def test_replay_bounds_and_missing_records(app, owner):
    task = app.state.db.create_task("No execution", "Draft only", "strategist")
    assert owner.get(f"/api/v1/tasks/{task}/replay?limit=101").status_code == 422
    assert owner.get(f"/api/v1/tasks/{task}/replay?offset=-1").status_code == 422
    doc = owner.get(f"/api/v1/tasks/{task}/debug").json()
    assert doc["execution"] is None and doc["receipts"] == []


def test_readiness_tracks_worker_and_provider(app, owner):
    db = app.state.db
    doc = owner.get("/api/v1/health")
    assert doc.status_code == 503 and not doc.json()["worker"]["online"]
    for value in ("invalid", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()):
        db.execute("INSERT OR REPLACE INTO settings VALUES ('worker_last_seen',?)", (value,))
        assert not owner.get("/api/v1/health").json()["worker"]["online"]
    db.execute("INSERT OR REPLACE INTO settings VALUES ('worker_last_seen',?)", (now(),))
    doc = owner.get("/api/v1/health")
    assert doc.status_code == 503 and doc.json()["worker"]["online"]
    assert not doc.json()["model_configured"]


def test_backup_migration_restore_preserves_receipts(settings, tmp_path):
    maintenance.migrate(settings)
    db = Database(settings.data_dir / "agent4good.sqlite3")
    task = db.create_task("Preserve", "Saved task", "strategist")
    db.execute(
        "INSERT INTO tool_runs(task_id,call_id,tool,arguments,status,result) VALUES (?,?,?,?,?,?)",
        (task, "accepted", "email_send", "{}", "done", "receipt"),
    )
    db.execute("INSERT INTO sessions VALUES (?,?,?)", ("hash", "csrf", 9999999999))
    db.execute("INSERT INTO settings VALUES ('worker_last_seen',?)", (now(),))
    db.execute("PRAGMA user_version=10")
    backup = tmp_path / "before.sqlite3"
    assert maintenance.migrate(settings, backup)["schema_version"] == 12
    assert maintenance.sqlite_info(backup)["schema_version"] == 10
    target = settings.model_copy(update={"data_dir": tmp_path / "restored"})
    maintenance.restore_sqlite(target, backup)
    restored = Database(target.data_dir / "agent4good.sqlite3")
    assert restored.task(task)["prompt"] == "Saved task"
    assert restored.one("SELECT result FROM tool_runs")["result"] == "receipt"
    assert restored.one("SELECT COUNT(*) AS n FROM sessions")["n"] == 0
    assert not restored.one("SELECT * FROM settings WHERE key='worker_last_seen'")
    with pytest.raises(FileExistsError):
        maintenance.restore_sqlite(target, backup)
    assert backup.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        maintenance.backup_sqlite(db.path, backup)


def test_future_schema_and_running_worker_refused(settings, tmp_path):
    maintenance.migrate(settings)
    path = settings.data_dir / "agent4good.sqlite3"
    with (settings.data_dir / "worker.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(maintenance.MaintenanceError, match="Stop the worker"):
            maintenance.migrate(settings, tmp_path / "should-not-exist")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="newer"):
        Database(path)
    with pytest.raises(maintenance.MaintenanceError, match="newer"):
        maintenance.backup_sqlite(path, tmp_path / "future")
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 999


def test_init_private_and_configuration_redaction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    maintenance.init_env(".env")
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert maintenance.configuration()[1]["valid"]
    with pytest.raises(FileExistsError):
        maintenance.init_env(".env")
    secret = "private-session-leak-marker"
    monkeypatch.setenv("A4G_ADMIN_PASSWORD", secret)
    monkeypatch.setenv("A4G_MAX_STEPS", secret)
    _, result = maintenance.configuration()
    assert result["valid"] is False and secret not in json.dumps(result)


def test_sdk_refuses_remote_plaintext_and_errors_hide_body():
    import httpx

    with pytest.raises(ValueError, match="HTTPS"):
        Client("http://example.com")
    with pytest.raises(ValueError):
        Client("https://user:secret@example.com")

    def response(request):
        return httpx.Response(500, json={"detail": "secret-server-message"})

    with Client("https://example.com", transport=httpx.MockTransport(response)) as api:
        with pytest.raises(APIError) as error:
            api.login("secret-owner-password")
        assert "secret" not in str(error.value)


def test_sdk_exact_approval_uses_existing_policy(client, app, settings):
    from agent4good.engine import Engine
    from test_engine import FakeProvider, answer, call

    settings.openai_api_key = "test-not-live"
    db = app.state.db
    task = db.create_task("Approval SDK", "Save a fact", "strategist", start=True)
    engine = Engine(
        db,
        settings,
        FakeProvider(answer(calls=[call("memory_write", {"key": "sdk", "content": "test fact"})])),
    )
    assert engine.claim() == task
    engine.run(task)
    with sdk(client, settings) as api:
        api.login(settings.admin_password)
        approval = api.approvals()[0]
        assert approval["status"] == "pending"
        assert api.decide(approval["id"], "approve")["status"] == "approved"
        with pytest.raises(APIError) as duplicate:
            api.decide(approval["id"], "approve")
        assert duplicate.value.status == 409
    assert db.one("SELECT COUNT(*) AS n FROM memory")["n"] == 0
    assert db.task(task)["status"] == "queued"
