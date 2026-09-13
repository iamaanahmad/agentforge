"""Mission acceptance uses real workers/storage/tools and explicitly scripted models."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from agent4good import missions
from agent4good.db import Database
from agent4good.engine import Engine
from agent4good.tools import ToolRegistry, ToolError
from test_engine import FakeProvider, answer, call
from test_tools import permit


def spec(**changes):
    return missions.MissionInput(
        **{
            "title": "[TEST] Evidence mission",
            "objective": "Save a checked report",
            "constraints": "Use supplied facts only",
            "criteria": [
                {
                    "id": "report",
                    "description": "Report includes the supplied fact",
                    "kind": "artifact",
                    "name": "report.md",
                    "contains": "checked fact",
                }
            ],
            "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            **changes,
        }
    )


def step(key="report", **changes):
    return {
        "key": key,
        "title": key,
        "prompt": "Save the supplied checked fact",
        "agent": "research_analyst",
        "tools": ["artifact_write"],
        **changes,
    }


def setup(settings, payload=None, steps=None):
    db = Database(settings.data_dir / "missions.sqlite3")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        mid = missions.create(conn, payload or spec())
        if steps is not None:
            missions.apply_plan(
                conn, db, mid, missions.Plan(expected_revision=0, reason="Initial plan", steps=steps)
            )
        missions.control(conn, db, mid, "start")
    return db, mid


def detail(db, mid):
    with db.connect() as conn:
        return missions.detail(conn, mid)


def control(db, mid, action):
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        missions.control(conn, db, mid, action)


def test_real_planner_and_dependent_workers_complete_evidence(settings):
    db, mid = setup(settings)
    plan = dict(
        expected_revision=0,
        reason="Create source then report",
        steps=[step("source"), step(depends_on=["source"])],
    )
    provider = FakeProvider(
        answer(calls=[call("mission_plan", {"request": json.dumps(plan)})]), answer("Plan saved")
    )
    e = Engine(db, settings, provider)
    planner = e.claim()
    e.run(planner)
    assert db.task(planner)["status"] == "done", db.task(planner)
    d = detail(db, mid)
    source, report = [next(s for s in d["steps"] if s["node_key"] == key) for key in ["source", "report"]]
    assert db.task(report["id"])["status"] == "draft"
    e = Engine(db, settings, FakeProvider(answer("Source checked")))
    assert e.claim() == source["id"]
    e.run(source["id"])
    assert detail(db, mid)["status"] == "running"
    e = Engine(
        db,
        settings,
        FakeProvider(
            answer(calls=[call("artifact_write", {"name": "report.md", "content": "checked fact"})]),
            answer("Saved"),
        ),
    )
    assert e.claim() == report["id"]
    e.run(report["id"])
    e.claim()  # next daemon tick aggregates terminal results
    d = detail(db, mid)
    assert d["status"] == "done", d
    assert d["verification"][0]["evidence"][0].startswith("artifact:")
    assert d["spec"]["objective"] == "Save a checked report"
    assert d["usage"]["tasks"] == 3


def test_final_text_alone_never_proves_success(settings):
    db, mid = setup(settings, steps=[step()])
    e = Engine(db, settings, FakeProvider(answer("All criteria met; checked fact")))
    t = e.claim()
    e.run(t)
    e.claim()
    assert db.task(t)["status"] == "done"
    assert detail(db, mid)["status"] == "needs_evidence"
    assert not detail(db, mid)["verification"][0]["met"]


@pytest.mark.parametrize(
    "steps",
    [
        [step(depends_on=["missing"])],
        [step("a", depends_on=["b"]), step("b", depends_on=["a"])],
        [step(), step()],
        [step(agent="product_engineer")],
        [step(tools=["send_email"])],
    ],
)
def test_invalid_plans_are_atomic(settings, steps):
    db = Database(settings.data_dir / "invalid.sqlite3")
    with db.connect() as conn:
        mid = missions.create(conn, spec())
    with pytest.raises(ValueError), db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        missions.apply_plan(conn, db, mid, missions.Plan(expected_revision=0, reason="bad", steps=steps))
    assert not detail(db, mid)["steps"]
    assert detail(db, mid)["revision"] == 0


def test_competing_plan_revisions_create_once(settings):
    db, mid = setup(settings, steps=[step("first")])

    def update():
        try:
            with db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                return missions.apply_plan(
                    conn,
                    db,
                    mid,
                    missions.Plan(
                        expected_revision=1, reason="Add next", steps=[step("next", depends_on=["first"])]
                    ),
                )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: update(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert len(detail(db, mid)["steps"]) == 2


def test_pause_recovery_and_cancel(settings):
    db, mid = setup(settings, steps=[step()])
    e = Engine(db, settings, FakeProvider())
    t = e.claim()
    control(db, mid, "pause")
    assert not e._active(t)
    assert e.claim() is None
    control(db, mid, "resume")
    assert e.claim() == t
    control(db, mid, "cancel")
    assert not e._active(t)
    assert db.task(t)["status"] == "cancelled"
    assert e.claim() is None


def test_deadline_expires_waiting_work(settings):
    db, mid = setup(settings, steps=[step()])
    d = detail(db, mid)["spec"]
    d["deadline"] = "2000-01-01T00:00:00+00:00"
    db.execute("UPDATE missions SET spec=? WHERE id=?", (json.dumps(d), mid))
    assert Engine(db, settings, FakeProvider()).claim() is None
    assert detail(db, mid)["status"] == "expired"
    assert all(s["status"] == "cancelled" for s in detail(db, mid)["steps"])


def test_budgets_cover_all_nodes_and_descendants(settings):
    payload = spec(
        tools=["worker_spawn", "artifact_write"],
        budget={"tasks": 3, "steps": 2, "tool_calls": 1, "model_tokens": 10000},
    )
    db, mid = setup(settings, payload, steps=[step(tools=["worker_spawn", "artifact_write"])])
    e = Engine(db, settings, FakeProvider())
    t = e.claim()
    r = e.registry
    args = {
        "request": json.dumps(
            {
                "title": "child",
                "prompt": "child",
                "agent": "research_analyst",
                "tools": ["artifact_write"],
                "reason": "Independent bounded work",
            }
        )
    }
    child = r.execute("worker_spawn", args, task_id=t, call_id="spawn")["data"]["child_id"]
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
    with pytest.raises((ValueError, ToolError), match="budget"):
        r.execute("artifact_write", {"name": "bad.md", "content": "bad"}, task_id=child, call_id="write")
    assert not db.all("SELECT * FROM artifacts")
    with db.connect() as conn:
        missions.reserve_model(conn, t, 6000, -1)
        conn.execute("INSERT INTO model_calls VALUES ('call',?,6000,-1,'reserved',NULL,'today')", (t,))
    with pytest.raises(ValueError, match="token budget"), db.connect() as conn:
        missions.reserve_model(conn, child, 6000, -1)
    db.execute("UPDATE tasks SET steps=2 WHERE id=?", (t,))
    with pytest.raises(ValueError, match="step budget"), db.connect() as conn:
        missions.reserve_step(conn, child)
    assert detail(db, mid)["usage"]["tasks"] == 2


def test_dependency_guard_and_worker_permissions(settings):
    db, mid = setup(settings, steps=[step("a"), step("b", depends_on=["a"])])
    t = next(s["id"] for s in detail(db, mid)["steps"] if s["node_key"] == "b")
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (t,))
    r = ToolRegistry(settings, db)
    with pytest.raises((ValueError, ToolError), match="dependencies"):
        r.execute(
            "artifact_write", {"name": "report.md", "content": "checked fact"}, task_id=t, call_id="bad"
        )
    with pytest.raises((ValueError, ToolError)):
        r.execute("mission_status", {}, task_id=t, call_id="privilege")


def test_replan_retains_original_contract_and_external_receipts(settings):
    payload = spec(tools=["memory_write"], writes=True)
    db, mid = setup(settings, payload, steps=[step("old", tools=["memory_write"])])
    e = Engine(db, settings, FakeProvider())
    t = e.claim()
    r = e.registry
    args = {"key": "mission_test", "content": "saved"}
    permit(r, t, "memory_write", args, "write")
    r.execute("memory_write", args, task_id=t, call_id="write")
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (t,))
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        missions.apply_plan(
            conn,
            db,
            mid,
            missions.Plan(
                expected_revision=1,
                reason="Keep old evidence",
                steps=[step("new", tools=["memory_write"], depends_on=["old"])],
            ),
        )
    nxt = e.claim()
    permit(r, nxt, "memory_write", args, "repeat")
    with pytest.raises(ToolError, match="already recorded"):
        r.execute("memory_write", args, task_id=nxt, call_id="repeat")
    assert db.one("SELECT COUNT(*) n FROM tool_runs")["n"] == 1
    assert detail(db, mid)["spec"] == payload.model_dump()


def test_mission_api_auth_reviews_and_direct_run_bypass(owner, client, app):
    payload = spec(
        criteria=[{"id": "review", "description": "Owner accepts report", "kind": "owner"}]
    ).model_dump()
    created = owner.post("/api/missions", json=payload)
    assert created.status_code == 201, created.text
    mid = created.json()["id"]
    plan = owner.post(
        f"/api/missions/{mid}/plan", json={"expected_revision": 0, "reason": "Owner plan", "steps": [step()]}
    )
    assert plan.status_code == 200, plan.text
    task = plan.json()["steps"][0]["id"]
    assert owner.post(f"/api/tasks/{task}/run", json={}).status_code == 409
    assert owner.post(f"/api/missions/{mid}/control/start", json={}).status_code == 409  # absent provider
    assert owner.post(
        f"/api/missions/{mid}/review",
        json={"criterion_id": "review", "evidence": "Inspected supplied report", "accepted": True},
    ).json()["verification"][0]["met"]
    assert owner.get(f"/api/missions/{mid}").json()["status"] == "draft"
    owner.post("/api/logout", json={})
    assert client.get("/api/missions").status_code == 401
    assert client.get(f"/api/missions/{mid}").status_code == 401


def test_mission_daemon_restart_keeps_completed_work(tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path

    db = Database(tmp_path / "agent4good.sqlite3")
    with db.connect() as conn:
        mid = missions.create(conn, spec())
        missions.control(conn, db, mid, "start")

    def start():
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("mission_daemon_probe.py")), str(tmp_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def until(predicate, proc):
        deadline = time.monotonic() + 20
        while not predicate():
            assert proc.poll() is None, "Daemon exited"
            assert time.monotonic() < deadline, db.all("SELECT title,status,error FROM tasks")
            time.sleep(0.03)

    proc = start()
    try:
        until(lambda: (tmp_path / "report-entered").exists(), proc)
        assert db.one("SELECT status FROM tasks WHERE title='source'")["status"] == "done"
        assert len(db.all("SELECT * FROM artifacts")) == 1
        proc.kill()
        proc.wait(timeout=5)
        (tmp_path / "release").touch()
        proc = start()
        until(lambda: detail(db, mid)["status"] == "done", proc)
        assert len(db.all("SELECT * FROM artifacts")) == 2
        assert len(db.all("SELECT * FROM tool_runs WHERE tool='mission_plan'")) == 1
        assert detail(db, mid)["verification"][0]["met"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_pause_during_external_request_preserves_receipt(settings, monkeypatch):
    db, mid = setup(settings, steps=[step()])
    e = Engine(
        db,
        settings,
        FakeProvider(
            answer(calls=[call("artifact_write", {"name": "report.md", "content": "checked fact"})]),
            answer("done"),
        ),
    )
    task = e.claim()
    original = e.registry._dispatch

    def paused(*args):
        result = original(*args)
        control(db, mid, "pause")
        return result

    monkeypatch.setattr(e.registry, "_dispatch", paused)
    e.run(task)
    assert db.task(task)["status"] == "queued"
    assert db.one("SELECT status FROM tool_runs")["status"] == "done"
    control(db, mid, "resume")
    again = Engine(db, settings, FakeProvider(answer("done")))
    assert again.claim() == task
    again.run(task)
    again.claim()
    assert detail(db, mid)["status"] == "done"
    assert len(db.all("SELECT * FROM artifacts")) == 1


def test_plan_task_budget_and_retirement_preserve_history(settings):
    db, mid = setup(settings, spec(budget={"tasks": 2}), steps=[step("a"), step("b", depends_on=["a"])])
    with pytest.raises(ValueError, match="task budget"), db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        missions.apply_plan(
            conn,
            db,
            mid,
            missions.Plan(expected_revision=1, reason="No extra budget", retire=["b"], steps=[step("c")]),
        )
    assert next(s for s in detail(db, mid)["steps"] if s["node_key"] == "b")["status"] == "draft"


def test_model_estimated_cost_is_shared_and_requires_prices(settings):
    from agent4good.model_router import ModelRouter
    from agent4good.provider import ProviderError

    db, mid = setup(settings, spec(budget={"model_cost_usd": 0.01}), steps=[step()])
    e = Engine(db, settings, FakeProvider())
    t = e.claim()
    router = ModelRouter(settings, e.registry.credentials, db)
    profile = router.pin(t, [])
    with pytest.raises(ProviderError, match="prices"):
        router.reserve(t, profile, "", [], [], None)
    with pytest.raises(ValueError, match="cost budget"), db.connect() as conn:
        missions.reserve_model(conn, t, 1000, 20000)
    with db.connect() as conn:
        missions.reserve_model(conn, t, 1000, 9000)
        conn.execute("INSERT INTO model_calls VALUES ('paid',?,1000,9000,'reserved',NULL,'today')", (t,))
    with pytest.raises(ValueError, match="cost budget"), db.connect() as conn:
        missions.reserve_model(conn, t, 1000, 2000)
    assert detail(db, mid)["usage"]["model_cost_usd"] == 0.009


def test_ambiguous_write_blocks_new_plan(settings):
    db, mid = setup(settings, spec(tools=["memory_write"], writes=True), steps=[step(tools=["memory_write"])])
    t = Engine(db, settings, FakeProvider()).claim()
    db.execute(
        "INSERT INTO tool_runs(task_id,call_id,tool,arguments,status) VALUES (?,'uncertain','memory_write','{}','started')",
        (t,),
    )
    with pytest.raises(ValueError, match="ambiguous"), db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        missions.apply_plan(
            conn,
            db,
            mid,
            missions.Plan(
                expected_revision=1, reason="Must inspect", steps=[step("retry", tools=["memory_write"])]
            ),
        )
    assert detail(db, mid)["revision"] == 1


def test_mission_priority_and_metric_evidence(settings):
    payload = spec(
        priority=5,
        tools=["memory_read"],
        criteria=[
            {
                "id": "observed",
                "description": "Measured count meets target",
                "kind": "metric",
                "name": "memory_read",
                "field": "count",
                "target": 3,
                "unit": "items",
            }
        ],
    )
    db, mid = setup(
        settings,
        payload,
        steps=[
            step("low", tools=["memory_read"], priority=-5),
            step("high", tools=["memory_read"], priority=4),
        ],
    )
    e = Engine(db, settings, FakeProvider())
    t = e.claim()
    assert db.task(t)["title"] == "high"
    assert not detail(db, mid)["verification"][0]["met"]
    db.execute(
        "INSERT INTO tool_runs(task_id,call_id,tool,arguments,status,result) VALUES (?,'measured','memory_read','{}','done',?)",
        (t, json.dumps({"count": 2})),
    )
    assert not detail(db, mid)["verification"][0]["met"]
    db.execute("UPDATE tool_runs SET result=? WHERE call_id='measured'", (json.dumps({"count": 3}),))
    assert detail(db, mid)["verification"][0]["met"]


def test_invalid_planning_calls_consume_mission_allowance(settings):
    db, mid = setup(settings, spec(budget={"tool_calls": 1}))
    e = Engine(db, settings, FakeProvider())
    t = e.claim()
    with pytest.raises(ValueError):
        e.registry.execute("mission_plan", {"request": "{}"}, task_id=t, call_id="invalid")
    with pytest.raises(ValueError, match="budget"):
        e.registry.execute("mission_status", {}, task_id=t, call_id="next")
    assert detail(db, mid)["revision"] == 0
    assert not db.all("SELECT * FROM mission_plans")
