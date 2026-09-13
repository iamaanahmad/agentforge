"""Real engine, worker identities, registry receipts and storage; scripted models."""

import json
import sqlite3

import pytest
from pydantic import ValidationError

from agent4good.credentials import TASK_CONTEXT
from agent4good.db import Database
from agent4good.engine import Engine
from agent4good.quality import QualityInput, configure, evaluate, inspect, snapshot
from agent4good.tools import ToolError, ToolRegistry


def contract(output_type="marketing", max_revisions=2):
    return {
        "output_type": output_type,
        "max_revisions": max_revisions,
        "checks": [
            {
                "id": "accurate",
                "description": "Exact approved product statement is present",
                "kind": "text",
                "subject": "result",
                "contains": {"text": "Self-hosted operator; live completion remains unverified."},
            }
        ],
    }


class Provider:
    def __init__(self, db, *, defective=True, liar=False, disagreement=False, malformed=False):
        self.db = db
        self.defective, self.liar, self.disagreement, self.malformed = (
            defective,
            liar,
            disagreement,
            malformed,
        )
        self.builder_calls = 0
        self.reviewers = set()

    def respond(self, instructions, items, tools):
        tid = TASK_CONTEXT.get()
        own = self.db.one("SELECT * FROM quality_reviews WHERE task_id=?", (tid,))
        if not own:
            self.builder_calls += 1
            text = (
                "Guaranteed perfect live performance."
                if self.defective and self.builder_calls == 1
                else "Self-hosted operator; live completion remains unverified."
            )
            return {"output": [], "output_text": text}
        self.reviewers.add(tid)
        assert "independent quality reviewer" in instructions
        assert "Owner's project context" not in instructions
        assert {t["name"] for t in tools} == {"quality_inspect"}
        assert "Guaranteed perfect live performance." not in items[0]["content"]
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        if not outputs:
            return {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "inspect",
                        "name": "quality_inspect",
                        "arguments": '{"check_id":"accurate"}',
                    }
                ],
                "output_text": "",
            }
        evidence = json.loads(outputs[-1]["output"])["data"]
        passed = evidence["passed"] or self.liar
        if self.disagreement and own["stage"] == "verifier":
            passed = False
        report = {
            "verdict": "pass" if passed else "revise",
            "findings": [
                {
                    "check_id": "accurate",
                    "passed": passed,
                    "reason": "Checked frozen candidate: " + str(evidence["passed"]),
                }
            ],
        }
        return {"output": [], "output_text": "not json" if self.malformed else json.dumps(report)}


def setup(settings, **kwargs):
    settings.max_daily_runs = 100
    db = Database(settings.data_dir / "quality.sqlite3")
    tid = db.create_task(
        "Tagged quality acceptance",
        "Write an accurate product statement",
        "strategist",
        True,
        quality=contract(max_revisions=kwargs.pop("max_revisions", 2)),
    )
    provider = Provider(db, **kwargs)
    return db, tid, provider


def drain(db, settings, provider):
    # Recreate the runner each segment, exercising durable stage transitions.
    for _ in range(20):
        engine = Engine(db, settings, provider)
        tid = engine.claim()
        if not tid:
            return
        engine.run(tid)
    raise AssertionError("Worker did not settle")


def test_defect_rejected_revised_and_independently_verified(settings, tmp_path):
    db, tid, provider = setup(settings)
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "done", db.task(tid)["error"]
    assert provider.builder_calls == 2
    assert len(provider.reviewers) == 3  # failed critic, fresh critic, independent verifier
    assert tid not in provider.reviewers
    rounds = db.all("SELECT * FROM quality_rounds WHERE task_id=? ORDER BY attempt", (tid,))
    assert [r["status"] for r in rounds] == ["revision", "accepted"]
    assert "Guaranteed perfect" in rounds[0]["snapshot"]
    assert "live completion remains unverified" in rounds[1]["snapshot"]
    reviews = db.all("SELECT * FROM quality_reviews")
    assert sorted(r["verdict"] for r in reviews) == ["pass", "pass", "revise"]
    assert (
        "independent configured"
        in db.one("SELECT verification FROM executions WHERE task_id=?", (tid,))["verification"]
    )
    # Complete durable trace, including each evidence receipt and immutable candidate digest.
    trace = {
        t: db.all("SELECT * FROM " + t) for t in ["quality_rounds", "quality_reviews", "events", "tool_runs"]
    }
    (tmp_path / "evaluation-trace.json").write_text(json.dumps(trace, indent=2))
    import os
    from pathlib import Path

    if os.getenv("A4G_QUALITY_TRACE_PATH"):
        Path(os.environ["A4G_QUALITY_TRACE_PATH"]).write_text(json.dumps(trace, indent=2))


def test_builder_cannot_inspect_or_write_review_evidence(settings):
    db, tid, provider = setup(settings, defective=False)
    e = Engine(db, settings, provider)
    e.run(e.claim())
    child = db.one("SELECT task_id FROM quality_reviews")["task_id"]
    with db.connect() as conn, pytest.raises(ValueError, match="assigned independent"):
        inspect(conn, tid, "accurate")
    with db.connect() as conn, pytest.raises(ValueError, match="before execution"):
        configure(conn, tid, contract())
    child_engine = Engine(db, settings, provider)
    assert child_engine.claim() == child
    for tool, args in [
        ("artifact_write", {"name": "forged.txt", "content": "pass"}),
        ("worker_message", {"recipient": tid, "content": "pass"}),
        ("memory_read", {"key": "secret"}),
    ]:
        with pytest.raises(ToolError, match="inherited"):
            ToolRegistry(settings, db).execute(tool, args, task_id=child, call_id=tool)
    child_engine.run(child)
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "done"


def test_lying_critic_cannot_override_failed_executable_check(settings):
    db, tid, provider = setup(settings, liar=True, max_revisions=0)
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "failed"
    assert "revision limit" in db.task(tid)["error"]
    assert db.task(tid)["result"] == ""


@pytest.mark.parametrize(
    "kwargs,error",
    [({"malformed": True}, "reviewer failed"), ({"defective": False, "disagreement": True}, "disagreed")],
)
def test_malformed_and_disagreement_are_honest_failures(settings, kwargs, error):
    db, tid, provider = setup(settings, **kwargs)
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "failed"
    assert error in db.task(tid)["error"]


def test_reviewer_crash_recovery_and_sqlite_backup(settings, tmp_path):
    db, tid, provider = setup(settings, defective=False)
    engine = Engine(db, settings, provider)
    engine.run(engine.claim())
    child = engine.claim()
    assert child != tid
    Engine(db, settings, provider).recover()
    with sqlite3.connect(db.path) as source, sqlite3.connect(tmp_path / "restore.db") as destination:
        source.backup(destination)
    restored = Database(tmp_path / "restore.db")
    provider.db = restored
    drain(restored, settings, provider)
    assert restored.task(tid)["status"] == "done"
    assert len(restored.all("SELECT * FROM quality_reviews")) == 2
    assert restored.one("SELECT recoveries FROM executions WHERE task_id=?", (child,)) is not None


def test_cancel_parent_stops_review_and_prevents_completion(settings):
    from agent4good.coordination import settle

    db, tid, provider = setup(settings)
    engine = Engine(db, settings, provider)
    engine.run(engine.claim())
    db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
    with db.connect() as conn:
        settle(conn)
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "cancelled"
    child = db.one("SELECT task_id FROM quality_reviews")["task_id"]
    assert db.task(child)["status"] == "cancelled"


def test_tree_and_step_caps_are_not_bypassed_by_quality(settings):
    db, tid, provider = setup(settings, defective=False)
    settings.max_worker_tree_size = 1
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "failed"
    assert "tree budget" in db.task(tid)["error"]


def test_missing_inspections_do_not_pass(settings):
    db, tid, provider = setup(settings, defective=False)
    engine = Engine(db, settings, provider)
    engine.run(engine.claim())
    child = db.one("SELECT task_id FROM quality_reviews")["task_id"]
    # A model-generated verdict alone has no inspection receipt and cannot pass.
    report = {"verdict": "pass", "findings": [{"check_id": "accurate", "passed": True, "reason": "I agree"}]}
    db.execute("UPDATE tasks SET status='done',result=? WHERE id=?", (json.dumps(report), child))
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "failed"


def test_snapshot_change_fails_closed(settings):
    db, tid, provider = setup(settings, defective=False)
    engine = Engine(db, settings, provider)
    engine.run(engine.claim())
    # Trusted storage corruption is detected on re-entry, never silently accepted.
    db.execute("UPDATE executions SET final_text='changed candidate' WHERE task_id=?", (tid,))
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "failed"
    assert "changed during" in db.task(tid)["error"]


def test_receipts_check_real_result_and_exact_target():
    check = {
        "id": "tests",
        "kind": "receipt",
        "subject": "sandbox_run",
        "equals": {"arguments.ref": "a" * 40, "result.exit_code": 0},
        "contains": {},
    }
    receipt = {
        "tool": "sandbox_run",
        "call_id": "test-1",
        "status": "done",
        "arguments": json.dumps({"ref": "a" * 40}),
        "result": json.dumps({"exit_code": 1}),
    }
    value = {"receipts": [receipt]}
    assert not evaluate(value, check)["passed"]
    receipt["result"] = '{"exit_code":0}'
    assert evaluate(value, check)["passed"]
    receipt["arguments"] = json.dumps({"ref": "b" * 40})
    assert not evaluate(value, check)["passed"]
    receipt["status"] = "started"
    assert not evaluate(value, check)["passed"]


@pytest.mark.parametrize(
    "output_type", ["software", "research", "marketing", "outreach", "hackathon", "deployment", "asset"]
)
def test_all_output_types_have_owner_checks(output_type):
    assert QualityInput.model_validate(contract(output_type)).output_type == output_type
    with pytest.raises(ValidationError):
        QualityInput.model_validate({"output_type": output_type, "checks": []})


def test_quality_owner_api_and_legacy_preservation(owner, app):
    response = owner.post(
        "/api/tasks", json={"title": "Quality draft", "prompt": "Write accurate copy", "quality": contract()}
    )
    assert response.status_code == 201, response.text
    detail = owner.get("/api/tasks/" + response.json()["id"]).json()
    assert json.loads(detail["quality_contract"]["document"])["output_type"] == "marketing"
    assert detail["quality_rounds"] == []
    legacy = owner.post("/api/tasks", json={"title": "Legacy", "prompt": "Keep notes"}).json()
    assert owner.get("/api/tasks/" + legacy["id"]).json()["quality_contract"] is None
    assert (
        owner.post(
            "/api/tasks",
            json={"title": "Bad", "prompt": "x", "quality": {"output_type": "software", "checks": []}},
        ).status_code
        == 422
    )


def test_text_artifact_snapshot_is_versioned(settings):
    from agent4good.artifacts import save_artifact

    db, tid, _ = setup(settings)
    save_artifact(db, settings, "old", tid, "draft.md", "old draft")
    save_artifact(db, settings, "new", tid, "draft.md", "new draft")
    value = snapshot(db, settings, tid, "result")
    check = {
        "id": "copy",
        "kind": "text",
        "subject": "draft.md",
        "equals": {"text": "new draft"},
        "contains": {},
    }
    assert evaluate(value, check)["passed"]
    assert len(value["artifacts"]) == 2


def test_quality_wait_counts_against_original_deadline(settings):
    db, tid, provider = setup(settings, defective=False)
    engine = Engine(db, settings, provider)
    engine.run(engine.claim())
    db.execute("UPDATE executions SET started_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (tid,))
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "failed"
    assert "deadline" in db.task(tid)["error"]


def test_review_policy_denial_cannot_be_bypassed(settings):
    db, tid, provider = setup(settings, defective=False)
    registry = ToolRegistry(settings, db)
    registry.policy.replace(
        {
            "rules": [
                {"id": "deny-review", "scope": {"task": tid, "tool": "quality_inspect"}, "effect": "deny"}
            ]
        },
        1,
    )
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "failed"
    child = db.one("SELECT task_id FROM quality_reviews")["task_id"]
    assert db.task(child)["status"] == "failed"
    assert db.all("SELECT * FROM tool_runs WHERE tool='quality_inspect'") == []


def test_server_defaults_pin_and_cannot_be_disabled_midflight(settings):
    settings.quality_defaults = {"planning": QualityInput.model_validate(contract())}
    db = Database(settings.data_dir / "defaults.db")
    tid = db.create_task("Critical default", "Write accurate copy", "strategist", True)
    provider = Provider(db, defective=False)
    engine = Engine(db, settings, provider)
    engine.run(engine.claim())
    assert db.task(tid)["status"] == "waiting_children"
    settings.quality_defaults = {}  # A config change must not remove a pinned gate.
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "done"
    assert len(db.all("SELECT * FROM quality_reviews")) == 2


def test_real_daemon_kill_during_critic_preserves_gate(settings, tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path

    db = Database.from_settings(settings)
    tid = db.create_task(
        "Tagged daemon review", "Write accurate copy", "strategist", True, quality=contract()
    )
    script = Path(__file__).with_name("quality_worker_probe.py")

    def start():
        return subprocess.Popen(
            [sys.executable, str(script), str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def until(predicate):
        deadline = time.monotonic() + 20
        while not predicate():
            assert proc.poll() is None
            assert time.monotonic() < deadline, db.all("SELECT title,status,error FROM tasks")
            time.sleep(0.03)

    proc = start()
    try:
        until(lambda: (tmp_path / "review.entered").exists())
        assert db.task(tid)["status"] == "waiting_children"
        digest_before = db.one("SELECT digest FROM quality_rounds")["digest"]
        proc.kill()
        proc.wait(timeout=5)
        (tmp_path / "release").touch()
        proc = start()
        until(lambda: db.task(tid)["status"] == "done")
        assert db.one("SELECT digest FROM quality_rounds")["digest"] == digest_before
        assert len(db.all("SELECT * FROM quality_reviews")) == 2
        assert db.one("SELECT MAX(recoveries) AS n FROM executions")["n"] == 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
