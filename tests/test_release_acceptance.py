"""Release integration checks. Scripted model output is NOT live provider evidence."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

from fastapi.testclient import TestClient

from agent4good.app import create_app
from agent4good.db import Database
from agent4good.engine import Engine
from test_engine import FakeProvider, answer, call


def login(client, settings):
    response = client.post("/api/login", json={"password": settings.admin_password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def backup(settings, destination):
    # Pass only test configuration. Never inherit provider credentials from the host.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "A4G_DATA_DIR": str(settings.data_dir),
        "A4G_ADMIN_PASSWORD": settings.admin_password,
        "A4G_SESSION_SECRET": settings.session_secret,
        "A4G_SECURE_COOKIES": "false",
        "A4G_PUBLIC_ORIGIN": "http://testserver",
    }
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/backup.py"), str(destination)],
        cwd=settings.data_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_backup_restore_resumes_owner_approval_and_preserves_artifact(settings, tmp_path):
    settings.openai_api_key = "test-key-never-used"
    app = create_app(settings)
    args = {"key": "product", "content": "Tagged release acceptance fact"}
    with TestClient(app) as client:
        login(client, settings)
        response = client.post(
            "/api/tasks",
            json={
                "title": "Tagged release acceptance",
                "prompt": "Save supplied evidence",
                "start": True,
            },
        )
        assert response.status_code == 201
        task = response.json()["id"]
        engine = Engine(app.state.db, settings, FakeProvider(answer(calls=[call("memory_write", args)])))
        assert engine.claim() == task
        engine.run(task)
        assert app.state.db.task(task)["status"] == "waiting_approval"
        assert client.get("/api/memory").json() == []

    snapshot = tmp_path / "snapshot.sqlite3"
    result = backup(settings, snapshot)
    assert result.returncode == 0, result.stderr
    assert snapshot.stat().st_mode & 0o777 == 0o600
    restored_dir = tmp_path / "restored"
    restored_dir.mkdir()
    shutil.copy2(snapshot, restored_dir / "agent4good.sqlite3")
    restored = settings.model_copy(update={"data_dir": restored_dir})
    restored_app = create_app(restored)
    provider = FakeProvider(
        answer(
            calls=[call("artifact_write", {"name": "proof.md", "content": args["content"]}, "artifact_1")]
        ),
        answer("Saved supplied evidence"),
    )
    engine = Engine(restored_app.state.db, restored, provider)
    engine.recover()
    assert engine.claim() is None
    with TestClient(restored_app) as client:
        assert client.get("/api/tasks").status_code == 401
        login(client, restored)
        approval = client.get("/api/approvals").json()[0]
        assert approval["arguments"] == args
        decision_url = "/api/approvals/" + approval["id"] + "/decision"
        assert client.post(decision_url, json={"decision": "approve"}).status_code == 200
        assert client.post(decision_url, json={"decision": "approve"}).status_code == 409
        assert engine.claim() == task
        engine.run(task)
        assert restored_app.state.db.task(task)["status"] == "done"
        artifact = restored_app.state.db.all("SELECT * FROM artifacts")[0]
        assert client.get("/api/artifacts/" + artifact["id"]).text == args["content"]
        assert client.get("/api/memory").json()[0]["content"] == args["content"]
    restarted = Database(restored_dir / "agent4good.sqlite3")
    again = Engine(restarted, restored, FakeProvider())
    again.recover()
    assert again.claim() is None
    assert restarted.task(task)["status"] == "done"
    assert len(restarted.all("SELECT * FROM tool_runs")) == 2
    assert app.state.db.task(task)["status"] == "waiting_approval"


def test_backup_refuses_overwrite_and_missing_source(settings, tmp_path):
    destination = tmp_path / "protected.sqlite3"
    destination.write_bytes(b"keep existing backup")
    assert backup(settings, destination).returncode != 0
    Database(settings.data_dir / "agent4good.sqlite3")
    assert backup(settings, destination).returncode != 0
    assert destination.read_bytes() == b"keep existing backup"


def test_restore_keeps_ambiguous_execution_failed(settings, tmp_path):
    db = Database(settings.data_dir / "agent4good.sqlite3")
    task = db.create_task("Tagged interrupted task", "No external contact", "strategist", True)
    engine = Engine(db, settings, FakeProvider())
    assert engine.claim() == task
    db.execute(
        "INSERT INTO tool_runs(task_id,call_id,tool,arguments,status) VALUES (?,?,?,?,?)",
        (task, "ambiguous", "send_email", "{}", "started"),
    )
    destination = tmp_path / "interrupted.sqlite3"
    result = backup(settings, destination)
    assert result.returncode == 0, result.stderr
    restored = Database(destination)
    provider = FakeProvider()
    recovery = Engine(restored, settings, provider)
    recovery.recover()
    assert recovery.claim() is None
    assert restored.task(task)["status"] == "failed"
    assert restored.all("SELECT * FROM tool_runs")[0]["status"] == "started"
    assert provider.calls == []
