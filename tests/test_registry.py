"""Exercise the public execution boundary, not a replacement for that boundary."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent4good.tools import CATEGORIES, SPECS, ToolError, ToolRegistry
from test_tools import runnable, permit


def test_catalog_covers_categories_without_exposing_placeholders(settings):
    r, task = runnable(settings)
    rows = r.catalog()
    assert {row["category"] for row in rows} == set(CATEGORIES)
    assert {t["name"] for t in r.definitions()} == {
        "skill_read",
        "memory_read",
        "memory_search",
        "memory_store",
        "memory_write",
        "artifact_write",
        "worker_spawn",
        "worker_message",
        "worker_context",
        "worker_results",
        "worker_wait",
        "schedule_create",
        "schedule_cancel",
    }
    assert next(row for row in rows if row["name"] == "send_email")["state"] == "unavailable"
    for row in rows:
        if row["state"] == "planned":
            assert row["name"] is None
        else:
            for field in (
                "description",
                "input_schema",
                "output_schema",
                "permissions",
                "risk",
                "rate_limit",
                "timeout_seconds",
                "audit",
            ):
                assert row[field]
            assert "authentication" in row
    r.execute("memory_read", {"key": "missing"}, task_id=task, call_id="check")
    assert next(row for row in r.catalog() if row["name"] == "memory_read")["state"] == "verified"
    # Discovery cannot mutate the source schema.
    r.definitions()[0]["parameters"]["properties"].clear()
    with pytest.raises(ToolError):
        r.execute("memory_read", {}, task_id=task, call_id="wrong")


def test_direct_execution_requires_context_and_exact_approval(settings, monkeypatch):
    settings.resend_api_key, settings.mail_from = "test-key", "owner@example.com"
    r, task = runnable(settings)
    calls = []
    monkeypatch.setattr(r, "_request", lambda *args: calls.append(args) or {"id": "ok"})
    args = {"to": "one@example.com", "subject": "Hello", "body": "Exact content"}
    for kwargs in ({}, {"task_id": task, "call_id": "check"}):
        with pytest.raises(ToolError):
            r.execute("send_email", args, **kwargs)
    permit(r, task, "send_email", args)
    for wrong_args, call_id in (({**args, "body": "Changed"}, "check"), (args, "other")):
        with pytest.raises(ToolError):
            r.execute("send_email", wrong_args, task_id=task, call_id=call_id)
    assert not calls
    assert r.execute("send_email", args, task_id=task, call_id="check") == {"message_id": "ok"}
    assert r.execute("send_email", args, task_id=task, call_id="check") == {"message_id": "ok"}
    assert len(calls) == 1


@pytest.mark.parametrize("result", [{"found": True}, {"found": False, "secret": "do-not-log"}, []])
def test_bad_output_is_audited_and_never_replayed(settings, monkeypatch, result):
    r, task = runnable(settings)
    monkeypatch.setattr(r, "_internal", lambda *args: result)
    with pytest.raises(ToolError, match="output"):
        r.execute("memory_read", {"key": "missing"}, task_id=task, call_id="check")
    receipt = r.db.all("SELECT * FROM tool_runs")[0]
    assert receipt["status"] == "started" and not receipt["result"]
    assert r.db.all("SELECT * FROM events WHERE kind='tool_failed'")
    with pytest.raises(ToolError, match="Ambiguous"):
        r.execute("memory_read", {"key": "missing"}, task_id=task, call_id="check")
    assert "do-not-log" not in json.dumps(r.db.all("SELECT * FROM events"))


def test_rate_limit_shared_between_instances_and_replay_is_free(settings, monkeypatch):
    r, task = runnable(settings)
    monkeypatch.setitem(SPECS, "memory_read", replace(SPECS["memory_read"], rate_limit=1))
    args = {"key": "missing"}
    r.execute("memory_read", args, task_id=task, call_id="one")
    second = ToolRegistry(settings, r.db)
    assert second.execute("memory_read", args, task_id=task, call_id="one") == {"found": False}
    with pytest.raises(ToolError, match="rate limit"):
        second.execute("memory_read", args, task_id=task, call_id="two")
    assert len(r.db.all("SELECT * FROM tool_runs")) == 1


def test_concurrent_call_has_one_receipt_and_one_mutation(settings):
    r, task = runnable(settings)
    args = {"name": "proof.txt", "content": "Tagged test"}

    def invoke(_):
        try:
            return ToolRegistry(settings, r.db).execute("artifact_write", args, task_id=task, call_id="one")
        except ToolError as exc:
            assert "Ambiguous" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(invoke, range(4)))
    assert any(results)
    assert len(r.db.all("SELECT * FROM artifacts")) == 1
    assert len(r.db.all("SELECT * FROM tool_runs")) == 1


def test_timeout_hook_cannot_be_skipped_by_direct_execution(settings, monkeypatch):
    r, task = runnable(settings)
    monkeypatch.setitem(SPECS, "memory_read", replace(SPECS["memory_read"], timeout_seconds=0))
    with pytest.raises(ToolError, match="deadline"):
        r.execute("memory_read", {"key": "missing"}, task_id=task, call_id="one")
    assert r.db.all("SELECT * FROM tool_runs")[0]["status"] == "started"
    with pytest.raises(ToolError, match="deadline"):
        r._dispatch(task, "memory_read", {"key": "missing"})


def test_manual_read_and_cancelled_task_refuse_direct_calls(settings):
    r, task = runnable(settings)
    r.db.execute("UPDATE settings SET value='manual' WHERE key='autonomy'")
    with pytest.raises(ToolError, match="Approval"):
        r.execute("memory_read", {"key": "missing"}, task_id=task, call_id="one")
    r.db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task,))
    with pytest.raises(ToolError, match="active"):
        r.execute("memory_read", {"key": "missing"}, task_id=task, call_id="one")


def test_catalog_endpoint_is_owner_only(client, owner):
    assert owner.get("/api/tools").status_code == 200
    owner.post("/api/logout", json={})
    assert client.get("/api/tools").status_code == 401


@pytest.mark.parametrize("name", list(SPECS))
def test_each_tool_rejects_invalid_input_and_output(name):
    from agent4good.tools import validate_arguments, validate_schema

    with pytest.raises(ToolError):
        validate_arguments(name, {"model_selected_credential": "must-not-be-accepted"})
    with pytest.raises(ToolError):
        validate_schema(SPECS[name].output_schema, None, "output")


def test_configured_credentials_without_audit_storage_are_not_executable(settings):
    settings.github_token = "test-token"
    settings.github_repo = "owner/repo"
    r = ToolRegistry(settings)
    assert r.definitions() == []
    assert "workspace database" in r.availability("github_list_issues")[1]
