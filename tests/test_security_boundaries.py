import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from agent4good.app import create_app
from agent4good.config import Settings
from agent4good.credentials import CredentialBroker, CredentialError
from agent4good.db import Database, now
from agent4good.engine import Engine
from agent4good.tools import ToolError, ToolRegistry


@pytest.fixture
def vault(settings, tmp_path):
    settings.data_dir = tmp_path / "data"
    settings.credential_key_file = tmp_path / "keyring.json"
    settings.credential_key_file.write_text(
        json.dumps({"active": "one", "keys": {"one": base64.b64encode(os.urandom(32)).decode()}})
    )
    settings.credential_key_file.chmod(0o600)
    db = Database(settings.data_dir / "agent4good.sqlite3")
    return CredentialBroker(settings, db)


def running(broker, agent="strategist"):
    task = broker.db.create_task("Security self-test", "No external contact", agent)
    broker.db.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
    return task


def test_ciphertext_backup_restore_rotation_and_replacement(vault, tmp_path):
    value = "test-secret-unique-not-a-real-key"
    vault.put("openai_api_key", value, ["strategist"])
    task = running(vault)
    assert vault.get("openai_api_key", "model", task) == value
    row = vault.db.one("SELECT * FROM credentials")
    assert value not in repr(row)
    backup = tmp_path / "backup.sqlite3"
    with vault.db.connect() as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    assert value.encode() not in backup.read_bytes()
    document = json.loads(vault.settings.credential_key_file.read_text())
    document["active"] = "two"
    document["keys"]["two"] = base64.b64encode(os.urandom(32)).decode()
    vault.settings.credential_key_file.write_text(json.dumps(document))
    vault.rotate()
    assert vault.db.one("SELECT * FROM credentials")["key_id"] == "two"
    assert vault.get("openai_api_key", "model", task) == value
    restored = CredentialBroker(vault.settings, Database(backup))
    assert restored.get("openai_api_key", "model", task) == value
    vault.put("openai_api_key", "replacement-test-secret", ["strategist"])
    assert vault.get("openai_api_key", "model", task) == "replacement-test-secret"
    assert "test-secret" not in repr(vault.db.all("SELECT * FROM events"))


@pytest.mark.parametrize("field", ["ciphertext", "nonce", "metadata", "key_id", "name"])
def test_tampered_ciphertext_and_scope_fail_closed(vault, field):
    vault.put("openai_api_key", "synthetic-test-secret", ["strategist"])
    values = {
        "ciphertext": b"forged",
        "nonce": os.urandom(12),
        "metadata": "{}",
        "key_id": "unknown",
        "name": "github_token",
    }
    vault.db.execute(f"UPDATE credentials SET {field}=?", (values[field],))
    with pytest.raises(CredentialError):
        vault._decrypt(vault.db.one("SELECT * FROM credentials"))


@pytest.mark.parametrize(
    "boundary", ["role", "purpose", "user", "tenant", "environment", "cancelled", "no_task"]
)
def test_broker_denies_wrong_execution_scope(vault, boundary):
    vault.put("openai_api_key", "scoped-test-secret", ["strategist"])
    task = running(vault, "research_analyst" if boundary == "role" else "strategist")
    if boundary == "user":
        vault.db.execute("UPDATE tasks SET owner_id='intruder'")
    if boundary == "tenant":
        vault.settings.tenant_id = "different"
    if boundary == "environment":
        vault.settings.environment = "production"
    if boundary == "cancelled":
        vault.db.execute("UPDATE tasks SET status='cancelled'")
    with pytest.raises(RuntimeError):
        vault.get(
            "openai_api_key",
            "send_email" if boundary == "purpose" else "model",
            None if boundary == "no_task" else task,
        )


@pytest.mark.parametrize("fault", ["missing", "public", "inside_data", "symlink", "wrong_key"])
def test_key_boundary_and_missing_keys_deny(vault, fault):
    vault.put("openai_api_key", "protected-test-secret", ["strategist"])
    task = running(vault)
    path = vault.settings.credential_key_file
    if fault == "missing":
        path.unlink()
    elif fault == "public":
        path.chmod(0o644)
    elif fault == "inside_data":
        moved = vault.settings.data_dir / "keyring"
        path.rename(moved)
        vault.settings.credential_key_file = moved
    elif fault == "symlink":
        path.rename(path.with_suffix(".real"))
        path.symlink_to(path.with_suffix(".real"))
    else:
        path.write_text(
            json.dumps({"active": "one", "keys": {"one": base64.b64encode(os.urandom(32)).decode()}})
        )
    with pytest.raises(CredentialError):
        vault.get("openai_api_key", "model", task)


def test_database_binding_preserves_owner_data_and_refuses_other_domains(settings):
    db = Database(settings.data_dir / "agent4good.sqlite3")
    task = db.create_task("Original owner work", "Keep this", "strategist")
    create_app(settings)
    assert db.task(task)["prompt"] == "Keep this"
    for field in ("tenant_id", "environment"):
        other = settings.model_copy(update={field: "other"})
        with pytest.raises(RuntimeError, match="tenant or environment"):
            create_app(other)
        with pytest.raises(RuntimeError):
            Engine(db, other)
    with pytest.raises(ValueError):
        Settings(_env_file=None, isolation_mode="multi_user")


def test_owner_endpoints_hide_foreign_task_records(owner, app):
    db = app.state.db
    task = db.create_task("Foreign private title", "Foreign private prompt", "strategist")
    db.execute("UPDATE tasks SET owner_id='foreign' WHERE id=?", (task,))
    db.event(task, "private", "Foreign private event")
    db.execute(
        "INSERT INTO artifacts VALUES ('foreign',?,'private.txt','Foreign private artifact',?)", (task, now())
    )
    db.execute(
        "INSERT INTO approvals(id,task_id,call_id,tool,arguments,created_at) VALUES ('foreign',?,'call','memory_write','{}',?)",
        (task, now()),
    )
    for route in ("/api/tasks", "/api/events", "/api/overview", "/api/approvals"):
        response = owner.get(route)
        assert response.status_code == 200
        assert "Foreign private" not in response.text
        assert task not in response.text
    for route in (f"/api/tasks/{task}", "/api/artifacts/foreign"):
        assert owner.get(route).status_code == 404
    assert owner.post(f"/api/tasks/{task}/cancel", json={}).status_code == 404
    assert owner.post("/api/approvals/foreign/decision", json={"decision": "approve"}).status_code == 404
    assert (
        owner.post(
            "/api/tasks", json={"title": "Forged", "prompt": "Owner override", "owner_id": "foreign"}
        ).status_code
        == 422
    )


def test_vault_provider_and_redaction_on_actual_engine_path(vault, monkeypatch):
    secret = 'unique-secret-"with-escape'
    vault.put("openai_api_key", secret, ["strategist"])
    task = running(vault)
    vault.db.execute("UPDATE tasks SET prompt=? WHERE id=?", ("Accidental " + secret, task))
    seen = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer " + secret
            assert secret not in repr(kwargs["json"])
            assert json.dumps(secret)[1:-1] not in json.dumps(kwargs["json"])
            seen.append(url)
            return httpx.Response(
                200,
                json={"output": [{"type": "message", "content": [{"type": "output_text", "text": secret}]}]},
            )

    monkeypatch.setattr(httpx, "Client", Client)
    Engine(vault.db, vault.settings).run(task)
    row = vault.db.task(task)
    assert row["status"] == "done"
    assert row["result"] == "[redacted]"
    assert secret not in repr(json.loads(row["items"]))
    assert vault.db.task(task)["items"].count("[redacted]") >= 2
    assert len(seen) == 1


def test_secrets_rejected_from_api_and_tool_arguments(vault):
    secret = "do-not-store-this-synthetic-key"
    vault.put("openai_api_key", secret, ["strategist"])
    with TestClient(create_app(vault.settings)) as client:
        login = client.post("/api/login", json={"password": vault.settings.admin_password})
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        response = client.post("/api/tasks", json={"title": "Private", "prompt": secret})
        assert response.status_code == 400
        assert secret not in response.text
        response = client.post("/api/login", json={"password": secret, "extra": secret})
        assert response.status_code == 422
        assert secret not in response.text
    task = running(vault)
    with pytest.raises(ToolError, match="Credentials"):
        ToolRegistry(vault.settings, vault.db).execute(
            "artifact_write", {"name": "leak.txt", "content": secret}, task_id=task, call_id="leak"
        )
    assert not vault.db.all("SELECT * FROM artifacts")
    assert secret not in repr(vault.settings)
    assert secret not in repr(vault.settings.model_dump())


def signature(settings, secret, body, timestamp=None, delivery="test_delivery_1234567890"):
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    prefix = f"v1\nPOST\n/api/webhooks/tasks\n{settings.tenant_id}\n{settings.environment}\n{timestamp}\n{delivery}\n"
    return {
        "content-type": "application/json",
        "x-a4g-timestamp": timestamp,
        "x-a4g-delivery": delivery,
        "x-a4g-signature": "v1="
        + hmac.new(secret.encode(), prefix.encode() + body, hashlib.sha256).hexdigest(),
    }


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "signature",
        "stale",
        "future",
        "changed_body",
        "tenant",
        "environment",
        "oversize",
        "scope",
        "unsigned",
    ],
)
def test_webhook_authentication_limits_and_no_execution(vault, fault):
    secret = "synthetic-webhook-authentication-secret"
    vault.put("webhook_secret", secret, ["strategist"])
    body = json.dumps({"title": "Tagged webhook test", "prompt": "Create a draft only"}).encode()
    timestamp = (
        int(time.time()) + (600 if fault == "future" else -600) if fault in {"future", "stale"} else None
    )
    if fault == "oversize":
        body = b" " * 32001
    if fault == "scope":
        body = json.dumps({"title": "Escalation", "prompt": "Run", "start": True}).encode()
    signing_settings = vault.settings.model_copy(
        update={fault: "other"}
        if fault == "environment"
        else {"tenant_id": "other"}
        if fault == "tenant"
        else {}
    )
    headers = signature(signing_settings, secret, body, timestamp)
    if fault == "signature":
        headers["x-a4g-signature"] = "v1=" + "0" * 64
    if fault == "unsigned":
        headers = {"content-type": "application/json"}
    if fault == "changed_body":
        body += b" "
    with TestClient(create_app(vault.settings)) as client:
        result = client.post("/api/webhooks/tasks", content=body, headers=headers)
        if fault == "none":
            assert result.status_code == 201
            assert result.json()["status"] == "draft"
            assert client.post("/api/webhooks/tasks", content=body, headers=headers).status_code == 409
            assert len(vault.db.all("SELECT * FROM tasks")) == 1
        else:
            assert result.status_code in {401, 413, 422}
            assert vault.db.all("SELECT * FROM tasks") == []


def test_simultaneous_webhook_delivery_creates_one_draft(vault):
    secret = "concurrent-webhook-test-secret-long-enough"
    vault.put("webhook_secret", secret, ["strategist"])
    body = b'{"title":"One draft","prompt":"Never start automatically"}'
    headers = signature(vault.settings, secret, body)
    app = create_app(vault.settings)

    def send(_):
        with TestClient(app) as client:
            return client.post("/api/webhooks/tasks", content=body, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, range(4)))
    assert results.count(201) == 1
    assert results.count(409) == 3


def test_unconfigured_webhook_and_unsupported_commands_stay_unavailable(owner, app, settings):
    assert owner.post("/api/webhooks/tasks", json={}).status_code == 404
    task = app.state.db.create_task("No shell", "No commands", "strategist")
    app.state.db.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
    registry = ToolRegistry(settings, app.state.db)
    for name in ("shell", "exec", "credential_read", "vault_put"):
        with pytest.raises((ValueError, ToolError)):
            registry.execute(name, {"command": "cat /etc/passwd"}, task_id=task, call_id=name)


def test_revocation_has_no_legacy_fallback(vault):
    vault.settings.openai_api_key = "legacy-stale-credential"
    vault.put("openai_api_key", "vault-test-credential", ["strategist"])
    task = running(vault)
    vault.revoke("openai_api_key")
    assert not vault.configured("openai_api_key")
    with pytest.raises(CredentialError):
        vault.get("openai_api_key", "model", task)


def test_webhook_throttle_counts_invalid_signatures(vault):
    vault.put("webhook_secret", "webhook-test-secret-longer-than-thirty-two", ["strategist"])
    with TestClient(create_app(vault.settings)) as client:
        for _ in range(60):
            assert client.post("/api/webhooks/tasks", json={}).status_code == 401
        assert client.post("/api/webhooks/tasks", json={}).status_code == 429
    assert vault.db.all("SELECT * FROM tasks") == []


def test_broker_model_role_denial_prevents_network(vault, monkeypatch):
    vault.put("openai_api_key", "model-credential-not-for-research", ["strategist"])
    task = running(vault, "research_analyst")

    def fail(*args, **kwargs):
        raise AssertionError("Denied model requests must not reach network")

    monkeypatch.setattr(httpx, "Client", fail)
    Engine(vault.db, vault.settings).run(task)
    assert vault.db.task(task)["status"] == "failed"
    assert "scope denied" in vault.db.task(task)["error"]


def test_failed_key_rotation_is_atomic(vault):
    vault.put("openai_api_key", "first-test-credential", ["strategist"])
    vault.put("github_token", "second-test-credential", ["strategist"])
    vault.db.execute("UPDATE credentials SET ciphertext=? WHERE name='github_token'", (b"tampered",))
    before = vault.db.all("SELECT * FROM credentials")
    with pytest.raises(CredentialError):
        vault.rotate()
    assert vault.db.all("SELECT * FROM credentials") == before


def test_adapter_uses_only_allowed_vault_credential(vault, monkeypatch):
    vault.settings.github_repo = "owner/repository"
    secret = "synthetic-github-adapter-credential"
    vault.put("github_token", secret, ["strategist"], ["github_list_issues"])
    task = running(vault)
    registry = ToolRegistry(vault.settings, vault.db)
    calls = []

    def request(method, url, headers, payload=None):
        calls.append(url)
        assert headers["Authorization"] == "Bearer " + secret
        return []

    monkeypatch.setattr(registry, "_request", request)
    assert registry.execute("github_list_issues", {}, task_id=task, call_id="allowed") == []
    with pytest.raises(CredentialError, match="scope"):
        registry.execute(
            "github_read_file", {"path": "README.md", "ref": "main"}, task_id=task, call_id="denied"
        )
    assert len(calls) == 1
    assert secret not in repr(vault.db.all("SELECT * FROM tool_runs"))


def test_host_cli_encryption_rotation_recovery_and_revocation(settings, tmp_path):
    import subprocess
    import sys

    env = {
        "PATH": os.environ["PATH"],
        "A4G_ADMIN_PASSWORD": settings.admin_password,
        "A4G_SESSION_SECRET": settings.session_secret,
        "A4G_SECURE_COOKIES": "false",
        "A4G_DATA_DIR": str(tmp_path / "cli-data"),
        "A4G_CREDENTIAL_KEY_FILE": str(tmp_path / "cli-key.json"),
    }
    secret = "synthetic-cli-input-never-print"

    def invoke(*args, stdin=None):
        result = subprocess.run(
            [sys.executable, "-m", "agent4good.vault", *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
        )
        assert secret not in result.stdout + result.stderr
        return result.stdout

    invoke("init-key", env["A4G_CREDENTIAL_KEY_FILE"])
    invoke("put", "openai_api_key", "--agent", "strategist", "--stdin", stdin=secret)
    assert "openai_api_key" in invoke("status")
    invoke("rotate")
    invoke("rewrap")
    document = json.loads((tmp_path / "cli-key.json").read_text())
    assert len(document["keys"]) == 2
    invoke("revoke", "openai_api_key")
    assert '"credentials": []' in invoke("status")


def test_sessions_cannot_move_to_another_environment(vault, tmp_path):
    with TestClient(create_app(vault.settings)) as source:
        source.post("/api/login", json={"password": vault.settings.admin_password})
        cookie = source.cookies.get("a4g_session")
    copied = vault.db.one("SELECT * FROM sessions")
    other = vault.settings.model_copy(
        update={"data_dir": tmp_path / "other", "environment": "other", "credential_key_file": None}
    )
    app = create_app(other)
    app.state.db.execute("INSERT INTO sessions VALUES (?,?,?)", tuple(copied.values()))
    with TestClient(app) as target:
        target.cookies.set("a4g_session", cookie)
        assert target.get("/api/session").status_code == 401


def test_unauthenticated_requests_cannot_probe_known_secrets(vault):
    secret = "do-not-reveal-secret-presence-to-callers"
    vault.put("openai_api_key", secret, ["strategist"])
    with TestClient(create_app(vault.settings)) as client:
        for value in (secret, "not-a-secret"):
            assert client.post("/api/tasks", json={"title": "probe", "prompt": value}).status_code == 401


def test_redaction_retains_inflight_value_after_revocation(vault):
    secret = "inflight-synthetic-credential-to-redact"
    vault.put("openai_api_key", secret, ["strategist"])
    task = running(vault)
    assert vault.get("openai_api_key", "model", task) == secret
    CredentialBroker(vault.settings, vault.db).revoke("openai_api_key")
    assert vault.redact(secret) == "[redacted]"
