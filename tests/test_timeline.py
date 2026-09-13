import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent4good.db import Database
from agent4good.engine import Engine
from agent4good.execution import ExecutionJournal
from agent4good.timeline import emit, read, usage
from agent4good.tools import ToolRegistry
from test_workers import spawn


def test_owner_access_filters_redaction_and_no_transcripts(owner, client, app, settings):
    db = app.state.db
    task = db.create_task("Timeline test", "Owner goal", "strategist")
    other = db.create_task("Other owner secret", "private", "research_analyst")
    db.execute("UPDATE tasks SET owner_id='other' WHERE id=?", (other,))
    db.execute(
        "UPDATE tasks SET items=?,pending=?,result=? WHERE id=?",
        ("hidden reasoning", "raw arguments", settings.admin_password, task),
    )
    db.event(task, "failed", settings.session_secret)
    db.event(other, "failed", "foreign event secret")
    payload = owner.get("/api/timeline", params={"task_id": task}).json()
    text = json.dumps(payload)
    for forbidden in [
        "hidden reasoning",
        "raw arguments",
        "foreign event secret",
        settings.admin_password,
        settings.session_secret,
    ]:
        assert forbidden not in text
    assert "[redacted]" in text
    assert owner.get("/api/timeline", params={"task_id": other}).status_code == 404
    assert owner.get("/api/timeline", params={"limit": 501}).status_code == 422
    assert owner.get("/api/timeline", params={"after": -1}).status_code == 422
    assert (
        owner.get("/api/timeline", params={"kind": "failed", "agent": "strategist"}).json()["events"][0][
            "kind"
        ]
        == "failed"
    )
    assert "foreign event secret" not in owner.get("/api/timeline").text
    owner.post("/api/logout", json={})
    assert client.get("/api/timeline").status_code == 401


def test_cursor_export_watermark_concurrent_writes_restore(settings, tmp_path):
    db = Database(tmp_path / "source.sqlite")
    credentials = ToolRegistry(settings, db).credentials
    task = db.create_task("Trace", "Observe", "strategist")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: db.event(task, "test", str(i)), range(45)))
    first = read(db, credentials, limit=7, task_id=task)
    db.event(task, "late", "after export started")
    collected = first["events"][:]
    cursor = first["next_cursor"]
    while first["has_more"]:
        first = read(db, credentials, limit=7, task_id=task, after=cursor, through=first["through"])
        collected += first["events"]
        cursor = first["next_cursor"]
    ids = [e["id"] for e in collected]
    assert len(ids) == len(set(ids)) == 46
    assert ids == sorted(ids)
    with sqlite3.connect(db.path) as source, sqlite3.connect(tmp_path / "restored.sqlite") as dest:
        source.backup(dest)
    restored = Database(tmp_path / "restored.sqlite")
    # Restart/reconnect uses the same durable cursor, including skipped filters.
    remaining = read(restored, credentials, task_id=task, after=cursor)
    assert [e["kind"] for e in remaining["events"]] == ["late"]
    assert read(restored, credentials, task_id=task, after=remaining["next_cursor"])["events"] == []
    assert read(restored, credentials, task_id=task, after=0)["events"][:46] == collected


def test_usage_never_presents_reservations_or_missing_cost_as_actual():
    call = dict(
        usage=json.dumps({"total_tokens": 12, "input_tokens": 10, "output_tokens": 2}), reserved_tokens=9000
    )
    profile = dict(input_usd_per_million=1, output_usd_per_million=5)
    report = usage([call], profile)
    assert report["reported_tokens"] == 12
    assert report["estimated_model_usd"] == 0.00002
    assert report["actual_cost_usd"] is None and report["tool_cost_usd"] is None
    assert usage([call], {})["cost_label"] == "unavailable"
    partial = usage([call, {**call, "usage": None}], profile)
    assert partial["tokens_label"] == "partial" and partial["estimated_model_usd"] is None
    assert usage([], {})["reported_tokens"] is None


def test_step_replay_records_one_action_and_rollback_keeps_log_atomic(settings, tmp_path):
    db = Database(tmp_path / "journal.sqlite")
    task = db.create_task("Trace", "Observe", "strategist")
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
    journal = ExecutionJournal(db, settings)
    call = dict(name="memory_read", arguments="{}", call_id="step-1")
    journal.checkpoint(task, [], [call])
    journal.observe(task, call, {"data": {}})
    journal.observe(task, call, {"data": {}})
    rows = read(db, ToolRegistry(settings, db).credentials, task_id=task)["events"]
    assert len([e for e in rows if e["kind"] == "action_observed"]) == 1
    assert next(e for e in rows if e["kind"] == "plan_step")["step_id"] == "step-1"
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            emit(conn, task, "rollback", "Must not survive")
            raise RuntimeError()
    assert not db.one("SELECT 1 FROM events WHERE kind='rollback'")


def test_real_parallel_workers_timeline_matches_storage(settings, tmp_path):
    db = Database(tmp_path / "workers.sqlite")
    registry = ToolRegistry(settings, db)
    parent = db.create_task(
        "Tagged scripted timeline acceptance", "Collect two independent results", "strategist"
    )
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
    children = [spawn(registry, parent, label) for label in ["Evidence A", "Evidence B"]]

    class Provider:
        def respond(self, instructions, items, tools):
            return {"output": [], "output_text": "Scripted acceptance evidence", "usage": {}}

    engines = [Engine(db, settings, Provider()) for _ in children]
    assert {e.claim() for e in engines} == set(children)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pair: pair[0].run(pair[1]), zip(engines, children)))
    registry.execute("worker_results", {}, task_id=parent, call_id="results")
    Engine(db, settings, Provider()).run(parent)
    data = read(db, registry.credentials, task_id=parent, limit=500)
    assert len(data["tasks"]) == 3
    assert all(t["status"] == "done" and t["result"] for t in data["tasks"])
    assert all(t["usage"]["cost_label"] == "unavailable" for t in data["tasks"])
    assert len([e for e in data["events"] if e["kind"] == "delegated"]) == 2
    assert len([e for e in data["events"] if e["kind"] == "completed"]) == 3
    assert {e["id"] for e in data["events"]} == {e["id"] for e in db.all("SELECT id FROM events")}
    assert all(e["root_id"] == parent for e in data["events"])


def test_router_retry_approval_and_final_evidence(owner, app, settings, monkeypatch):
    from agent4good.model_router import ADAPTERS
    from agent4good.provider import ProviderError

    settings.openai_api_key = "test-timeline-model-key"
    db = app.state.db
    calls = 0

    class Adapter:
        credential_name = "openai_api_key"

        def __init__(self, *args):
            pass

        def validate(self, *args):
            pass

        def respond(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderError("transport", "scripted failure", retryable=True)
            if calls == 2:
                return {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "write",
                            "name": "memory_write",
                            "arguments": json.dumps({"key": "timeline_test", "content": "Tagged fixture"}),
                        }
                    ],
                    "output_text": "",
                    "usage": {"total_tokens": 30, "input_tokens": 20, "output_tokens": 10},
                }
            return {
                "output": [],
                "output_text": "Approved action completed",
                "usage": {"total_tokens": 10, "input_tokens": 5, "output_tokens": 5},
            }

    monkeypatch.setitem(ADAPTERS, "openai", Adapter)
    task = db.create_task("Approval trace", "Write tagged test memory", "strategist", True)
    engine = Engine(db, settings)
    assert engine.claim() == task
    engine.run(task)
    assert db.task(task)["status"] == "waiting_approval", db.task(task)["error"]
    approval = db.one("SELECT * FROM approvals WHERE task_id=?", (task,))
    assert (
        owner.post("/api/approvals/" + approval["id"] + "/decision", json={"decision": "approve"}).status_code
        == 200
    )
    # Fresh engine resumes saved plan, as a restarted process would.
    engine = Engine(db, settings)
    assert engine.claim() == task
    engine.run(task)
    report = owner.get("/api/timeline", params={"task_id": task, "limit": 500}).json()
    events = report["events"]
    assert report["tasks"][0]["status"] == "done", db.task(task)["error"]
    assert {
        "model_retry",
        "model_call_failed",
        "model_call_started",
        "model_call_completed",
        "approval_created",
        "approved",
        "plan_step",
        "action_observed",
        "completed",
    } <= {e["kind"] for e in events}
    assert next(e for e in events if e["kind"] == "approved")["approval_id"] == approval["id"]
    assert report["tasks"][0]["usage"]["reported_tokens"] == 40
    assert report["tasks"][0]["usage"]["tokens_label"] == "partial"
    assert settings.openai_api_key not in json.dumps(report)
