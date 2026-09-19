import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent4good import conversations
from agent4good.db import Database
from agent4good.engine import Engine
from test_engine import FakeProvider, answer, call, make


def completed(db, title="First task", result="A private result"):
    tid = db.create_task(title, title + " instructions", "strategist", True)
    db.execute("UPDATE tasks SET status='done',result=? WHERE id=?", (result, tid))
    return tid


def test_followups_isolated_idempotent_and_serialized(settings):
    db = Database(settings.data_dir / "chat.sqlite3")
    root = completed(db)
    other = completed(db, "Other thread", "NEVER SHARE THIS")

    def send(_):
        return conversations.follow_up(db, root, "request-0001", "Explain the result", "ask")

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(send, range(4)))
    assert len(set(ids)) == 1
    assert "NEVER SHARE THIS" not in db.task(ids[0])["prompt"]
    assert db.task(root)["result"] == "A private result"
    with pytest.raises(ValueError, match="different content"):
        conversations.follow_up(db, root, "request-0001", "Different", "ask")
    with pytest.raises(ValueError, match="still active"):
        conversations.follow_up(db, root, "request-0002", "Next", "ask")
    assert len(conversations.transcript(db, other)["messages"]) == 2
    db.execute("UPDATE tasks SET status='done',result='Explanation' WHERE id=?", (ids[0],))
    next_id = conversations.follow_up(db, ids[0], "request-0002", "Now improve it", "work")
    assert "Explanation" in db.task(next_id)["prompt"]
    assert conversations.transcript(db, next_id)["root_id"] == root


def test_readonly_mode_rejects_model_tool_calls(settings):
    db = Database(settings.data_dir / "chat.sqlite3")
    root = completed(db)
    tid = conversations.follow_up(db, root, "request-0001", "Explain", "ask")
    p = FakeProvider(
        answer(calls=[call("artifact_write", {"name": "bad.md", "content": "bad"})]), answer("No action")
    )
    e = Engine(db, settings, p)
    assert e.claim() == tid
    e.run(tid)
    assert not db.all("SELECT * FROM artifacts")
    assert p.calls[0][2] == []
    assert db.task(tid)["status"] == "failed"
    assert "read-only" in db.task(tid)["error"]


def test_question_pauses_and_resumes_after_restart(settings):
    p = FakeProvider(answer(calls=[call("ask_owner", {"question": "Which audience?"})]))
    db, e, tid = make(settings, p)
    e.run(tid)
    assert db.task(tid)["status"] == "waiting_input"
    assert len(p.calls) == 1
    q = db.one("SELECT * FROM owner_questions WHERE task_id=?", (tid,))
    assert db.one("SELECT * FROM tool_runs WHERE task_id=?", (tid,))["status"] == "done"
    conversations.answer(db, tid, q["id"], "Independent developers")
    conversations.answer(db, tid, q["id"], "Independent developers")
    with pytest.raises(ValueError, match="different answer"):
        conversations.answer(db, tid, q["id"], "Someone else")
    resumed = FakeProvider(answer("Built for independent developers"))
    e2 = Engine(Database(db.path), settings, resumed)
    assert e2.claim() == tid
    e2.run(tid)
    assert db.task(tid)["status"] == "done"
    assert "Independent developers" in json.dumps(resumed.calls[0][1])
    assert len(db.all("SELECT * FROM owner_questions")) == 1
    assert len(db.all("SELECT * FROM tool_runs")) == 1
    assert len(conversations.transcript(db, tid)["messages"]) == 4


def test_cancelled_question_cannot_resume_and_wrong_thread_rejected(settings):
    db, e, tid = make(settings, FakeProvider(answer(calls=[call("ask_owner", {"question": "Which name?"})])))
    e.run(tid)
    q = db.one("SELECT * FROM owner_questions")
    with pytest.raises(ValueError, match="not found"):
        conversations.answer(db, "wrong-task", q["id"], "Name")
    db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
    with pytest.raises(ValueError, match="not waiting"):
        conversations.answer(db, tid, q["id"], "Name")
    assert not any(m.get("needs_answer") for m in conversations.transcript(db, tid)["messages"])


def test_chat_api_auth_validation_and_persistence(app, client, owner, settings):
    settings.openai_api_key = "test-key-never-used"
    root = completed(app.state.db)
    assert owner.get("/api/chats").status_code == 200
    route = f"/api/tasks/{root}/chat"
    payload = {"request_id": "request-0001", "content": "What happened?", "mode": "ask"}
    no_csrf = owner.post(route, json=payload, headers={"X-CSRF-Token": ""})
    assert no_csrf.status_code == 403
    assert owner.post(route, json={**payload, "mode": "unsafe"}).status_code == 422
    sent = owner.post(route, json=payload)
    assert sent.status_code == 201, sent.text
    assert owner.post(route, json=payload).json() == sent.json()
    assert owner.get(route).json()["messages"][-1]["content"] == "What happened?"
    assert owner.post("/api/logout", json={}).status_code == 200
    assert client.get(route).status_code == 401


def test_work_cannot_escape_active_task(settings):
    db = Database(settings.data_dir / "chat.sqlite3")
    tid = db.create_task("Active", "Work", "strategist", True)
    with pytest.raises(ValueError, match="Finish or stop"):
        conversations.follow_up(db, tid, "request-0001", "More work", "work")
    assert conversations.follow_up(db, tid, "request-0002", "Status?", "ask")


def test_pending_question_recovery_before_deadline_and_no_later_tool(settings):
    p = FakeProvider(
        answer(
            calls=[
                call("ask_owner", {"question": "Choose one?"}, "ask"),
                call("artifact_write", {"name": "after.md", "content": "Only after answer"}, "later"),
            ]
        )
    )
    db, e, tid = make(settings, p)
    e.run(tid)
    assert db.task(tid)["status"] == "waiting_input"
    assert not db.all("SELECT * FROM artifacts")
    # Simulate recovery after checkpoint, before the worker parked the task.
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    db.execute("UPDATE executions SET started_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (tid,))
    Engine(db, settings, FakeProvider()).run(tid)
    assert db.task(tid)["status"] == "waiting_input"
    q = db.one("SELECT * FROM owner_questions")
    conversations.answer(db, tid, q["id"], "One")
    resumed = Engine(db, settings, FakeProvider(answer("Done after your answer")))
    assert resumed.claim() == tid
    resumed.run(tid)
    assert db.task(tid)["status"] == "done"
    assert len(db.all("SELECT * FROM artifacts")) == 1


def test_followup_inherits_root_policy_and_worker_tool_scope(settings):
    from agent4good.policy import PolicyDocument

    db = Database(settings.data_dir / "chat.sqlite3")
    root = completed(db)
    db.execute(
        "INSERT INTO worker_nodes VALUES (?,?,?,?,?,?)",
        (root, None, root, 0, 0, json.dumps(["artifact_write"])),
    )
    tid = conversations.follow_up(db, root, "request-0001", "Do work", "work")
    e = Engine(
        db,
        settings,
        FakeProvider(answer(calls=[call("artifact_write", {"name": "blocked.md", "content": "blocked"})])),
    )
    e.registry.policy.replace(
        PolicyDocument.model_validate(
            {
                "rules": [
                    {
                        "id": "no-root-write",
                        "scope": {"task": root, "tool": "artifact_write"},
                        "effect": "deny",
                    }
                ]
            }
        ),
        expected_revision=1,
    )
    assert json.loads(db.one("SELECT tools FROM worker_nodes WHERE task_id=?", (tid,))["tools"]) == [
        "artifact_write"
    ]
    assert e.claim() == tid
    e.run(tid)
    assert db.task(tid)["status"] == "failed"
    assert not db.all("SELECT * FROM artifacts")


def test_question_does_not_inherit_work_quality_defaults(settings):
    from agent4good.quality import QualityInput
    from test_quality import contract

    settings.quality_defaults = {"planning": QualityInput.model_validate(contract())}
    db = Database(settings.data_dir / "chat.sqlite3")
    root = completed(db)
    tid = conversations.follow_up(db, root, "request-0001", "Explain the result", "ask")
    e = Engine(db, settings, FakeProvider(answer(text="The saved result explains the work.")))
    assert e.claim() == tid
    e.run(tid)
    assert db.task(tid)["status"] == "done"
    assert not db.all("SELECT * FROM quality_contracts WHERE task_id=?", (tid,))


def test_followup_question_answers_remain_in_context(settings):
    db = Database(settings.data_dir / "chat.sqlite3")
    root = completed(db)
    tid = conversations.follow_up(db, root, "request-0001", "Revise this", "work")
    e = Engine(db, settings, FakeProvider(answer(calls=[call("ask_owner", {"question": "Which revision?"})])))
    assert e.claim() == tid
    e.run(tid)
    q = db.one("SELECT * FROM owner_questions WHERE task_id=?", (tid,))
    conversations.answer(db, tid, q["id"], "Use the cobalt edition")
    db.execute("UPDATE tasks SET status='done',result='Saved' WHERE id=?", (tid,))
    followup = conversations.follow_up(db, root, "request-0002", "Which edition did I choose?", "ask")
    assert "Use the cobalt edition" in db.task(followup)["prompt"]
