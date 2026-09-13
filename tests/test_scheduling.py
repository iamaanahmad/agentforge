"""Real storage and workers; models are scripted and no external effects run."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent4good import scheduling as sch
from agent4good.db import Database, now
from agent4good.engine import Engine
from agent4good.tools import ToolRegistry
from test_engine import FakeProvider, answer, call


def setup(settings):
    db = Database(settings.data_dir / "scheduling.sqlite3")
    db.execute("UPDATE settings SET value='autonomous' WHERE key='autonomy'")
    return db, ToolRegistry(settings, db)


def create(db, **changes):
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return sch.create(
            conn, sch.ScheduleInput(name="[TEST] scheduled evidence", prompt="Use supplied facts", **changes)
        )


def due(db, sid):
    db.execute("UPDATE schedules SET next_run_at=? WHERE id=?", ("2020-01-01T00:00:00+00:00", sid))


def tasks(db, sid):
    return db.all(
        "SELECT t.* FROM tasks t JOIN schedule_occurrences o ON o.task_id=t.id WHERE o.schedule_id=?", (sid,)
    )


@pytest.mark.parametrize("mode", ["once", "recurring", "deadline", "event", "condition", "interval"])
def test_each_mode_runs_real_worker(settings, mode):
    db, registry = setup(settings)
    fields = {"mode": mode}
    if mode in {"once", "deadline"}:
        fields["at"] = "2020-01-01T00:00:00+00:00"
    if mode == "condition":
        target = db.create_task("dependency", "facts", "strategist")
        db.execute("UPDATE tasks SET status='done' WHERE id=?", (target,))
        fields["condition"] = {"task_id": target}
    sid = create(db, **fields)
    due(db, sid)
    if mode == "event":
        with db.connect() as conn:
            sch.event(conn, sid, "event-1")
    assert sch.dispatch(db, settings, registry) == 1
    assert sch.dispatch(db, settings, registry) == 0
    engine = Engine(db, settings, FakeProvider(answer("Evidence from scripted model")))
    tid = engine.claim()
    assert tid == tasks(db, sid)[0]["id"]
    engine.run(tid)
    assert db.task(tid)["status"] == "done"
    assert db.task(tid)["result"] == "Evidence from scripted model"


def test_dependency_priority_and_failure(settings):
    db, registry = setup(settings)
    dep = db.create_task("Prerequisite", "facts", "strategist")
    waiting = create(db, mode="once", at=now(), depends_on=[dep], priority=9)
    lower = create(db, mode="once", at=now(), priority=-2)
    assert sch.dispatch(db, settings, registry) == 1
    assert not tasks(db, waiting)
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (dep,))
    assert sch.dispatch(db, settings, registry) == 1
    assert Engine(db, settings, FakeProvider()).claim() == tasks(db, waiting)[0]["id"]
    assert tasks(db, lower)[0]["status"] == "queued"
    fail = db.create_task("Failed source", "facts", "strategist")
    db.execute("UPDATE tasks SET status='failed' WHERE id=?", (fail,))
    sid = create(db, mode="once", at=now(), depends_on=[fail])
    sch.dispatch(db, settings, registry)
    assert not tasks(db, sid)
    assert (
        db.one("SELECT status FROM schedule_occurrences WHERE schedule_id=?", (sid,))["status"]
        == "dependency_failed"
    )


def test_restarts_races_and_event_replays(settings):
    db, registry = setup(settings)
    sid = create(db, mode="event", overlap="allow")

    def ingest(_):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return sch.event(conn, sid, "delivery-1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(ingest, range(16))) == 1

    def tick(_):
        other = Database(db.path)
        return sch.dispatch(other, settings, ToolRegistry(settings, other))

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(tick, range(16))) == 1
    assert len(tasks(db, sid)) == 1
    assert tick(1) == 0
    assert len(db.all("SELECT * FROM schedule_occurrences")) == 1


def test_transaction_crash_rolls_back_task_and_occurrence(settings, monkeypatch):
    db, registry = setup(settings)
    sid = create(db, mode="once", at=now())
    original = db.create_task

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Injected process interruption before commit")

    monkeypatch.setattr(db, "create_task", crash)
    with pytest.raises(RuntimeError):
        sch.dispatch(db, settings, registry)
    assert not tasks(db, sid)
    assert not db.all("SELECT * FROM tasks")
    assert not db.all("SELECT * FROM schedule_occurrences")
    monkeypatch.setattr(db, "create_task", original)
    assert sch.dispatch(db, settings, registry) == 1


def test_calendar_dst_and_offsets():
    spec = sch.ScheduleInput(
        name="Daily", prompt="Facts", mode="recurring", timezone="America/New_York", local_time="02:30"
    )
    assert sch.calendar_next(spec, sch.instant("2026-03-08T00:00:00-05:00")) == "2026-03-09T06:30:00+00:00"
    spec.local_time = "01:30"
    assert sch.calendar_next(spec, sch.instant("2026-11-01T00:00:00-04:00")) == "2026-11-01T05:30:00+00:00"
    assert sch.calendar_next(spec, sch.instant("2026-11-01T05:30:00+00:00")) == "2026-11-02T06:30:00+00:00"
    with pytest.raises(ValidationError):
        sch.ScheduleInput(name="Bad", prompt="Facts", mode="once", at="2026-10-01T09:00:00")
    assert sch.instant("2026-10-01T09:00:00+02:00").hour == 7


def test_missed_deadline_overlap_and_max_runs(settings):
    db, registry = setup(settings)
    missed = create(db, mode="once", at="2020-01-01T00:00:00Z", missed="skip")
    expired = create(db, mode="deadline", at="2020-01-01T00:00:00Z", deadline="2020-01-02T00:00:00Z")
    sid = create(db, mode="interval", max_runs=2)
    due(db, sid)
    assert sch.dispatch(db, settings, registry) == 1
    assert not tasks(db, missed) and not tasks(db, expired)
    # A new occurrence waits for the active task to finish.
    db.execute("UPDATE schedules SET next_run_at='2020-01-02T00:00:00+00:00' WHERE id=?", (sid,))
    assert sch.dispatch(db, settings, registry) == 0
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (tasks(db, sid)[0]["id"],))
    assert sch.dispatch(db, settings, registry) == 1
    due(db, sid)
    assert sch.dispatch(db, settings, registry) == 0
    assert not db.one("SELECT enabled FROM schedules WHERE id=?", (sid,))["enabled"]


def test_condition_polling_and_rising_edge(settings):
    db, registry = setup(settings)
    target = db.create_task("Condition source", "Facts", "strategist")
    create(db, mode="condition", condition={"task_id": target}, overlap="allow")
    t = datetime.now(timezone.utc)
    assert sch.dispatch(db, settings, registry, t) == 0
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (target,))
    assert sch.dispatch(db, settings, registry, t + timedelta(minutes=1)) == 0
    assert sch.dispatch(db, settings, registry, t + timedelta(days=1)) == 1
    assert sch.dispatch(db, settings, registry, t + timedelta(days=2)) == 0


def install_policy(registry, deny=False):
    registry.policy.replace(
        {
            "rules": [
                {
                    "id": "schedule",
                    "scope": {"tool": "schedule_create"},
                    "effect": "deny" if deny else "allow",
                }
            ]
        },
        registry.db.one("SELECT revision FROM action_policy")["revision"],
    )


def agent_schedule(settings, db, registry):
    install_policy(registry)
    request = sch.ScheduleInput(
        name="Agent future work", prompt="Facts", mode="once", at=now()
    ).model_dump_json()
    engine = Engine(
        db,
        settings,
        FakeProvider(answer(calls=[call("schedule_create", {"request": request})]), answer("Scheduled")),
    )
    tid = db.create_task("Plan future work", "Schedule bounded work", "strategist", True)
    assert engine.claim() == tid
    engine.run(tid)
    assert db.task(tid)["status"] == "done", db.task(tid)
    return tid, db.one("SELECT schedule_id FROM schedule_definitions")["schedule_id"]


def test_agent_scope_completion_inheritance_revocation(settings):
    db, registry = setup(settings)
    tid = db.create_task("No permission", "Facts", "strategist", True)
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    args = {"request": json.dumps({"name": "Denied", "prompt": "Facts"})}
    with pytest.raises(sch.ScheduleError):
        registry.execute("schedule_create", args, task_id=tid, call_id="denied")
    assert not db.all("SELECT * FROM schedules")
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    creator, sid = agent_schedule(settings, db, registry)
    assert sch.dispatch(db, settings, registry) == 1
    child = tasks(db, sid)[0]["id"]
    # Creator completed, but its task-scoped deny still restricts scheduled outputs.
    registry.policy.replace(
        {"rules": [{"id": "deny", "scope": {"task": creator, "tool": "artifact_write"}, "effect": "deny"}]}, 2
    )
    e = Engine(
        db,
        settings,
        FakeProvider(
            answer(calls=[call("artifact_write", {"name": "x.md", "content": "no"})]), answer("Done")
        ),
    )
    assert e.claim() == child
    e.run(child)
    assert not db.all("SELECT * FROM artifacts")
    with db.connect() as conn:
        assert sch.origins(conn, child)[0]["id"] == creator


def test_agent_policy_revoked_before_dispatch(settings):
    db, registry = setup(settings)
    _, sid = agent_schedule(settings, db, registry)
    install_policy(registry, deny=True)
    assert sch.dispatch(db, settings, registry) == 0
    assert not tasks(db, sid)
    assert db.one("SELECT status FROM schedule_occurrences")["status"] == "policy_denied"


def test_cancellation_stops_future_and_active_tasks(settings):
    db, registry = setup(settings)
    sid = create(db, mode="event", overlap="allow")
    with db.connect() as conn:
        sch.event(conn, sid, "one")
        sch.event(conn, sid, "two")
    sch.dispatch(db, settings, registry)
    tid = tasks(db, sid)[0]["id"]
    assert Engine(db, settings, FakeProvider()).claim() == tid
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sch.control(conn, sid, cancel=True)
    assert db.task(tid)["status"] == "cancelled"
    assert sch.dispatch(db, settings, registry) == 0
    with db.connect() as conn, pytest.raises(sch.ScheduleError):
        sch.control(conn, sid, enabled=True)


def test_authenticated_api(owner, client, app):
    r = owner.post("/api/schedules", json={"name": "[TEST] event", "prompt": "Facts", "mode": "event"})
    assert r.status_code == 201
    sid = r.json()["id"]
    path = f"/api/schedules/{sid}/events"
    assert owner.post(path, json={"event_id": "one"}).json() == {"accepted": True}
    assert owner.post(path, json={"event_id": "one"}).json() == {"accepted": False}
    assert owner.post(path, json={"event_id": "bad id"}).status_code == 422
    assert owner.get(f"/api/schedules/{sid}").status_code == 200
    assert owner.post(f"/api/schedules/{sid}/cancel", json={}).status_code == 200
    assert owner.post(path, json={"event_id": "two"}).status_code == 409
    owner.cookies.clear()
    assert client.post(path, json={"event_id": "three"}).status_code == 401


def test_process_crash_and_restart(settings):
    import subprocess
    import sys

    db, registry = setup(settings)
    sid = create(db, mode="once", at=now())
    # A genuinely killed process writes a task inside a transaction, then exits without commit.
    script = """
import os,sys
from agent4good.db import Database
from agent4good.config import Settings
from agent4good.tools import ToolRegistry
from agent4good.scheduling import dispatch
s=Settings(_env_file=None,secure_cookies=False,data_dir=sys.argv[1],admin_password='test-password-only-123',session_secret='test-session-secret-not-for-deployment-1234')
db=Database(sys.argv[2]); registry=ToolRegistry(s,db); original=db.create_task
def crash(*a,**k):
    original(*a,**k)
    os._exit(17)
db.create_task=crash
dispatch(db,s,registry)
"""
    result = subprocess.run([sys.executable, "-c", script, str(settings.data_dir), db.path], timeout=20)
    assert result.returncode == 17
    reopened = Database(db.path)
    assert not reopened.all("SELECT * FROM tasks")
    assert sch.dispatch(reopened, settings, ToolRegistry(settings, reopened)) == 1
    assert len(tasks(reopened, sid)) == 1


def test_separate_scheduler_processes(settings):
    import subprocess
    import sys

    db, _ = setup(settings)
    sid = create(db, mode="once", at=now())
    script = """
import sys
from agent4good.db import Database
from agent4good.config import Settings
from agent4good.tools import ToolRegistry
from agent4good.scheduling import dispatch
s=Settings(_env_file=None,secure_cookies=False,data_dir=sys.argv[1],admin_password='test-password-only-123',session_secret='test-session-secret-not-for-deployment-1234')
db=Database(sys.argv[2]); dispatch(db,s,ToolRegistry(s,db))
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(settings.data_dir), db.path]) for _ in range(4)
    ]
    assert [p.wait(timeout=30) for p in processes] == [0] * 4
    assert len(tasks(db, sid)) == 1


def test_existing_schedule_migration_preserves_pause_and_interval(settings):
    db, registry = setup(settings)
    db.execute(
        "INSERT INTO schedules VALUES (?,?,?,?,?,0,?,?)",
        ("old", "Legacy", "Facts", "strategist", 15, "2020-01-01T00:00:00+00:00", now()),
    )
    db = Database(db.path)
    definition = db.one("SELECT * FROM schedule_definitions WHERE schedule_id='old'")
    assert json.loads(definition["definition"])["_legacy"]
    assert sch.dispatch(db, settings, registry) == 0
    db.execute("UPDATE schedules SET enabled=1 WHERE id='old'")
    db.execute("UPDATE schedule_definitions SET runs=1001 WHERE schedule_id='old'")
    assert sch.dispatch(db, settings, registry) == 1
    assert tasks(db, "old")


def test_scope_limits_nested_scheduling_and_cancel_receipt(settings):
    db, registry = setup(settings)
    creator, sid = agent_schedule(settings, db, registry)
    assert sch.dispatch(db, settings, registry) == 1
    child = tasks(db, sid)[0]["id"]
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
    with pytest.raises(sch.ScheduleError, match="independent root"):
        registry.execute(
            "schedule_create",
            {"request": sch.ScheduleInput(name="Nested", prompt="No").model_dump_json()},
            task_id=child,
            call_id="nested",
        )
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (creator,))
    args = {"schedule_id": sid}
    result = registry.execute("schedule_cancel", args, task_id=creator, call_id="cancel")
    assert registry.execute("schedule_cancel", args, task_id=creator, call_id="cancel") == result
    assert db.task(child)["status"] == "cancelled"
    assert len(db.all("SELECT * FROM tool_runs WHERE tool='schedule_cancel'")) == 1
    with db.connect() as conn, pytest.raises(sch.ScheduleError, match="at most 100"):
        sch.create(conn, sch.ScheduleInput(name="Too many", prompt="No", max_runs=101), creator)


def test_csrf_event_and_foreign_dependency_denied(owner, app):
    sid = owner.post("/api/schedules", json={"name": "Event", "prompt": "Facts", "mode": "event"}).json()[
        "id"
    ]
    token = owner.headers.pop("X-CSRF-Token")
    assert owner.post(f"/api/schedules/{sid}/events", json={"event_id": "one"}).status_code == 403
    owner.headers["X-CSRF-Token"] = token
    task = app.state.db.create_task("Foreign", "No", "strategist")
    app.state.db.execute("UPDATE tasks SET owner_id='other' WHERE id=?", (task,))
    assert (
        owner.post("/api/schedules", json={"name": "No", "prompt": "No", "depends_on": [task]}).status_code
        == 409
    )


def test_agent_budget_exhaustion_isolated(settings):
    db, registry = setup(settings)
    _, sid = agent_schedule(settings, db, registry)
    registry.policy.replace(
        {
            "rules": [
                {
                    "id": "limited",
                    "scope": {"tool": "schedule_create"},
                    "effect": "allow",
                    "limits": {"calls": 1},
                }
            ]
        },
        2,
    )
    # Exhaust just the rule budget; a failed reservation must roll back the global debit.
    import time

    db.execute(
        "INSERT INTO policy_usage(bucket,created_at,calls,cost,recipients) VALUES (?,?,1,0,0)",
        ("rule:limited", time.time()),
    )
    before = db.one("SELECT COUNT(*) AS n FROM policy_usage")["n"]
    owner = create(db, mode="once", at=now())
    assert sch.dispatch(db, settings, registry) == 1
    assert len(tasks(db, owner)) == 1
    assert not tasks(db, sid)
    assert (
        db.one("SELECT status FROM schedule_occurrences WHERE schedule_id=?", (sid,))["status"]
        == "policy_denied"
    )
    assert db.one("SELECT COUNT(*) AS n FROM policy_usage")["n"] == before


@pytest.mark.parametrize("mode", ["event", "condition"])
def test_deadline_expires_without_event_or_true_condition(settings, mode):
    db, registry = setup(settings)
    fields = {"mode": mode, "deadline": "2020-01-01T00:00:00+00:00"}
    if mode == "condition":
        fields["condition"] = {"task_id": db.create_task("False condition", "Facts", "strategist")}
    sid = create(db, **fields)
    assert sch.dispatch(db, settings, registry) == 0
    assert (
        db.one("SELECT status FROM schedule_occurrences WHERE schedule_id=?", (sid,))["status"] == "expired"
    )
    assert db.one("SELECT enabled FROM schedules WHERE id=?", (sid,))["enabled"] == 0


def test_resume_respects_active_schedule_cap(settings):
    db, registry = setup(settings)
    sid = create(db)
    with db.connect() as conn:
        sch.control(conn, sid, enabled=False)
    for _ in range(100):
        create(db)
    with db.connect() as conn:
        with pytest.raises(sch.ScheduleError, match="Active schedule limit"):
            sch.control(conn, sid, enabled=True)
    assert db.one("SELECT enabled FROM schedules WHERE id=?", (sid,))["enabled"] == 0


def test_legacy_date_only_occurrence_migrates(settings):
    db, registry = setup(settings)
    db.execute(
        "INSERT INTO schedules VALUES ('legacy-date','Legacy','Facts','strategist',1440,1,'2000-01-01',?)",
        (now(),),
    )
    assert sch.dispatch(db, settings, registry) == 1
    assert sch.dispatch(db, settings, registry) == 0
    assert len(tasks(db, "legacy-date")) == 1
    assert (
        db.one("SELECT occurrence FROM schedule_occurrences WHERE schedule_id='legacy-date'")["occurrence"]
        == "2000-01-01"
    )
