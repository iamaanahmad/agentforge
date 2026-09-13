import json
from concurrent.futures import ThreadPoolExecutor
from agent4good.db import Database, now
from agent4good.engine import Engine


def answer(text="Complete", calls=None):
    return {
        "output": calls
        or [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
        "output_text": text if not calls else "",
        "usage": {"total_tokens": 42},
    }


def call(name, args, call_id="call_1"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}


class FakeProvider:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def respond(self, instructions, items, tools):
        self.calls.append((instructions, list(items), tools))
        return next(self.responses)


def make(settings, provider):
    db = Database(settings.data_dir / "engine.sqlite3")
    engine = Engine(db, settings, provider)
    task = db.create_task("Review product", "Use the supplied facts", "strategist", True)
    assert engine.claim() == task
    return db, engine, task


def approve(db, task):
    db.execute("UPDATE approvals SET status='approved' WHERE task_id=?", (task,))
    db.execute("UPDATE tasks SET status='queued' WHERE id=?", (task,))


def test_real_loop_saves_artifact_and_result(settings):
    p = FakeProvider(
        answer(calls=[call("artifact_write", {"name": "report.md", "content": "Evidence and conclusion"})]),
        answer("Saved the report"),
    )
    db, e, t = make(settings, p)
    e.run(t)
    assert db.task(t)["status"] == "done"
    assert db.task(t)["steps"] == 2
    assert db.all("SELECT * FROM artifacts")[0]["content"] == "Evidence and conclusion"
    assert p.calls[1][1][-1]["type"] == "function_call_output"
    assert "Do not invent revenue" in p.calls[0][0]


def test_write_pauses_then_resumes_exactly_once(settings):
    args = {"key": "product", "content": "A real fact"}
    p = FakeProvider(answer(calls=[call("memory_write", args)]), answer("Saved"))
    db, e, t = make(settings, p)
    e.run(t)
    assert db.task(t)["status"] == "waiting_approval"
    assert not db.all("SELECT * FROM memory")
    assert json.loads(db.all("SELECT * FROM approvals")[0]["arguments"]) == args
    approve(db, t)
    assert e.claim() == t
    e.run(t)
    assert db.task(t)["status"] == "done"
    assert db.all("SELECT * FROM memory")[0]["content"] == "A real fact"
    e.run(t)
    assert len(db.all("SELECT * FROM tool_runs")) == 1


def test_external_tool_approval_and_receipt_replay(settings):
    args = {"to": "person@example.com", "subject": "Hello", "body": "Exact body"}
    p = FakeProvider(
        answer(calls=[call("send_email", args)]),
        answer(calls=[call("send_email", args)]),
        answer("Sent once"),
    )
    db, e, t = make(settings, p)
    sent = []
    settings.resend_api_key = "test-key"
    settings.mail_from = "owner@example.com"
    e.registry._request = lambda *args: sent.append(args[3]) or {"id": "provider_receipt"}
    e.run(t)
    assert sent == []
    approve(db, t)
    e.claim()
    e.run(t)
    assert db.task(t)["status"] == "done"
    assert len(sent) == 1
    assert sent[0]["text"] == args["body"]


def test_tampered_approval_fails(settings):
    p = FakeProvider(answer(calls=[call("memory_write", {"key": "product", "content": "Original"})]))
    db, e, t = make(settings, p)
    e.run(t)
    approve(db, t)
    db.execute("UPDATE approvals SET arguments=?", (json.dumps({"key": "product", "content": "Changed"}),))
    e.claim()
    e.run(t)
    assert db.task(t)["status"] == "failed"
    assert not db.all("SELECT * FROM memory")


def test_unapproved_unknown_tool_fails_closed(settings):
    p = FakeProvider(answer(calls=[call("shell", {"command": "rm -rf /"})]))
    db, e, t = make(settings, p)
    e.run(t)
    assert db.task(t)["status"] == "failed"
    assert not db.all("SELECT * FROM tool_runs")


def test_manual_mode_requires_read_approval(settings):
    p = FakeProvider(answer(calls=[call("memory_read", {"key": "product"})]))
    db, e, t = make(settings, p)
    db.execute("UPDATE settings SET value='manual' WHERE key='autonomy'")
    e.run(t)
    assert db.task(t)["status"] == "waiting_approval"


def test_restart_requeues_work_without_external_intent(settings):
    p = FakeProvider()
    db, e, t = make(settings, p)
    e.recover()
    assert db.task(t)["status"] == "queued"
    assert e.claim() == t
    assert not p.calls


def test_step_limit_and_secret_redaction(settings):
    settings.max_steps = 1
    p = FakeProvider(answer(calls=[call("memory_read", {"key": "product"})]))
    db, e, t = make(settings, p)
    e.run(t)
    assert db.task(t)["status"] == "failed"
    assert "budget" in db.task(t)["error"]
    assert e._redact(settings.admin_password) == "[redacted]"


def test_cancel_during_model_does_not_complete(settings):
    p = FakeProvider()
    db, e, t = make(settings, p)

    def respond(*args):
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (t,))
        return answer("Must not complete")

    p.respond = respond
    e.run(t)
    assert db.task(t)["status"] == "cancelled"
    assert not db.all("SELECT * FROM events WHERE kind='completed'")


def test_scheduler_coalesces_and_obeys_mode(settings):
    db = Database(settings.data_dir / "scheduler.sqlite3")
    e = Engine(db, settings, FakeProvider())
    db.execute(
        "INSERT INTO schedules VALUES (?,?,?,?,?,1,?,?)",
        ("schedule", "Review", "Use facts", "strategist", 15, "2020-01-01T00:00:00+00:00", now()),
    )
    e.schedule_due()
    e.schedule_due()
    assert len(db.all("SELECT * FROM tasks")) == 1
    assert db.all("SELECT * FROM tasks")[0]["status"] == "draft"
    db.execute("UPDATE settings SET value='autonomous' WHERE key='autonomy'")
    db.execute("UPDATE schedules SET next_run_at='2020-01-01T00:00:00+00:00'")
    e.schedule_due()
    assert len(db.all("SELECT * FROM tasks WHERE status='queued'")) == 1


def test_claim_atomic_and_daily_limit(settings):
    settings.max_daily_runs = 1
    db = Database(settings.data_dir / "claims.sqlite3")
    e = Engine(db, settings, FakeProvider())
    db.create_task("One", "Task", "strategist", True)
    db.create_task("Two", "Task", "strategist", True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: e.claim(), range(4)))
    assert len([c for c in claims if c]) == 1
    assert e.claim() is None


def test_ambiguous_external_failure_does_not_retry(settings):
    settings.resend_api_key = "test-key"
    settings.mail_from = "owner@example.com"
    args = {"to": "person@example.com", "subject": "Hello", "body": "A message"}
    p = FakeProvider(answer(calls=[call("send_email", args)]))
    db, e, t = make(settings, p)
    e.run(t)
    approve(db, t)
    e.claim()

    def fail(*args):
        raise TimeoutError("Unknown delivery")

    settings.resend_api_key = "test-key"
    settings.mail_from = "owner@example.com"
    e.registry._request = fail
    e.run(t)
    assert db.task(t)["status"] == "failed"
    assert db.all("SELECT * FROM tool_runs")[0]["status"] == "started"
    e.recover()
    assert e.claim() is None
