import time
from concurrent.futures import ThreadPoolExecutor
from agent4good.db import Database, now


def test_private_routes_require_login(client):
    for path in [
        "overview",
        "session",
        "tasks",
        "agents",
        "memory",
        "approvals",
        "settings",
        "integrations",
        "events",
        "readiness",
        "schedules",
    ]:
        assert client.get("/api/" + path).status_code == 401
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/").status_code == 200


def test_origin_csrf_and_logout(owner):
    payload = {"title": "Research", "prompt": "Use supplied evidence"}
    assert (
        owner.post("/api/tasks", json=payload, headers={"Origin": "https://attacker.test"}).status_code == 403
    )
    assert owner.post("/api/tasks", json=payload, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert owner.post("/api/tasks", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert owner.post("/api/tasks", json=payload).status_code == 201
    assert owner.post("/api/logout", json={}).status_code == 200
    assert owner.get("/api/tasks").status_code == 401


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post("/api/login", json={"password": "bad"}).status_code == 401
    assert client.post("/api/login", json={"password": "bad"}).status_code == 429


def test_expired_session_rejected(owner, app):
    app.state.db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert owner.get("/api/tasks").status_code == 401


def test_secure_cookie_headers_and_invalid_host(settings):
    from fastapi.testclient import TestClient
    from agent4good.app import create_app

    settings.secure_cookies = True
    settings.public_origin = "https://testserver"
    with TestClient(create_app(settings), base_url="https://testserver") as c:
        r = c.post("/api/login", json={"password": settings.admin_password})
        for flag in ["HttpOnly", "Secure", "SameSite=strict"]:
            assert flag in r.headers["set-cookie"]
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["strict-transport-security"]
        assert c.get("/", headers={"host": "attacker.test"}).status_code == 400


def test_body_limit(owner):
    assert owner.post("/api/tasks", json={"prompt": "x" * 128001, "title": "Too big"}).status_code == 413


def test_draft_lifecycle_and_missing_provider(owner, app):
    payload = {"title": "  Research  ", "prompt": "Compare supplied data", "agent": "research_analyst"}
    response = owner.post("/api/tasks", json=payload)
    assert response.status_code == 201
    t = response.json()
    assert t["title"] == "Research" and t["status"] == "draft"
    assert "pending" not in t and "items" not in t
    assert owner.post("/api/tasks/" + t["id"] + "/run", json={}).status_code == 409
    assert owner.post("/api/tasks", json={**payload, "start": True}).status_code == 409
    assert owner.get("/api/tasks/" + t["id"]).json()["events"][0]["kind"] == "created"
    assert owner.post("/api/tasks/" + t["id"] + "/cancel", json={}).json()["status"] == "cancelled"
    assert owner.post("/api/tasks/" + t["id"] + "/cancel", json={}).status_code == 409
    assert owner.get("/api/tasks/missing").status_code == 404
    assert owner.post("/api/tasks", json={**payload, "agent": "invented"}).status_code == 422
    assert owner.post("/api/tasks", json={**payload, "title": " "}).status_code == 422
    assert Database(app.state.settings.data_dir / "agent4good.sqlite3").task(t["id"])["status"] == "cancelled"


def test_start_once(owner, app):
    app.state.settings.openai_api_key = "test-key-never-used"
    task = owner.post("/api/tasks", json={"title": "One", "prompt": "Two"}).json()
    assert owner.post("/api/tasks/" + task["id"] + "/run", json={}).status_code == 200
    assert owner.post("/api/tasks/" + task["id"] + "/run", json={}).status_code == 409


def test_memory_settings_schedule(owner):
    assert owner.put("/api/memory/product", json={"content": "Honest product facts"}).status_code == 200
    assert owner.get("/api/memory").json()[0]["content"] == "Honest product facts"
    assert owner.put("/api/memory/bad.key", json={"content": "x"}).status_code == 422
    p = {"name": "My business", "goal": "Ship useful changes", "autonomy": "autonomous"}
    assert owner.patch("/api/settings", json=p).json() == p
    assert owner.get("/api/overview").json()["project"] == p
    s = owner.post(
        "/api/schedules", json={"name": "Review", "prompt": "Read issues", "interval_minutes": 60}
    ).json()
    assert owner.patch("/api/schedules/" + s["id"], json={"enabled": False}).status_code == 200
    assert owner.get("/api/schedules").json()[0]["enabled"] is False
    assert (
        owner.post(
            "/api/schedules", json={"name": "Too fast", "prompt": "No", "interval_minutes": 1}
        ).status_code
        == 422
    )


def test_approval_atomic_once_and_cancel(owner, app):
    db = app.state.db
    task = db.create_task("Email", "Draft a reply", "customer_support_engineer")
    db.execute("UPDATE tasks SET status='waiting_approval' WHERE id=?", (task,))
    db.execute(
        "INSERT INTO approvals(id,task_id,call_id,tool,arguments,created_at) VALUES (?,?,?,?,?,?)",
        ("approval_1", task, "call_1", "send_email", '{"to":"person@example.com"}', now()),
    )
    assert owner.post("/api/approvals/approval_1/decision", json={"decision": "approve"}).status_code == 200
    assert db.task(task)["status"] == "queued"
    assert owner.post("/api/approvals/approval_1/decision", json={"decision": "approve"}).status_code == 409
    assert owner.post("/api/tasks/" + task + "/cancel", json={}).status_code == 200


def test_artifact_is_attachment_not_executable(owner, app):
    db = app.state.db
    task = db.create_task("Report", "Write", "strategist")
    db.execute(
        "INSERT INTO artifacts VALUES (?,?,?,?,?)",
        ("file_1", task, "report.html", "<script>alert(1)</script>", now()),
    )
    r = owner.get("/api/artifacts/file_1")
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-disposition"] == 'attachment; filename="report.html"'
    assert r.headers["x-content-type-options"] == "nosniff"


def test_concurrent_task_creation_is_durable(app):
    db = app.state.db
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda n: db.create_task(str(n), "Work", "strategist"), range(12)))
    assert len(set(ids)) == 12
    assert len(db.all("SELECT * FROM tasks")) == 12
