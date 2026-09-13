"""Real runtime/storage with scripted planners. No measured performance claim."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent4good.db import Database, now
from agent4good.engine import Engine
from agent4good.learning import LearningStore, sync, verified_document
from test_engine import FakeProvider, answer, call, make
from test_quality import setup, drain
from test_tools import runnable
from test_workers import spawn, active
from test_policy import install


def finished(settings, status="done"):
    db, engine, task = make(settings, FakeProvider(answer("PostgreSQL storage approach completed")))
    db.execute(
        "UPDATE tasks SET title='PostgreSQL storage',prompt='Plan PostgreSQL storage' WHERE id=?", (task,)
    )
    if status == "done":
        engine.run(task)
    elif status == "failed":
        engine.provider = FakeProvider(answer(calls=[call("unknown", {})]))
        engine.run(task)
    else:
        db.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (now(), task))
    return db, task, LearningStore(db)


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_terminal_outcomes_and_restart(settings, tmp_path, status):
    db, task, store = finished(settings, status)
    doc = store.inspect(task)[0]
    assert doc["outcome"] == status
    assert doc["objective"] == "Plan PostgreSQL storage"
    assert doc["verification"]["status"] == "unverified_outcome"
    assert doc["cost"]["estimated_usd"] is None
    assert doc["cost"]["input_tokens"] is None
    assert doc["evidence"]["task"] == f"/api/tasks/{task}"
    assert doc["abandoned"] == (status == "cancelled")
    assert bool(doc["failure"]) == (status == "failed")
    if status != "cancelled":
        assert doc["timing"]["elapsed_seconds"] >= 0
    path = tmp_path / "restored.sqlite3"
    with sqlite3.connect(db.path) as src, sqlite3.connect(path) as dst:
        src.backup(dst)
    restored = LearningStore(Database(path)).inspect(task)[0]
    assert doc == restored
    sync(db)
    assert len(store.inspect(task)) == 1


def test_full_action_evidence_and_failed_attempt(settings):
    db, e, t = make(
        settings,
        FakeProvider(
            answer(calls=[call("artifact_write", {"name": "postgres.md", "content": "PostgreSQL plan"})]),
            answer(calls=[call("unknown", {})]),
        ),
    )
    e.run(t)
    doc = LearningStore(db).inspect(t)[0]
    assert doc["outcome"] == "failed"
    assert doc["tools"] == ["artifact_write"]
    assert doc["actions"][0]["status"] == "done"
    assert doc["evidence"]["artifacts"][0]["name"] == "postgres.md"
    assert doc["approach"]["plan"][0]["tool"] == "artifact_write"


def test_verified_scope_requires_both_reviews(settings):
    db, task, provider = setup(settings)
    drain(db, settings, provider)
    doc = LearningStore(db).inspect(task)[0]
    assert doc["verification"]["status"] == "configured_checks_passed"
    assert len(doc["verification"]["rounds"]) == 2
    assert doc["classification"] == "runtime_observation"
    assert "only the configured" in doc["verification"]["scope"]


def test_later_planner_records_concrete_effect_and_action(settings):
    db, original, store = finished(settings, "failed")
    oid = store.inspect(original)[0]["id"]
    later = db.create_task("PostgreSQL storage", "Plan PostgreSQL storage", "strategist", True)

    class Planner:
        seen = False

        def respond(self, instructions, items, tools):
            if not self.seen:
                self.seen = True
                assert oid in json.dumps(items)
                assert "unverified_outcome" in json.dumps(items)
                response = answer(
                    "",
                    [
                        call(
                            "artifact_write",
                            {"name": "plan.md", "content": "PostgreSQL: use supported storage tools"},
                        )
                    ],
                )
                response["output_text"] = (
                    f"Learning use: {oid}: Avoid the unsupported tool. Save an inspectable PostgreSQL plan instead."
                )
                return response
            return answer("PostgreSQL plan saved")

    engine = Engine(db, settings, Planner())
    assert engine.claim() == later
    engine.run(later)
    assert db.task(later)["status"] == "done", db.task(later)["error"]
    use = verified_document(db.one("SELECT * FROM learning_uses WHERE task_id=? ORDER BY step", (later,)))
    assert use["uses"][0]["reported_effect"]
    assert use["uses"][0]["effect_status"] == "model_interpretation"
    assert use["proposed_actions"][0]["name"] == "artifact_write"
    assert db.one("SELECT * FROM artifacts WHERE task_id=?", (later,))


def test_correction_invalidation_concurrency_and_history(settings):
    db, task, store = finished(settings)
    oid = store.inspect(task)[0]["id"]
    second = db.create_task("PostgreSQL", "PostgreSQL storage", "strategist")
    correction = dict(
        kind="correction",
        text="PostgreSQL conclusion contradicted by inspection",
        source="owner inspection",
        confidence=1,
    )
    note = store.correct(oid, correction)
    entry = store.search(second, "PostgreSQL", 4000)["records"][0]
    assert not entry["verified_fact"] and entry["verification"] == "owner_annotation_not_verified"
    assert entry["note"]["text"] == correction["text"]

    def invalidate(_):
        try:
            return store.correct(oid, {**correction, "kind": "invalidation", "supersedes": note["id"]})
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        assert sum(bool(r) for r in pool.map(invalidate, range(2))) == 1
    assert not store.search(second, "PostgreSQL", 4000)["records"]
    assert len(store.inspect(task)[0]["notes"]) == 2


def test_cost_provenance_late_accounting_and_no_double_count(settings):
    db, task, store = finished(settings, "cancelled")
    db.execute(
        "INSERT INTO model_routes VALUES (?,?)",
        (task, json.dumps({"input_usd_per_million": 2, "output_usd_per_million": 8})),
    )
    db.execute(
        "INSERT INTO model_calls VALUES (?,?,?,?,?,?,?)",
        ("model_1", task, 900, 5000, "reserved", None, now()),
    )
    first = store.inspect(task)[0]
    assert first["cost"]["estimated_usd"] is None
    assert first["cost"]["unknown_calls"] == 1
    db.execute(
        "UPDATE model_calls SET status='completed',usage=? WHERE id='model_1'",
        (json.dumps({"input_tokens": 100, "output_tokens": 20}),),
    )
    second = store.inspect(task)[0]
    assert second["id"] == first["id"]
    assert second["cost"]["estimated_usd"] == pytest.approx(0.00036)
    assert second["cost"]["reserved_micro_usd"] == 5000
    assert second["cost"]["unknown_calls"] == 0
    assert len(store.inspect(task)) == 1
    db.execute("UPDATE model_calls SET status='failed_or_unknown',usage=NULL WHERE id='model_1'")
    assert store.inspect(task)[0]["cost"]["estimated_usd"] is None


def test_private_children_foreign_owners_and_policy(settings):
    r, parent = runnable(settings)
    child = spawn(r, parent, tools=["memory_search"])
    active(r, child)
    r.db.execute(
        "UPDATE tasks SET status='cancelled',title='PostgreSQL private',prompt='PostgreSQL private' WHERE id=?",
        (child,),
    )
    store = LearningStore(r.db)
    assert store.inspect(child)
    assert not store.search(parent, "PostgreSQL", 4000)["records"]
    foreign = r.db.create_task("PostgreSQL foreign", "PostgreSQL", "strategist")
    r.db.execute("UPDATE tasks SET owner_id='other',status='done' WHERE id=?", (foreign,))
    with pytest.raises(RuntimeError):
        store.inspect(foreign)
    assert all(d.get("task_id") != foreign for d in store.inspect())
    install(r, rules=[{"id": "deny-history", "scope": {"tool": "memory_search"}, "effect": "deny"}])
    assert Engine(r.db, settings, FakeProvider())._memory_context(r.db.task(parent)) == ""


def test_budget_corruption_and_no_relevance(settings):
    db, task, store = finished(settings)
    oid = store.inspect(task)[0]["id"]
    second = db.create_task("PostgreSQL", "PostgreSQL", "strategist")
    assert not store.search(second, "unrelated chocolate", 4000)["records"]
    assert not store.search(second, "PostgreSQL", 100)["records"]
    result = store.search(second, "PostgreSQL", 1400)
    assert result["records"] and result["budget_units"] <= 1400
    db.execute("UPDATE learning_outcomes SET document='broken' WHERE id=?", (oid,))
    assert not store.search(second, "PostgreSQL", 4000)["records"]
    assert store.inspect(task)[0]["status"] == "quarantined"


def test_authenticated_api_and_correction(owner, app, client):
    task = app.state.db.create_task("Tagged PostgreSQL", "PostgreSQL", "strategist")
    app.state.db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task,))
    doc = owner.get(f"/api/outcomes?task_id={task}").json()[0]
    response = owner.post(
        f"/api/outcomes/{doc['id']}/notes",
        json={"kind": "invalidation", "text": "Test fixture", "source": "owner"},
    )
    assert response.status_code == 200
    assert owner.get(f"/api/tasks/{task}/learning").status_code == 200
    app.state.db.execute("UPDATE tasks SET owner_id='other' WHERE id=?", (task,))
    assert owner.get(f"/api/outcomes?task_id={task}").status_code == 404
    assert (
        owner.post(
            f"/api/outcomes/{doc['id']}/notes", json={"kind": "invalidation", "text": "x", "source": "owner"}
        ).status_code
        == 409
    )
    owner.post("/api/logout", json={})
    assert client.get("/api/outcomes").status_code == 401


def test_correction_retires_compatibility_episode(settings):
    from agent4good.memory import MemoryStore

    db, task, store = finished(settings)
    oid = store.inspect(task)[0]["id"]
    later = db.create_task("PostgreSQL", "PostgreSQL", "strategist")
    assert MemoryStore(db).search(later, "PostgreSQL")["records"]
    store.correct(oid, {"kind": "invalidation", "text": "Contradicted", "source": "owner inspection"})
    assert not MemoryStore(db).search(later, "PostgreSQL")["records"]


def test_retrieval_preserves_pre_update_accounting_evidence(settings):
    db, task, store = finished(settings)
    original = store.inspect(task)[0]
    db.execute(
        "INSERT INTO model_calls VALUES (?,?,?,?,?,?,?)",
        ("late", task, 100, -1, "failed_or_unknown", None, now()),
    )
    updated = store.inspect(task)[0]
    assert original["sha256"] != updated["sha256"]
    row = db.one(
        "SELECT * FROM learning_evidence WHERE outcome_id=? AND sha256=?",
        (original["id"], original["sha256"]),
    )
    assert json.loads(row["document"])["cost"]["unknown_calls"] == 0
    assert updated["cost"]["unknown_calls"] == 1
