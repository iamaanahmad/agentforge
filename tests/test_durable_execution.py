"""Crash-point tests use scripted services. No live messages are sent."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent4good.engine import Engine
from test_engine import FakeProvider, answer, approve, call, make


class Crash(BaseException):
    pass


def restart(db, settings, task, provider):
    engine = Engine(db, settings, provider)
    engine.recover()
    assert engine.claim() == task
    return engine


def test_crash_before_dispatch_resumes_dependencies(settings, monkeypatch):
    p = FakeProvider(
        answer(
            calls=[
                call("memory_read", {"key": "first"}, "a"),
                call("artifact_write", {"name": "proof.md", "content": "proof"}, "b"),
            ]
        )
    )
    db, e, t = make(settings, p)
    monkeypatch.setattr(e.registry, "execute", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        e.run(t)
    steps = db.all("SELECT * FROM plan_steps ORDER BY action_id")
    assert steps[1]["depends_on"] == "a"
    again = restart(db, settings, t, FakeProvider(answer("Saved evidence")))
    again.run(t)
    assert db.task(t)["status"] == "done"
    assert len(db.all("SELECT * FROM artifacts")) == 1
    assert db.one("SELECT phase FROM executions")["phase"] == "verified"


def test_crash_after_receipt_before_checkpoint_reuses_write(settings, monkeypatch):
    args = {"name": "proof.md", "content": "only once"}
    db, e, t = make(settings, FakeProvider(answer(calls=[call("artifact_write", args)])))
    monkeypatch.setattr(e.journal, "observe", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        e.run(t)
    assert len(db.all("SELECT * FROM artifacts")) == 1
    again = restart(
        db,
        settings,
        t,
        FakeProvider(answer(calls=[call("artifact_write", args, "new-model-id")]), answer("Saved once")),
    )
    again.run(t)
    assert db.task(t)["status"] == "done"
    assert len(db.all("SELECT * FROM artifacts")) == 1
    assert len(db.all("SELECT * FROM tool_runs")) == 1


def test_provider_acceptance_before_local_receipt_is_held(settings):
    settings.resend_api_key, settings.mail_from = "test-key", "owner@example.com"
    db, e, t = make(
        settings,
        FakeProvider(
            answer(
                calls=[
                    call("send_email", {"to": "test@example.com", "subject": "Test", "body": "Tagged test"})
                ]
            )
        ),
    )
    e.run(t)
    approve(db, t)
    e.claim()
    accepted = []

    def accept_then_crash(*args):
        accepted.append(args[2]["Idempotency-Key"])
        raise Crash()

    e.registry._request = accept_then_crash
    with pytest.raises(Crash):
        e.run(t)
    again = Engine(db, settings, FakeProvider())
    again.recover()
    assert db.task(t)["status"] == "failed"
    assert again.claim() is None
    assert len(accepted) == 1 and len(accepted[0]) == 64
    assert db.one("SELECT status FROM tool_runs")["status"] == "started"


def test_read_failure_replans_and_finishes_after_restart(settings, monkeypatch):
    db, e, t = make(settings, FakeProvider(answer(calls=[call("memory_read", {"key": "missing"}, "read")])))
    e.registry._dispatch = lambda *a: (_ for _ in ()).throw(TimeoutError())
    saved = e._save

    def crash_after_observation(task_id, items, pending, final_text=None):
        saved(task_id, items, pending, final_text)
        if items and items[-1].get("type") == "function_call_output":
            raise Crash()

    e._save = crash_after_observation
    with pytest.raises(Crash):
        e.run(t)
    provider = FakeProvider(
        answer(
            calls=[
                call(
                    "artifact_write",
                    {"name": "limits.md", "content": "Read failed; evidence unavailable"},
                    "fallback",
                )
            ]
        ),
        answer("Saved the limitation"),
    )
    again = restart(db, settings, t, provider)
    again.run(t)
    assert db.task(t)["status"] == "done"
    assert "Read failed" in json.dumps(provider.calls[0][1])
    assert db.one("SELECT revision FROM executions")["revision"] >= 3


def test_final_checkpoint_finishes_without_another_model_call(settings, monkeypatch):
    db, e, t = make(settings, FakeProvider(answer("Verified saved result")))
    monkeypatch.setattr(e.journal, "finish", lambda *a: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        e.run(t)
    provider = FakeProvider()
    again = restart(db, settings, t, provider)
    again.run(t)
    assert db.task(t)["result"] == "Verified saved result"
    assert provider.calls == []


def test_concurrent_runners_and_recovery_are_fenced(settings):
    entered, release = threading.Event(), threading.Event()
    db, e, t = make(settings, FakeProvider())

    def respond(*a):
        entered.set()
        assert release.wait(5)
        return answer("One runner")

    e.provider.respond = respond
    other = Engine(db, settings, FakeProvider())
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(e.run, t)
        assert entered.wait(5)
        other.recover()
        other.run(t)
        assert db.task(t)["status"] == "running"
        release.set()
        future.result()
    assert db.task(t)["steps"] == 1
    assert db.task(t)["status"] == "done"


def test_dependency_refuses_out_of_order_action(settings):
    db, e, t = make(settings, FakeProvider())
    pending = [call("memory_read", {"key": "one"}, "a"), call("memory_read", {"key": "two"}, "b")]
    e._save(t, [], pending)
    with pytest.raises(RuntimeError, match="dependency"):
        e.journal.before(t, pending[1])


def test_cancelled_task_is_not_recovered(settings):
    db, e, t = make(settings, FakeProvider())
    e._save(t, [], [])
    db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (t,))
    e.recover()
    assert e.claim() is None
    assert db.task(t)["status"] == "cancelled"


def test_deadline_and_recovery_budget(settings):
    db, e, t = make(settings, FakeProvider())
    e._save(t, [], [])
    db.execute("UPDATE executions SET started_at='2000-01-01T00:00:00+00:00'")
    e.run(t)
    assert "deadline" in db.task(t)["error"]
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (t,))
    db.execute("UPDATE executions SET recoveries=?", (settings.max_recoveries,))
    e.recover()
    assert db.task(t)["status"] == "failed"
    assert e.claim() is None


def test_model_transport_retries_are_bounded(settings):
    settings.max_model_retries = 1
    db, e, t = make(settings, FakeProvider())
    e.provider.respond = lambda *a: (_ for _ in ()).throw(TimeoutError())
    e.run(t)
    assert "retry limit" in db.task(t)["error"]
    assert db.task(t)["steps"] == 2


def test_verification_refuses_missing_observations(settings):
    db, e, t = make(settings, FakeProvider())
    e._save(t, [], [call("memory_read", {"key": "x"})])
    db.execute("UPDATE tasks SET pending='[]' WHERE id=?", (t,))
    with pytest.raises(RuntimeError, match="incomplete plan"):
        e.journal.finish(t, "Unsupported completion")


def test_external_receipt_survives_replan_with_new_id(settings):
    settings.resend_api_key, settings.mail_from = "test-key", "owner@example.com"
    args = {"to": "test@example.com", "subject": "Test", "body": "Tagged test"}
    db, e, t = make(
        settings,
        FakeProvider(
            answer(calls=[call("send_email", args, "first")]),
            answer(calls=[call("send_email", args, "new")]),
            answer("Sent once"),
        ),
    )
    sent = []
    e.registry._request = lambda *a: sent.append(a[3]) or {"id": "one"}
    e.run(t)
    approve(db, t)
    e.claim()
    e.run(t)
    assert db.task(t)["status"] == "done"
    assert len(sent) == 1
    assert len(db.all("SELECT * FROM approvals")) == 1
    assert len(db.all("SELECT * FROM plan_steps")) == 1


def test_crash_after_intent_before_request_never_dispatches(settings):
    settings.resend_api_key, settings.mail_from = "test-key", "owner@example.com"
    db, e, t = make(
        settings,
        FakeProvider(
            answer(
                calls=[
                    call("send_email", {"to": "test@example.com", "subject": "Test", "body": "Tagged test"})
                ]
            )
        ),
    )
    e.run(t)
    approve(db, t)
    e.claim()
    e.registry._dispatch = lambda *a: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        e.run(t)
    e.recover()
    assert e.claim() is None
    assert db.task(t)["status"] == "failed"


def test_cancel_during_write_retains_receipt(settings):
    db, e, t = make(
        settings,
        FakeProvider(answer(calls=[call("artifact_write", {"name": "proof.md", "content": "kept"})])),
    )
    dispatch = e.registry._dispatch

    def cancel_after_write(*a):
        result = dispatch(*a)
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (t,))
        return result

    e.registry._dispatch = cancel_after_write
    e.run(t)
    assert db.task(t)["status"] == "cancelled"
    assert db.one("SELECT status FROM tool_runs")["status"] == "done"
    assert len(db.all("SELECT * FROM artifacts")) == 1


def test_read_transport_retry_limit(settings):
    db, e, t = make(settings, FakeProvider())
    e.registry._dispatch = lambda *a: (_ for _ in ()).throw(TimeoutError())
    for _ in range(3):
        with pytest.raises(TimeoutError):
            e.registry.execute("memory_read", {"key": "x"}, task_id=t, call_id="read")
    with pytest.raises(ValueError, match="retry limit"):
        e.registry.execute("memory_read", {"key": "x"}, task_id=t, call_id="read")
    assert db.one("SELECT attempts FROM tool_runs")["attempts"] == 3


def test_unknown_execution_version_fails_closed(settings):
    db, e, t = make(settings, FakeProvider())
    e._save(t, [], [])
    db.execute("UPDATE executions SET version=999")
    e.run(t)
    assert db.task(t)["status"] == "failed"
    assert "Unsupported execution version" in db.task(t)["error"]


def test_task_api_exposes_journal_only_to_owner(settings):
    from fastapi.testclient import TestClient
    from agent4good.app import create_app
    from test_release_acceptance import login

    app = create_app(settings)
    db = app.state.db
    t = db.create_task("Tagged test", "Save evidence", "strategist", True)
    e = Engine(db, settings, FakeProvider(answer("No tools needed")))
    assert e.claim() == t
    e.run(t)
    with TestClient(app) as client:
        assert client.get("/api/tasks/" + t).status_code == 401
        login(client, settings)
        result = client.get("/api/tasks/" + t).json()
        assert result["execution"]["phase"] == "verified"
        assert result["plan_steps"] == []
        db.execute("UPDATE tasks SET owner_id='other' WHERE id=?", (t,))
        assert client.get("/api/tasks/" + t).status_code == 404


def test_process_death_releases_lock_and_preserves_completed_action(settings):
    import multiprocessing
    import os

    db, e, t = make(
        settings,
        FakeProvider(
            answer(
                calls=[
                    call("artifact_write", {"name": "process-proof.md", "content": "survives process death"})
                ]
            )
        ),
    )
    e.journal.observe = lambda *a, **k: os._exit(17)
    process = multiprocessing.get_context("fork").Process(target=e.run, args=(t,))
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("Crash test process did not exit")
    assert process.exitcode == 17
    assert db.one("SELECT status FROM tool_runs")["status"] == "done"
    again = restart(db, settings, t, FakeProvider(answer("Recovered after process death")))
    again.run(t)
    assert db.task(t)["status"] == "done"
    assert len(db.all("SELECT * FROM artifacts")) == 1
