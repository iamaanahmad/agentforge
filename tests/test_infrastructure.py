"""Real PostgreSQL/MinIO tests. CI supplies disposable service containers.

Run with A4G_TEST_DATABASE_URL; the named database MUST be disposable. Each test
uses a fresh schema. Model answers are scripted; no provider spend or customer contact.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from agent4good.app import create_app
from agent4good.artifacts import ObjectStore, read_artifact, save_artifact
from agent4good.db import Database, now
from agent4good.engine import Engine
from agent4good.postgres import LeaseLost, PostgresDatabase, QueueFull
from agent4good.transfer import backup_postgres, migrate_sqlite, restore_postgres

DSN = os.getenv("A4G_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="Requires a disposable PostgreSQL service")


@pytest.fixture
def distributed(settings):
    from psycopg.conninfo import make_conninfo

    schema = "test_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    # Separate tests retain parallel worker access to the same schema.
    settings = settings.model_copy(
        update={
            "database_url": make_conninfo(DSN, options=f"-c search_path={schema}"),
            "s3_bucket": "a4g-infrastructure-test",
            "s3_endpoint": os.getenv("A4G_TEST_S3_ENDPOINT", "http://127.0.0.1:59000"),
            "s3_access_key": os.getenv("A4G_TEST_S3_ACCESS_KEY", "test-only-access"),
            "s3_secret_key": os.getenv("A4G_TEST_S3_SECRET_KEY", "test-only-secret-never-production"),
            "environment": schema,
            "worker_lease_seconds": 15,
            "worker_poll_seconds": 0.2,
        }
    )
    yield settings, PostgresDatabase(settings)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


class FinalProvider:
    def respond(self, *args, **kwargs):
        return {"output": [], "output_text": "Verified scripted completion", "usage": {}}


def task(db):
    return db.create_task("Infrastructure acceptance", "Tagged internal test", "strategist", start=True)


def test_atomic_claims_and_global_backpressure(distributed):
    settings, db = distributed
    ids = {task(db) for _ in range(10)}
    workers = [PostgresDatabase(settings) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda d: d.claim(settings), workers))
    claimed = [r for r in results if r]
    assert len(claimed) == len(set(claimed)) == settings.max_concurrent_runs
    assert set(claimed) <= ids
    assert db.metrics()["running"] == 4


def test_queue_admission_rolls_back(distributed):
    settings, db = distributed
    db.config = settings.model_copy(update={"max_queued_tasks": 1})
    task(db)
    with pytest.raises(QueueFull):
        task(db)
    assert db.metrics()["queue_depth"] == 1


def test_expired_worker_cannot_checkpoint_or_renew(distributed):
    settings, db = distributed
    t = task(db)
    assert db.claim(settings) == t
    with db.run_scope(t):
        other = PostgresDatabase(settings)
        with ThreadPoolExecutor(1) as pool:
            pool.submit(other.execute, "UPDATE worker_leases SET expires=0 WHERE task_id=?", (t,)).result()
        with pytest.raises(LeaseLost):
            db.execute("UPDATE tasks SET result='stale' WHERE id=?", (t,))
        # A thread does not inherit the execution context. Heartbeats cannot revive expired ownership.
        with ThreadPoolExecutor(1) as pool:
            pool.submit(db.heartbeat).result()
    assert db.task(t)["result"] == ""
    assert other.one("SELECT expires FROM worker_leases WHERE task_id=?", (t,))["expires"] == 0


def test_session_lock_and_live_lease_prevent_recovery(distributed):
    settings, db = distributed
    t = task(db)
    db.claim(settings)
    other = PostgresDatabase(settings)
    with db.task_lock(t) as acquired:
        assert acquired
        with other.task_lock(t) as second:
            assert not second
        Engine(other, settings, provider=FinalProvider()).recover()
    Engine(other, settings, provider=FinalProvider()).recover()
    assert db.task(t)["status"] == "running"


def test_crash_recovery_preserves_completed_receipts(distributed):
    settings, db = distributed
    t = task(db)
    db.claim(settings)
    Engine(db, settings, provider=FinalProvider())._save(t, [{"role": "user", "content": "test"}], [])
    db.execute(
        "INSERT INTO tool_runs(task_id,call_id,tool,arguments,status,result) VALUES (?,?,'artifact_write',?,'done',?)",
        (
            t,
            "saved",
            json.dumps({"name": "proof.txt", "content": "saved"}),
            json.dumps({"id": "artifact", "name": "proof.txt", "download": "/api/artifacts/artifact"}),
        ),
    )
    db.execute("UPDATE worker_leases SET expires=0 WHERE task_id=?", (t,))
    other = PostgresDatabase(settings)
    engine = Engine(other, settings, provider=FinalProvider())
    engine.recover()
    assert other.task(t)["status"] == "queued"
    assert engine.claim() == t
    engine.run(t)
    assert other.task(t)["status"] == "done"
    assert len(other.all("SELECT * FROM tool_runs WHERE task_id=?", (t,))) == 1
    assert other.one("SELECT recoveries FROM executions WHERE task_id=?", (t,))["recoveries"] == 1


def test_ambiguous_write_is_held_after_crash(distributed):
    settings, db = distributed
    t = task(db)
    db.claim(settings)
    db.execute(
        "INSERT INTO tool_runs(task_id,call_id,tool,arguments,status) VALUES (?,?,'send_email','{}','started')",
        (t, "ambiguous"),
    )
    db.execute("UPDATE worker_leases SET expires=0 WHERE task_id=?", (t,))
    Engine(PostgresDatabase(settings), settings, provider=FinalProvider()).recover()
    assert db.task(t)["status"] == "failed"
    assert "Inspect incomplete receipts" in db.task(t)["error"]


def test_schedule_coalescing_between_workers(distributed):
    settings, db = distributed
    db.execute(
        "INSERT INTO schedules VALUES (?,?,?,?,?,1,?,?)",
        ("schedule", "test", "test", "strategist", 15, "2000-01-01", now()),
    )
    with ThreadPoolExecutor(4) as pool:
        list(
            pool.map(
                lambda _: Engine(
                    PostgresDatabase(settings), settings, provider=FinalProvider()
                ).schedule_due(),
                range(4),
            )
        )
    assert len(db.all("SELECT * FROM tasks")) == 1


def test_model_budget_and_policy_compatibility(distributed):
    settings, db = distributed
    from agent4good.tools import ToolRegistry
    from agent4good.model_router import ModelRouter
    from agent4good.model_config import ModelProfile

    t = task(db)
    db.claim(settings)
    registry = ToolRegistry(settings, db)
    result = registry.execute("memory_read", {"key": "missing"}, task_id=t, call_id="read")
    assert result is not None
    router = ModelRouter(settings, registry.credentials, db)
    profile = ModelProfile(provider="openai", model="test-model")
    # Exercise the two independent aggregate columns and PostgreSQL policy auto IDs.
    assert router.reserve(t, profile, "test", [], [], None)
    assert db.one("SELECT COUNT(*) n FROM policy_usage")["n"] > 0


def test_sqlite_migration_is_exact_and_non_destructive(distributed, tmp_path):
    settings, db = distributed
    source = Database(tmp_path / "source.sqlite3")
    t = task(source)
    source.execute("INSERT INTO memory VALUES (?,?,?)", ("unicode", "Héllo 🟩", now()))
    source.execute(
        "INSERT INTO artifacts VALUES (?,?,?,?,?)", ("artifact", t, "proof.txt", "saved text", now())
    )
    source.execute(
        "INSERT INTO credentials VALUES (?,?,?,?,?)", ("test", "key", b"nonce", b"encrypted", "{}")
    )
    counts = migrate_sqlite(tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3", db)
    assert counts["tasks"] == 1
    assert db.task(t) == source.task(t)
    assert db.one("SELECT content FROM memory")["content"] == "Héllo 🟩"
    assert db.one("SELECT nonce FROM credentials")["nonce"] == b"nonce"
    assert read_artifact(db, settings, db.one("SELECT * FROM artifacts")) == "saved text"
    with pytest.raises(RuntimeError, match="empty destination"):
        migrate_sqlite(tmp_path / "source.sqlite3", tmp_path / "second.sqlite3", db)
    assert source.task(t)["status"] == "queued"
    db.event(t, "check", "identity sequence restored")
    assert len(db.all("SELECT * FROM events")) == 2


def test_real_object_backup_restore_and_corruption(distributed, tmp_path):
    settings, db = distributed
    store = ObjectStore(settings)
    try:
        store.client.head_bucket(Bucket=store.bucket)
    except Exception:
        store.client.create_bucket(Bucket=store.bucket)
    t = task(db)
    save_artifact(db, settings, "artifact-proof", t, "proof.txt", "Object storage acceptance 🟩")
    row = db.one("SELECT * FROM artifacts")
    assert row["content"] == ""
    assert read_artifact(db, settings, row) == "Object storage acceptance 🟩"
    backup_postgres(db, tmp_path / "backup.json")
    # Restore to a new database schema and a new empty bucket in the same domain.
    schema = "restore_" + uuid4().hex
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    target_settings = settings.model_copy(
        update={
            "database_url": make_conninfo(DSN, options=f"-c search_path={schema}"),
            "s3_bucket": "restore-" + uuid4().hex,
        }
    )
    target = PostgresDatabase(target_settings)
    target_store = ObjectStore(target_settings)
    target_store.client.create_bucket(Bucket=target_store.bucket)
    try:
        restore_postgres(target, tmp_path / "backup.json")
        assert target.task(t) == db.task(t)
        assert (
            read_artifact(target, target_settings, target.one("SELECT * FROM artifacts"))
            == "Object storage acceptance 🟩"
        )
        obj = target.one("SELECT * FROM artifact_objects")
        target_store.client.put_object(Bucket=target_store.bucket, Key=obj["object_key"], Body=b"corrupt")
        with pytest.raises(RuntimeError, match="integrity"):
            read_artifact(target, target_settings, target.one("SELECT * FROM artifacts"))
    finally:
        for obj in target_store.client.list_objects_v2(Bucket=target_store.bucket).get("Contents", []):
            target_store.client.delete_object(Bucket=target_store.bucket, Key=obj["Key"])
        target_store.client.delete_bucket(Bucket=target_store.bucket)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_control_api_uses_shared_postgres(distributed):
    settings, db = distributed
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/infrastructure").status_code == 401
        login = client.post("/api/login", json={"password": settings.admin_password})
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        reply = client.post(
            "/api/tasks",
            json={"title": "Internal proof", "prompt": "Test", "agent": "strategist", "start": False},
        )
        assert reply.status_code == 201
        assert len(db.all("SELECT * FROM tasks")) == 1
        db.heartbeat()
        assert client.get("/api/infrastructure").json()["metrics"]["workers_online"] == 1


def test_real_worker_process_kill_recovery_and_drain(distributed, tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    import time

    settings, db = distributed
    store = ObjectStore(settings)
    try:
        store.health()
    except Exception:
        store.client.create_bucket(Bucket=store.bucket)
    # Test settings carry only generated test credentials. Nothing is printed.
    values = settings.model_dump(mode="json")
    values.update(
        {
            name: getattr(settings, name)
            for name in ("database_url", "admin_password", "session_secret", "s3_access_key", "s3_secret_key")
        }
    )
    # The public setting accepts URLs; encode PostgreSQL schema options in a URL.
    from psycopg.conninfo import conninfo_to_dict
    from urllib.parse import quote

    parsed = conninfo_to_dict(settings.database_url)
    schema_options = parsed["options"]
    values["database_url"] = DSN + ("&" if "?" in DSN else "?") + "options=" + quote(schema_options)
    env = dict(os.environ, A4G_TEST_SETTINGS=json.dumps(values), A4G_TEST_FINAL_DELAY="60")
    log = (tmp_path / "worker.log").open("w")
    script = str(Path(__file__).parent / "distributed_worker_probe.py")
    first = subprocess.Popen([sys.executable, script], env=env, stdout=log, stderr=log)
    second = None

    def wait_for(predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        raise AssertionError("Worker acceptance timed out; inspect the test log")

    try:
        t = task(db)
        wait_for(lambda: db.one("SELECT 1 FROM tool_runs WHERE task_id=? AND status='done'", (t,)))
        first.kill()
        first.wait(timeout=10)
        # Do not shorten the real lease: prove expiry and reclaim after process death.
        env["A4G_TEST_FINAL_DELAY"] = "0"
        second = subprocess.Popen([sys.executable, script], env=env, stdout=log, stderr=log)
        wait_for(lambda: db.task(t)["status"] == "done", timeout=35)
        assert len(db.all("SELECT * FROM artifacts WHERE task_id=?", (t,))) == 1
        assert len(db.all("SELECT * FROM tool_runs WHERE task_id=?", (t,))) == 1
        assert (
            read_artifact(db, settings, db.one("SELECT * FROM artifacts WHERE task_id=?", (t,)))
            == "Real worker and object store acceptance"
        )
        # SIGTERM drains and clears the current worker's heartbeat.
        second.terminate()
        assert second.wait(timeout=15) == 0
        # The killed worker's heartbeat ages out; it cannot keep the system healthy indefinitely.
        assert db.one("SELECT recoveries FROM executions WHERE task_id=?", (t,))["recoveries"] == 1
    finally:
        for process in (first, second):
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        log.close()


def test_distributed_child_workers_and_backup(distributed, tmp_path):
    import threading
    from test_workers import spawn, context
    from agent4good.tools import ToolRegistry

    settings, db = distributed
    parent = task(db)
    assert db.claim(settings) == parent
    r = ToolRegistry(settings, db)
    a, b = spawn(r, parent, "alpha", priority=-1), spawn(r, parent, "beta", priority=1)
    db.execute("UPDATE tasks SET status='waiting_children' WHERE id=?", (parent,))
    barrier = threading.Barrier(2)

    class ConcurrentProvider:
        def respond(self, *args):
            barrier.wait(timeout=10)
            return {"output": [], "output_text": "real PostgreSQL worker evidence", "usage": {}}

    workers = [PostgresDatabase(settings), PostgresDatabase(settings)]
    ids = [w.claim(settings) for w in workers]
    assert ids == [b, a]
    context(r, a, "private", "write", "0", "private child")
    context(r, b, "shared", "write", "0", "shared tree")
    r.execute(
        "worker_message", {"recipient": parent, "content": "durable message"}, task_id=a, call_id="message"
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(Engine(w, settings, ConcurrentProvider()).run, t) for w, t in zip(workers, ids)
        ]
        for future in futures:
            future.result(timeout=20)
    assert all(db.task(t)["status"] == "done" for t in ids)
    assert db.claim(settings) == parent
    result = r.execute("worker_results", {}, task_id=parent, call_id="collect")
    assert len(result["data"]["children"]) == 2
    backup_postgres(db, tmp_path / "tree.json")
    document = json.loads((tmp_path / "tree.json").read_text())
    assert len(document["tables"]["worker_context"]) == 2
    assert len(document["tables"]["worker_nodes"]) == 3
    # Restore the entire populated tree to a second real schema.
    from psycopg.conninfo import make_conninfo

    schema = "tree_restore_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        target = PostgresDatabase(
            settings.model_copy(
                update={"database_url": make_conninfo(DSN, options=f"-c search_path={schema}")}
            )
        )
        restore_postgres(target, tmp_path / "tree.json")
        assert target.all("SELECT * FROM worker_messages") == db.all("SELECT * FROM worker_messages")
        assert len(target.all("SELECT * FROM worker_context")) == 2
        assert target.task(a)["result"] == db.task(a)["result"]
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
