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


def test_layered_memory_runtime_backup_restore(distributed, tmp_path):
    from agent4good.memory import MemoryStore

    settings, db = distributed
    store = MemoryStore(db)
    first = store.save(
        {"key": "architecture", "content": "PostgreSQL queue leases", "source": "docs/architecture.md"}
    )
    current = store.save(
        {
            "key": "architecture",
            "content": "PostgreSQL durable leases",
            "source": "owner correction",
            "supersedes": first["id"],
        }
    )
    t = db.create_task("PostgreSQL review", "Review PostgreSQL leases", "strategist", True)
    engine = Engine(db, settings, FinalProvider())
    assert engine.claim() == t
    engine.run(t)
    assert db.task(t)["status"] == "done", db.task(t)["error"]
    assert {d["layer"] for d in store.inspect(task_id=t)} == {"working", "episodic", "semantic"}
    backup = tmp_path / "memory-backup.json"
    backup_postgres(db, backup)
    document = json.loads(backup.read_text())
    assert document["version"] == 8
    assert len(document["tables"]["memory_records"]) >= 4
    # Restore into a second disposable schema using the same domain and object prefix.
    from psycopg.conninfo import make_conninfo

    schema = "restored_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        target_settings = settings.model_copy(
            update={"database_url": make_conninfo(DSN, options=f"-c search_path={schema}")}
        )
        target = PostgresDatabase(target_settings)
        restore_postgres(target, backup)
        records = MemoryStore(target).search(t, "PostgreSQL", 3000)["records"]
        assert current["id"] in {d["id"] for d in records}
        assert first["id"] not in {d["id"] for d in records}
        target.execute("UPDATE memory_records SET document='corrupt' WHERE id=?", (current["id"],))
        assert current["id"] not in {d["id"] for d in MemoryStore(target).search(t, "PostgreSQL")["records"]}
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_mission_postgres_workers_objects_and_restore(distributed, tmp_path):
    from agent4good import missions
    from test_missions import spec, step
    from test_engine import FakeProvider, answer, call
    from psycopg.conninfo import make_conninfo

    settings, db = distributed
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        mid = missions.create(conn, spec())
        missions.apply_plan(
            conn,
            db,
            mid,
            missions.Plan(expected_revision=0, reason="Real PostgreSQL mission", steps=[step()]),
        )
        missions.control(conn, db, mid, "start")
    e = Engine(
        db,
        settings,
        FakeProvider(
            answer(calls=[call("artifact_write", {"name": "report.md", "content": "checked fact"})]),
            answer("Saved"),
        ),
    )
    task = e.claim()
    e.run(task)
    e.claim()
    assert db.task(task)["status"] == "done", db.task(task)["error"]
    assert db.one("SELECT status FROM missions")["status"] == "done"
    assert db.one("SELECT content FROM artifacts")["content"] == ""  # real private object
    backup = tmp_path / "mission.json"
    backup_postgres(db, backup)
    schema = "mission_restore_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        target = PostgresDatabase(
            settings.model_copy(
                update={"database_url": make_conninfo(DSN, options=f"-c search_path={schema}")}
            )
        )
        restore_postgres(target, backup)
        with target.connect() as conn:
            d = missions.detail(conn, mid)
        assert d["verification"][0]["met"]
        assert d["revision"] == 1 and d["status"] == "done"
        assert len(target.all("SELECT * FROM mission_plans")) == 1
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_scheduler_modes_concurrency_and_restore(distributed, tmp_path):
    from agent4good import scheduling as sch

    settings, db = distributed
    db.execute("UPDATE settings SET value='autonomous' WHERE key='autonomy'")
    source = db.create_task("Source", "Supplied facts", "strategist")
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (source,))
    ids = []
    for mode in ["once", "recurring", "deadline", "event", "condition"]:
        data = dict(
            name="[TEST] " + mode, prompt="Supplied facts", mode=mode, depends_on=[source], priority=7
        )
        if mode in {"once", "deadline"}:
            data["at"] = "2020-01-01T00:00:00+00:00"
        if mode == "condition":
            data["condition"] = {"task_id": source}
        with db.connect() as conn:
            sid = sch.create(conn, sch.ScheduleInput(**data))
            conn.execute("UPDATE schedules SET next_run_at=? WHERE id=?", ("2020-01-01T00:00:00+00:00", sid))
            if mode == "event":
                assert sch.event(conn, sid, "delivery")
                assert not sch.event(conn, sid, "delivery")
        ids.append(sid)

    def dispatch(_):
        worker = PostgresDatabase(settings)
        return Engine(worker, settings, FinalProvider()).schedule_due()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(dispatch, range(8))) == 5
    assert len(db.all("SELECT * FROM schedule_occurrences")) == 5
    for _ in range(5):
        engine = Engine(db, settings, FinalProvider())
        tid = engine.claim()
        assert tid
        engine.run(tid)
        assert db.task(tid)["status"] == "done"
    assert dispatch(0) == 0
    backup = tmp_path / "scheduler-backup.json"
    backup_postgres(db, backup)
    schema = "scheduler_restore_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        from psycopg.conninfo import make_conninfo

        target_settings = settings.model_copy(
            update={"database_url": make_conninfo(DSN, options=f"-c search_path={schema}")}
        )
        target = PostgresDatabase(target_settings)
        restore_postgres(target, backup)
        assert Engine(target, target_settings, FinalProvider()).schedule_due() == 0
        assert len(target.all("SELECT * FROM schedule_occurrences")) == 5
        assert len(target.all("SELECT * FROM schedule_events")) == 1
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_quality_postgres_workers_evidence_and_restore(distributed, tmp_path):
    from test_quality import Provider, contract, drain
    from psycopg.conninfo import make_conninfo

    settings, db = distributed
    settings.max_daily_runs = 100
    tid = db.create_task("Tagged quality PG", "Write accurate copy", "strategist", True, quality=contract())
    provider = Provider(db)
    drain(db, settings, provider)
    assert db.task(tid)["status"] == "done", db.task(tid)["error"]
    assert len(db.all("SELECT * FROM quality_reviews")) == 3
    backup = tmp_path / "quality-backup.json"
    backup_postgres(db, backup)
    schema = "restored_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        target_settings = settings.model_copy(
            update={"database_url": make_conninfo(DSN, options=f"-c search_path={schema}")}
        )
        target = PostgresDatabase(target_settings)
        restore_postgres(target, backup)
        assert target.task(tid)["status"] == "done"
        assert target.all("SELECT * FROM quality_rounds ORDER BY attempt") == db.all(
            "SELECT * FROM quality_rounds ORDER BY attempt"
        )
        assert target.all("SELECT * FROM quality_reviews ORDER BY task_id") == db.all(
            "SELECT * FROM quality_reviews ORDER BY task_id"
        )
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_learning_postgres_reuse_correction_and_restore(distributed, tmp_path):
    from agent4good.learning import LearningStore
    from psycopg.conninfo import make_conninfo
    from test_engine import FakeProvider, answer

    settings, db = distributed
    tid = db.create_task("PostgreSQL learning", "PostgreSQL learning", "strategist", True)
    engine = Engine(db, settings, FakeProvider(answer("PostgreSQL lesson observed")))
    assert engine.claim() == tid
    engine.run(tid)
    store = LearningStore(db)
    outcome = store.inspect(tid)[0]
    later = db.create_task("PostgreSQL learning", "PostgreSQL learning", "strategist", True)
    provider = FakeProvider(
        answer(f"Learning use: {outcome['id']}: Reuse the documented PostgreSQL approach.")
    )
    engine = Engine(db, settings, provider)
    assert engine.claim() == later
    engine.run(later)
    assert db.task(later)["status"] == "done", db.task(later)["error"]
    assert db.all("SELECT * FROM learning_uses WHERE task_id=?", (later,))
    store.correct(
        outcome["id"],
        {"kind": "invalidation", "text": "PostgreSQL observation contradicted", "source": "owner check"},
    )
    assert not store.search(later, "PostgreSQL", 4000)["records"]
    backup = tmp_path / "learning.json"
    backup_postgres(db, backup)
    schema = "restored_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        target = PostgresDatabase(
            settings.model_copy(
                update={"database_url": make_conninfo(DSN, options=f"-c search_path={schema}")}
            )
        )
        restore_postgres(target, backup)
        assert LearningStore(target).inspect(tid) == store.inspect(tid)
        assert target.all("SELECT * FROM learning_uses ORDER BY id") == db.all(
            "SELECT * FROM learning_uses ORDER BY id"
        )
        assert target.all("SELECT * FROM learning_evidence ORDER BY sha256") == db.all(
            "SELECT * FROM learning_evidence ORDER BY sha256"
        )
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_timeline_postgres_cursor_owner_and_restore(distributed, tmp_path):
    from agent4good.timeline import read
    from agent4good.tools import ToolRegistry

    settings, db = distributed
    ident = task(db)
    engine = Engine(db, settings, provider=FinalProvider())
    assert engine.claim() == ident
    engine.run(ident)
    credentials = ToolRegistry(settings, db).credentials
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda n: db.event(ident, "acceptance", str(n)), range(12)))
    page = read(db, credentials, task_id=ident, limit=3)
    collected = page["events"][:]
    while page["has_more"]:
        page = read(
            db,
            credentials,
            task_id=ident,
            limit=3,
            after=page["next_cursor"],
            through=page["through"],
        )
        collected.extend(page["events"])
    ids = [event["id"] for event in collected]
    assert ids == sorted(set(ids))
    assert ids == [row["id"] for row in db.all("SELECT id FROM events WHERE task_id=? ORDER BY id", (ident,))]
    assert page["tasks"][0]["result"] == "Verified scripted completion"
    reopened = PostgresDatabase(settings)
    assert read(reopened, credentials, task_id=ident, after=page["next_cursor"])["events"] == []
    foreign = task(db)
    db.execute("UPDATE tasks SET owner_id='other' WHERE id=?", (foreign,))
    with pytest.raises(LookupError):
        read(db, credentials, task_id=foreign)


def test_developer_postgres_projection_and_worker_readiness(distributed):
    from agent4good.developer import replay, worker_readiness
    from agent4good.tools import ToolRegistry

    settings, db = distributed
    registry = ToolRegistry(settings, db)
    assert not worker_readiness(db)["online"]
    db.heartbeat()
    assert worker_readiness(db)["online"]
    task_id = db.create_task("Developer PostgreSQL", "Inspect saved receipts", "strategist")
    db.execute(
        "INSERT INTO tool_runs(task_id,call_id,tool,arguments,status,result) VALUES (?,?,?,?,?,?)",
        (task_id, "saved", "email_send", "{}", "done", "accepted receipt"),
    )
    doc = replay(db, registry.credentials, task_id)
    assert doc["receipts"][0]["result"] == "accepted receipt"
    assert doc["external_effects"] is False
    assert db.task(task_id)["status"] == "draft"


def test_conversations_postgres_question_recovery_and_backup(distributed, tmp_path):
    from agent4good import conversations
    from test_engine import FakeProvider, answer, call
    from psycopg.conninfo import make_conninfo

    settings, db = distributed
    tid = db.create_task("Tagged chat PG", "Ask a question", "strategist", True)
    engine = Engine(
        db, settings, FakeProvider(answer(calls=[call("ask_owner", {"question": "Which audience?"})]))
    )
    assert engine.claim() == tid
    engine.run(tid)
    assert db.task(tid)["status"] == "waiting_input"
    q = db.one("SELECT * FROM owner_questions")
    conversations.answer(db, tid, q["id"], "Developers")
    engine = Engine(db, settings, FakeProvider(answer("For developers")))
    assert engine.claim() == tid
    engine.run(tid)
    assert db.task(tid)["status"] == "done"
    follow = conversations.follow_up(db, tid, "request-0001", "Explain", "ask")
    assert conversations.follow_up(db, tid, "request-0001", "Explain", "ask") == follow
    backup = tmp_path / "chat-backup.json"
    backup_postgres(db, backup)
    schema = "restored_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        target = PostgresDatabase(
            settings.model_copy(
                update={"database_url": make_conninfo(DSN, options=f"-c search_path={schema}")}
            )
        )
        restore_postgres(target, backup)
        assert target.all("SELECT * FROM owner_questions") == db.all("SELECT * FROM owner_questions")
        assert target.all("SELECT * FROM conversation_turns") == db.all("SELECT * FROM conversation_turns")
        assert conversations.transcript(target, tid)["root_id"] == tid
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
