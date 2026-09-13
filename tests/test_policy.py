"""Policy acceptance through real registry, engine, owner API and SQLite transactions."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent4good.db import Database
from agent4good.policy import CLASSES, PolicyError
from agent4good.tools import SPECS, ToolError, ToolRegistry
from test_engine import FakeProvider, answer, approve, call, make
from test_tools import permit, runnable


def install(r, **document):
    revision = r.db.one("SELECT revision FROM action_policy")["revision"]
    return r.policy.replace(document, revision)


def execute(r, task, call_id="one"):
    return r.execute("memory_read", {"key": "missing"}, task_id=task, call_id=call_id)


@pytest.mark.parametrize("scope", ["user", "agent", "task", "tool", "environment", "action"])
def test_every_scope_controls_real_invocation(settings, scope):
    r, task = runnable(settings)
    actual = dict(
        user="owner", agent="strategist", task=task, tool="memory_read", environment="local", action="READ"
    )
    install(r, rules=[dict(id="deny", scope={scope: actual[scope]}, effect="deny")])
    with pytest.raises(ToolError, match="denied"):
        execute(r, task)
    other = "WRITE" if scope == "action" else "other"
    install(r, rules=[dict(id="deny", scope={scope: other}, effect="deny")])
    assert execute(r, task) == {"found": False}


@pytest.mark.parametrize("action", CLASSES)
def test_every_class_is_enforced_at_execution(settings, monkeypatch, action):
    r, task = runnable(settings)
    monkeypatch.setitem(SPECS, "memory_read", replace(SPECS["memory_read"], action_class=action))
    install(r, rules=[dict(id="class", scope={"action": action}, effect="deny")])
    with pytest.raises(ToolError, match="denied"):
        execute(r, task)
    assert not r.db.all("SELECT * FROM tool_runs")


@pytest.mark.parametrize("effect", ["approval", "conditional_approval", "escalation"])
def test_engine_owner_approval_full_flow(settings, effect):
    args = {"key": "missing"}
    db, engine, task = make(
        settings, FakeProvider(answer(calls=[call("memory_read", args)]), answer("Checked"))
    )
    rule = dict(id="gate", effect=effect, conditions={"argument_equals": {"key": "missing"}})
    install(engine.registry, rules=[rule])
    engine.run(task)
    assert db.task(task)["status"] == "waiting_approval"
    assert db.one("SELECT effect FROM policy_approvals")["effect"] == effect
    assert not db.all("SELECT * FROM tool_runs")
    approve(db, task)
    assert engine.claim() == task
    engine.run(task)
    assert db.task(task)["status"] == "done"
    assert len(db.all("SELECT * FROM tool_runs")) == 1


def test_conditions_deny_instead_of_falling_through(settings):
    r, task = runnable(settings)
    install(
        r,
        rules=[
            dict(
                id="gate", effect="conditional_approval", conditions={"argument_equals": {"key": "allowed"}}
            ),
            dict(id="broad", effect="allow"),
        ],
    )
    with pytest.raises(ToolError, match="denied"):
        execute(r, task)


def test_default_floor_and_deny_precedence(settings):
    r, task = runnable(settings)
    args = {"key": "test", "content": "Must stay approved"}
    install(r, rules=[dict(id="allow", effect="allow")])
    with pytest.raises(ToolError, match="Approval"):
        r.execute("memory_write", args, task_id=task, call_id="one")
    install(r, rules=[dict(id="deny", effect="deny"), dict(id="allow", effect="allow", scope={"task": task})])
    with pytest.raises(ToolError, match="denied"):
        execute(r, task)


@pytest.mark.parametrize(
    "change", ["arguments", "agent", "owner", "environment", "policy", "expiry", "signature", "sender"]
)
def test_approval_tampering_and_stale_scope_never_execute(settings, change):
    args = {"key": "test", "content": "Original"}
    db, engine, task = make(settings, FakeProvider(answer(calls=[call("memory_write", args)])))
    engine.run(task)
    approve(db, task)
    if change == "arguments":
        pending = json.loads(db.task(task)["pending"])
        pending[0]["arguments"] = json.dumps({**args, "content": "Tampered"})
        db.execute("UPDATE tasks SET pending=? WHERE id=?", (json.dumps(pending), task))
        # Altering both records still cannot forge the signed binding.
        db.execute("UPDATE approvals SET arguments=?", (pending[0]["arguments"],))
    elif change == "agent":
        db.execute("UPDATE tasks SET agent='research_analyst' WHERE id=?", (task,))
    elif change == "owner":
        db.execute("UPDATE tasks SET owner_id='other' WHERE id=?", (task,))
    elif change == "environment":
        settings.environment = "production"
    elif change == "sender":
        settings.mail_from = "changed@example.com"
    elif change == "policy":
        install(engine.registry)
    elif change == "expiry":
        db.execute("UPDATE policy_approvals SET expires=0")
    elif change == "signature":
        db.execute("UPDATE policy_approvals SET signature='forged'")
    engine.claim()
    engine.run(task)
    assert db.task(task)["status"] == "failed"
    assert not db.all("SELECT * FROM memory")
    assert not db.all("SELECT * FROM tool_runs")


def test_policy_change_between_reservation_and_dispatch(settings, monkeypatch):
    r, task = runnable(settings)
    original = r.policy.evaluate
    evaluations = []

    def evaluate(conn, *args):
        evaluations.append(1)
        if len(evaluations) == 2:
            conn.execute("UPDATE action_policy SET revision=revision+1")
        return original(conn, *args)

    monkeypatch.setattr(r.policy, "evaluate", evaluate)
    with pytest.raises(ToolError, match="changed before dispatch"):
        execute(r, task)
    assert r.db.one("SELECT status FROM tool_runs")["status"] == "started"


@pytest.mark.parametrize("limit", ["calls", "spend_microusd", "recipients"])
def test_concurrent_workers_share_caps_and_replay_costs_nothing(settings, monkeypatch, limit):
    settings.resend_api_key, settings.mail_from = "fake-resend-credential", "owner@example.com"
    r, _ = runnable(settings)
    install(r, limits={limit: 2}, tool_costs_microusd={"send_email": 1})
    tasks = [r.db.create_task("Tagged test", "No real send", "strategist") for _ in range(8)]
    args = {"to": "test@example.com", "subject": "Test", "body": "Simulated transport"}
    for task in tasks:
        r.db.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
        permit(r, task, "send_email", args)
    sent = []
    monkeypatch.setattr(ToolRegistry, "_request", lambda *a: sent.append(1) or {"id": "simulated"})

    def invoke(task):
        registry = ToolRegistry(settings, r.db)
        try:
            return registry.execute("send_email", args, task_id=task, call_id="check")
        except ToolError as exc:
            assert "limit" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(invoke, tasks))
    assert sum(result is not None for result in outcomes) == 2
    assert len(sent) == 2
    winner = tasks[next(i for i, result in enumerate(outcomes) if result)]
    assert invoke(winner) == {"message_id": "simulated"}
    assert len(sent) == 2
    assert len(r.db.all("SELECT * FROM policy_usage")) == 2


def test_rule_limit_survives_policy_edits_and_failures(settings, monkeypatch):
    r, task = runnable(settings)
    rule = dict(id="same-budget", effect="allow", limits={"calls": 1})
    install(r, rules=[rule])
    monkeypatch.setattr(r, "_dispatch", lambda *a: None)  # invalid output, possibly acted already
    with pytest.raises(ToolError, match="output"):
        execute(r, task)
    install(r, rules=[rule])
    with pytest.raises(ToolError, match="limit"):
        execute(ToolRegistry(settings, r.db), task, "two")
    assert len(r.db.all("SELECT * FROM tool_runs")) == 1


def test_recipient_and_cost_conditions(settings):
    settings.resend_api_key, settings.mail_from = "fake-resend-credential", "owner@example.com"
    r, task = runnable(settings)
    args = {"to": "other@example.com", "subject": "Test", "body": "Never sent"}
    install(
        r,
        tool_costs_microusd={"send_email": 2},
        rules=[
            dict(
                id="bounded",
                effect="conditional_approval",
                conditions={"recipients": ["allowed@example.com"], "max_cost_microusd": 1},
            )
        ],
    )
    permit(r, task, "send_email", args)
    with pytest.raises(ToolError, match="denied"):
        r.execute("send_email", args, task_id=task, call_id="check")


def test_policy_owner_api_validation_csrf_and_conflicts(client, owner):
    current = owner.get("/api/policy").json()
    assert current["revision"] == 1
    payload = dict(expected_revision=1, document={"rules": [{"id": "test", "effect": "deny"}]})
    assert owner.put("/api/policy", json=payload).json()["revision"] == 2
    assert owner.put("/api/policy", json=payload).status_code == 409
    assert (
        owner.put("/api/policy", json={"expected_revision": 2, "document": {"surprise": True}}).status_code
        == 422
    )
    owner.headers.pop("X-CSRF-Token")
    assert owner.put("/api/policy", json=payload).status_code == 403
    client.cookies.clear()
    assert client.get("/api/policy").status_code == 401


def test_schema_migration_preserves_only_preexisting_approvals(settings):
    # Build a genuine old schema by removing only the newly introduced policy tables.
    r, task = runnable(settings)
    args = {"key": "test", "content": "Legacy approved content"}
    permit(r, task, "memory_write", args)
    with r.db.connect() as conn:
        for table in ["policy_approvals", "policy_legacy", "policy_usage", "action_policy"]:
            conn.execute("DROP TABLE " + table)
        conn.execute("ALTER TABLE tasks DROP COLUMN owner_id")
        conn.execute("PRAGMA user_version=1")
    db = Database(settings.data_dir / "tools.sqlite3")
    upgraded = ToolRegistry(settings, db)
    assert upgraded.execute("memory_write", args, task_id=task, call_id="check") == {"saved": "test"}
    assert db.one("SELECT owner_id FROM tasks")["owner_id"] == "owner"
    assert db.one("SELECT status FROM approvals")["status"] == "approved"
    # Reopening cannot extend expiry or rebind changed content.
    expiry = db.one("SELECT expires FROM policy_approvals")["expires"]
    ToolRegistry(settings, Database(settings.data_dir / "tools.sqlite3"))
    assert db.one("SELECT expires FROM policy_approvals")["expires"] == expiry
    db.execute(
        "INSERT INTO approvals(id,task_id,call_id,tool,arguments,status,created_at) VALUES ('forged',?,'new','memory_write',?,'approved','now')",
        (task, json.dumps(args)),
    )
    with pytest.raises(ToolError, match="no policy binding"):
        upgraded.execute("memory_write", args, task_id=task, call_id="new")


def test_invalid_documents_do_not_replace_active_policy(settings):
    r, task = runnable(settings)
    with pytest.raises(ValueError):
        install(r, rules=[{"id": "bad", "effect": "conditional_approval"}])
    with pytest.raises(ValueError):
        install(r, tool_costs_microusd={"memory_read": -1})
    with pytest.raises(PolicyError):
        r.policy.replace({}, 999)
    assert execute(r, task) == {"found": False}


def test_unknown_class_fails_closed(settings, monkeypatch):
    r, task = runnable(settings)
    monkeypatch.setitem(SPECS, "memory_read", replace(SPECS["memory_read"], action_class="invented"))
    with pytest.raises(ToolError, match="denied"):
        execute(r, task)


def test_owner_api_resumes_escalation_and_records_no_content(owner, app, settings):
    from agent4good.engine import Engine

    db = app.state.db
    document = {"rules": [{"id": "escalate", "effect": "escalation"}]}
    assert owner.put("/api/policy", json={"expected_revision": 1, "document": document}).status_code == 200
    task = owner.post("/api/tasks", json={"title": "Tagged policy test", "prompt": "Use test data"}).json()[
        "id"
    ]
    db.execute("UPDATE tasks SET status='queued' WHERE id=?", (task,))
    args = {"name": "test.txt", "content": "private-body-never-in-audit"}
    engine = Engine(
        db, settings, FakeProvider(answer(calls=[call("artifact_write", args)]), answer("Saved test"))
    )
    assert engine.claim() == task
    engine.run(task)
    approval = owner.get("/api/approvals").json()[0]
    assert approval["policy"]["effect"] == "escalation"
    assert (
        owner.post("/api/approvals/" + approval["id"] + "/decision", json={"decision": "approve"}).status_code
        == 200
    )
    assert engine.claim() == task
    engine.run(task)
    assert db.task(task)["status"] == "done"
    assert len(db.all("SELECT * FROM artifacts")) == 1
    assert "private-body-never-in-audit" not in json.dumps(db.all("SELECT * FROM events"))


def test_github_communications_use_recipient_budget(settings, monkeypatch):
    settings.github_repo, settings.github_token = "owner/repo", "fake-github-credential"
    r, task = runnable(settings)
    install(r, limits={"recipients": 0})
    args = {"title": "Tagged test", "body": "Must not send"}
    permit(r, task, "github_create_issue", args)
    with pytest.raises(ToolError, match="limit"):
        r.execute("github_create_issue", args, task_id=task, call_id="check")
    assert not r.db.all("SELECT * FROM tool_runs")
