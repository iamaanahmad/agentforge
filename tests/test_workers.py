"""Exercise real storage and runners; model outputs are deliberately scripted."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent4good.coordination import NAMES, settle
from agent4good.db import Database
from agent4good.engine import Engine
from agent4good.tools import ToolRegistry, ToolError
from test_tools import runnable, permit


def spawn(r, parent, label="child", tools=None, priority=0):
    return r.execute(
        "worker_spawn",
        {
            "request": json.dumps(
                {
                    "title": label,
                    "prompt": label,
                    "agent": "research_analyst",
                    "reason": "Independent evidence check",
                    "tools": list(NAMES) if tools is None else tools,
                    "priority": priority,
                }
            )
        },
        task_id=parent,
        call_id=label,
    )["data"]["child_id"]


def active(r, child):
    r.db.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))


def context(r, task, scope, operation, revision="", content="", call="ctx"):
    return r.execute(
        "worker_context",
        dict(scope=scope, operation=operation, revision=revision, content=content),
        task_id=task,
        call_id=call,
    )["data"]


def test_concurrent_real_children_and_aggregation(settings):
    r, parent = runnable(settings)
    a, b = spawn(r, parent, "a"), spawn(r, parent, "b")
    barrier = threading.Barrier(2)
    seen = []

    class Provider:
        def respond(self, instructions, items, tools):
            seen.append(threading.get_ident())
            barrier.wait(timeout=5)
            return {"output": [], "output_text": "Accepted " + items[0]["content"], "usage": {}}

    engines = [Engine(r.db, settings, Provider()), Engine(r.db, settings, Provider())]
    assert {e.claim() for e in engines} == {a, b}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(e.run, child) for e, child in zip(engines, [a, b])]
        for f in futures:
            f.result(timeout=10)
    assert len(set(seen)) == 2
    results = r.execute("worker_results", {}, task_id=parent, call_id="results")["data"]["children"]
    assert len(results) == 2 and all(c["status"] == "done" for c in results)
    assert {c["result"] for c in results} == {"Accepted a", "Accepted b"}


def test_context_messages_results_survive_database_backup(settings, tmp_path):
    r, parent = runnable(settings)
    a, b = spawn(r, parent, "a"), spawn(r, parent, "b")
    active(r, a)
    active(r, b)
    context(r, a, "private", "write", "0", "only a")
    context(r, parent, "shared", "write", "0", "shared fact")
    r.execute("worker_message", {"recipient": parent, "content": "child evidence"}, task_id=a, call_id="msg")
    import sqlite3

    with sqlite3.connect(r.db.path) as source, sqlite3.connect(tmp_path / "restore.sqlite3") as dest:
        source.backup(dest)
    restored = ToolRegistry(settings, Database(tmp_path / "restore.sqlite3"))
    assert context(restored, a, "private", "read", call="read-a")["content"] == "only a"
    assert context(restored, b, "private", "read", call="read-b")["content"] == ""
    assert context(restored, b, "shared", "read", call="read-shared")["content"] == "shared fact"
    messages = restored.execute("worker_results", {}, task_id=parent, call_id="results")["data"]["messages"]
    assert messages[0]["content"] == "child evidence"
    with pytest.raises(ValueError, match="parent-child"):
        restored.execute(
            "worker_message", {"recipient": b, "content": "cross sibling"}, task_id=a, call_id="bad"
        )


def test_context_concurrent_compare_and_swap_prevents_lost_write(settings):
    r, parent = runnable(settings)
    a, b = spawn(r, parent, "a"), spawn(r, parent, "b")
    active(r, a)
    active(r, b)

    def write(t):
        try:
            return context(ToolRegistry(settings, r.db), t, "shared", "write", "0", t)
        except ValueError as exc:
            assert "changed" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [a, b]))
    assert sum(x is not None for x in results) == 1
    assert context(r, parent, "shared", "read", call="read")["revision"] == 1


def test_permissions_narrow_and_inherit_parent_denials(settings):
    r, parent = runnable(settings)
    a = spawn(r, parent, tools=["worker_spawn", "memory_read"])
    active(r, a)
    with pytest.raises(ValueError, match="narrow"):
        spawn(r, a, tools=["send_email"])
    with pytest.raises(ToolError, match="inherited"):
        r.execute("artifact_write", {"name": "x.txt", "content": "x"}, task_id=a, call_id="forbidden")
    r.policy.replace(
        {
            "rules": [
                {"id": "parent-deny", "scope": {"task": parent, "tool": "memory_read"}, "effect": "deny"}
            ]
        },
        1,
    )
    with pytest.raises(ToolError):
        r.execute("memory_read", {"key": "x"}, task_id=a, call_id="denied")
    with pytest.raises(ValueError, match="narrow"):
        spawn(r, parent, "memory", tools=["memory_write"])


def test_priority_global_limit_tree_size_and_depth(settings):
    settings.max_worker_tree_size = 4
    settings.max_worker_depth = 1
    settings.max_concurrent_runs = 2
    r, parent = runnable(settings)
    low = spawn(r, parent, "low", priority=-10)
    high = spawn(r, parent, "high", priority=10)
    engine = Engine(r.db, settings)
    assert engine.claim() == high
    assert engine.claim() is None  # parent occupies the second slot
    with pytest.raises(ValueError, match="depth"):
        spawn(r, high, "grandchild")
    spawn(r, parent, "third")
    with pytest.raises(ValueError, match="size"):
        spawn(r, parent, "fourth")
    assert r.db.task(low)["status"] == "queued"


def test_cancel_subtree_and_failure_leave_sibling_running(settings):
    r, parent = runnable(settings)
    a, b = spawn(r, parent, "a"), spawn(r, parent, "b")
    active(r, a)
    grandchild = spawn(r, a, "grandchild")
    r.db.execute("UPDATE tasks SET status='failed' WHERE id=?", (a,))
    with r.db.connect() as conn:
        settle(conn)
    assert r.db.task(grandchild)["status"] == "cancelled"
    assert r.db.task(b)["status"] == "queued"
    r.db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (parent,))
    with r.db.connect() as conn:
        settle(conn)
    assert r.db.task(b)["status"] == "cancelled"


def test_restart_wait_and_shared_tree_budget(settings):
    settings.max_worker_tree_steps = 1
    r, parent = runnable(settings)
    a = spawn(r, parent)
    r.db.execute("UPDATE tasks SET status='waiting_children' WHERE id=?", (parent,))

    class Final:
        def respond(self, *args):
            return {"output": [], "output_text": "child result", "usage": {}}

    engine = Engine(r.db, settings, Final())
    assert engine.claim() == a
    engine.run(a)
    assert r.db.task(a)["status"] == "done"
    restarted = Engine(Database(r.db.path), settings, Final())
    assert restarted.claim() == parent
    restarted.run(parent)
    assert "tree step budget" in r.db.task(parent)["error"]


def test_spawn_receipt_atomic_idempotence(settings):
    r, parent = runnable(settings)
    assert spawn(r, parent) == spawn(r, parent)
    assert len(r.db.all("SELECT * FROM worker_nodes WHERE parent_id=?", (parent,))) == 1
    assert len(r.db.all("SELECT * FROM tool_runs WHERE tool='worker_spawn'")) == 1


def test_external_write_conflict_is_held(settings, monkeypatch):
    r, parent = runnable(settings)
    a, b = spawn(r, parent, "a", ["github_create_branch"]), spawn(r, parent, "b", ["github_create_branch"])
    active(r, a)
    active(r, b)
    monkeypatch.setattr(r, "availability", lambda name: ("configured", "test"))
    monkeypatch.setattr(r, "_dispatch", lambda *args: {"ref": "agent4good/test"})
    args = {"branch": "agent4good/test"}
    permit(r, a, "github_create_branch", args)
    permit(r, b, "github_create_branch", args)
    r.execute("github_create_branch", args, task_id=a, call_id="check")
    with pytest.raises(ToolError, match="another active worker"):
        r.execute("github_create_branch", args, task_id=b, call_id="check")


def test_worker_daemon_concurrent_children_survive_process_death(tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path

    db = Database(tmp_path / "agent4good.sqlite3")
    parent = db.create_task("parent", "Tagged parent mission", "strategist", True)
    script = Path(__file__).parent / "worker_tree_probe.py"

    def start():
        return subprocess.Popen(
            [sys.executable, str(script), str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def until(predicate, proc):
        deadline = time.monotonic() + 15
        while not predicate():
            assert proc.poll() is None, "Worker process exited unexpectedly"
            assert time.monotonic() < deadline, db.all("SELECT title,status,error FROM tasks")
            time.sleep(0.03)

    proc = start()
    try:
        until(lambda: all((tmp_path / (n + ".entered")).exists() for n in ["alpha", "beta"]), proc)
        assert len(db.all("SELECT id FROM tasks WHERE status='running'")) == 2
        assert db.task(parent)["status"] == "waiting_children"
        proc.kill()
        proc.wait(timeout=5)
        (tmp_path / "release").write_text("release")
        proc = start()
        until(lambda: db.task(parent)["status"] == "done", proc)
        assert "alpha evidence" in db.task(parent)["result"]
        assert "beta evidence" in db.task(parent)["result"]
        assert len(db.all("SELECT * FROM worker_messages")) == 2
        assert len(db.all("SELECT * FROM worker_context WHERE scope='private'")) == 2
        assert len(db.all("SELECT * FROM tool_runs WHERE tool='worker_spawn'")) == 2
        assert all(
            r["recoveries"] == 1
            for r in db.all("SELECT recoveries FROM executions WHERE task_id!=?", (parent,))
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_tree_model_token_reservations_include_siblings(settings):
    from agent4good.model_router import ModelRouter
    from agent4good.model_config import ModelProfile
    from agent4good.provider import ProviderError

    settings.max_task_model_reserved_tokens = 15000
    r, parent = runnable(settings)
    a, b = spawn(r, parent, "a"), spawn(r, parent, "b")
    active(r, a)
    active(r, b)
    router = ModelRouter(settings, r.credentials, r.db)
    router.reserve(a, ModelProfile(model="test-model", max_output_tokens=4096), "", [], [], None)
    with pytest.raises(ProviderError, match="token reservation"):
        router.reserve(b, ModelProfile(model="test-model", max_output_tokens=4096), "", [], [], None)
    assert len(r.db.all("SELECT * FROM model_calls")) == 1


def test_child_retry_budget_is_independent(settings):
    from agent4good.provider import ProviderError

    r, parent = runnable(settings)
    a, b = spawn(r, parent, "a"), spawn(r, parent, "b")

    class Retry:
        def __init__(self):
            self.calls = 0

        def respond(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("transport", "Tagged retry", retryable=True)
            return {"output": [], "output_text": "recovered child", "usage": {}}

    engine = Engine(r.db, settings, Retry())
    assert engine.claim() == a
    engine.run(a)
    assert r.db.task(a)["status"] == "done"
    assert r.db.one("SELECT model_failures FROM executions WHERE task_id=?", (a,))["model_failures"] == 1
    assert r.db.task(b)["steps"] == 0


def test_cancel_api_rejects_child_approval(owner, app):
    # Exercise owner route and persistent descendants, not a direct status edit.
    db = app.state.db
    parent = db.create_task("parent", "tagged", "strategist", True)
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
    r = ToolRegistry(app.state.settings, db)
    child = spawn(r, parent)
    response = owner.post("/api/tasks/" + parent + "/cancel", json={})
    assert response.status_code == 200
    assert db.task(child)["status"] == "cancelled"


def read_result(r, parent, child, offset=0, limit=4000, call="page"):
    return r.execute(
        "worker_result_read",
        {"child_id": child, "offset": str(offset), "limit": str(limit)},
        task_id=parent,
        call_id=call,
    )["data"]


def test_complete_result_pages_preserve_unicode_tail_and_repeat_receipts(settings):
    r, parent = runnable(settings)
    child = spawn(r, parent)
    answer = "😀" * 8100 + "\nSECOND SAFE CLAIM\nCLAIM TO AVOID"
    r.db.execute("UPDATE tasks SET status='done',result=? WHERE id=?", (answer, child))
    preview = r.execute("worker_results", {}, task_id=parent, call_id="preview")["data"]["children"][0]
    assert preview["result"] == answer[:750]
    assert preview["result_truncated"] and preview["result_length"] == len(answer)
    collected, offset, hashes = preview["result"], preview["next_offset"], {preview["result_sha256"]}
    while offset is not None:
        page = read_result(r, parent, child, offset, call=f"page-{offset}")
        assert page == read_result(r, parent, child, offset, call=f"page-{offset}")
        assert page == read_result(r, parent, child, offset, call=f"fresh-{offset}")
        assert len(page["result"]) <= 4000
        assert len(json.dumps({"data": page})) < 50000
        hashes.add(page["result_sha256"])
        collected += page["result"]
        assert page["has_more"] == (page["next_offset"] is not None)
        offset = page["next_offset"]
    assert len(hashes) == 1 and collected == answer
    end = read_result(r, parent, child, len(answer), call="end")
    assert end["result"] == "" and not end["has_more"]
    assert end["next_offset"] is None


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_empty_terminal_result(settings, status):
    r, parent = runnable(settings)
    child = spawn(r, parent)
    r.db.execute("UPDATE tasks SET status=?,error='private error' WHERE id=?", (status, child))
    page = read_result(r, parent, child)
    assert page["status"] == status
    assert page["result"] == "" and page["result_length"] == 0
    assert not page["has_more"] and page["next_offset"] is None
    assert "private error" not in json.dumps(page)


@pytest.mark.parametrize(
    "offset,limit", [(-1, 1), (0, 0), (0, 4001), (4, 1), ("1.5", 1), ("", 1), (True, 1), ("9" * 11, 1)]
)
def test_result_page_rejects_bad_ranges(settings, offset, limit):
    r, parent = runnable(settings)
    child = spawn(r, parent)
    r.db.execute("UPDATE tasks SET status='done',result='abc' WHERE id=?", (child,))
    with pytest.raises(ValueError):
        read_result(r, parent, child, offset, limit)
    assert not r.db.one("SELECT 1 FROM tool_runs WHERE call_id='page'")


def test_result_access_enforces_direct_relationship_owner_and_grants(settings):
    r, parent = runnable(settings)
    child = spawn(r, parent, "child")
    sibling = spawn(r, parent, "sibling")
    active(r, child)
    active(r, sibling)
    grandchild = spawn(r, child, "grandchild")
    stranger = r.db.create_task("other", "other", "strategist", True)
    active(r, stranger)
    for caller, target in [
        (sibling, child),
        (child, parent),
        (parent, grandchild),
        (stranger, child),
        (parent, "missing"),
    ]:
        with pytest.raises(ValueError, match="owned direct child"):
            read_result(r, caller, target)
    with pytest.raises(ValueError, match="not final"):
        read_result(r, parent, child)
    r.db.execute("UPDATE tasks SET status='done',result='private',owner_id='other' WHERE id=?", (child,))
    with pytest.raises(ValueError, match="owned direct child"):
        read_result(r, parent, child)
    assert child not in {
        c["id"] for c in r.execute("worker_results", {}, task_id=parent, call_id="list")["data"]["children"]
    }
    r.db.execute("UPDATE tasks SET owner_id='owner' WHERE id=?", (child,))
    r.db.execute("UPDATE worker_nodes SET tools=? WHERE task_id=?", (json.dumps(["worker_results"]), parent))
    with pytest.raises(ValueError, match="inherited worker permissions"):
        read_result(r, parent, child)
    assert "worker_result_read" not in {t["name"] for t in r.definitions(parent)}


def test_result_redacts_before_preview_and_page_boundaries(settings):
    r, parent = runnable(settings)
    child = spawn(r, parent)
    secret = settings.admin_password
    raw = "a" * 740 + secret + "b" * 3245 + secret + "TAIL" * 30
    r.db.execute("UPDATE tasks SET status='done',result=? WHERE id=?", (raw, child))
    expected = raw.replace(secret, "[redacted]")
    preview = r.execute("worker_results", {}, task_id=parent, call_id="preview")["data"]["children"][0]
    assert preview["result"] == expected[:750]
    first = read_result(r, parent, child)
    last = read_result(r, parent, child, first["next_offset"], call="tail")
    assert first["result"] + last["result"] == expected
    assert first["result_length"] == len(expected)
    assert secret not in json.dumps(r.db.all("SELECT result FROM tool_runs"))


def test_result_pages_are_read_observations_and_survive_backup(settings, tmp_path):
    import sqlite3
    from agent4good.execution import ExecutionJournal
    from agent4good.tools import SPECS

    r, parent = runnable(settings)
    child = spawn(r, parent)
    r.db.execute("UPDATE tasks SET status='done',result=? WHERE id=?", ("text" * 1200, child))
    args = {"child_id": child, "offset": "4000", "limit": "4000"}
    assert SPECS["worker_result_read"].action_class == "READ"
    journal = ExecutionJournal(r.db, settings)
    for call_id in ["one", "two"]:
        journal.checkpoint(
            parent, [], [{"name": "worker_result_read", "arguments": json.dumps(args), "call_id": call_id}]
        )
    assert len(r.db.all("SELECT * FROM plan_steps WHERE tool='worker_result_read'")) == 2
    with sqlite3.connect(r.db.path) as source, sqlite3.connect(tmp_path / "result-backup.sqlite3") as dest:
        source.backup(dest)
    restored = ToolRegistry(settings, Database(tmp_path / "result-backup.sqlite3"))
    assert read_result(restored, parent, child, 4000)["result"] == "text" * 200
