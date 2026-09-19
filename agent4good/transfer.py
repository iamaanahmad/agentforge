"""Versioned offline SQLite migration and portable PostgreSQL/object backup.

No destination records are overwritten. Restores require an empty initialized
application database. Backup files include private data and must be protected.
"""

import base64
import json
import os
import sqlite3
from pathlib import Path

from psycopg import sql

from .artifacts import ObjectStore
from .db import Database
from .missions import TABLES as MISSION_TABLES
from .scheduling import TABLES as SCHEDULE_TABLES
from .quality import TABLES as QUALITY_TABLES
from .learning import TABLES as LEARNING_TABLES
from .conversations import TABLES as CONVERSATION_TABLES

TABLES = [
    "settings",
    "tasks",
    "events",
    "approvals",
    "tool_runs",
    "memory",
    "artifacts",
    "schedules",
    "sessions",
    "rate_limits",
    "action_policy",
    "policy_approvals",
    "policy_legacy",
    "policy_usage",
    "security_domain",
    "credentials",
    "executions",
    "plan_steps",
    "webhook_receipts",
    "model_routes",
    "model_calls",
    "worker_nodes",
    "worker_context",
    "worker_messages",
    "worker_resources",
    "memory_records",
    "memory_migrations",
]


TABLES += MISSION_TABLES + SCHEDULE_TABLES + QUALITY_TABLES + LEARNING_TABLES + CONVERSATION_TABLES


def encode(value):
    return {"base64": base64.b64encode(value).decode()} if isinstance(value, bytes) else value


def decode(value):
    return base64.b64decode(value["base64"], validate=True) if isinstance(value, dict) else value


def records(conn, table):
    return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"').fetchall()]


def canonical(rows):
    return sorted(json.dumps({k: encode(v) for k, v in r.items()}, sort_keys=True) for r in rows)


def load_tables(db, tables, objects=None):
    with db.connect() as conn:
        # Default settings and an untouched policy are the only allowed rows.
        for table in TABLES + ["artifact_objects", "workers", "worker_leases"]:
            rows = records(conn, table)
            allowed = []
            if table == "settings":
                allowed = [
                    {"key": k, "value": v}
                    for k, v in {"name": "Agent4Good", "goal": "", "autonomy": "supervised"}.items()
                ]
            if table == "action_policy":
                allowed = [{"id": 1, "revision": 1, "document": "{}"}]
            if rows and canonical(rows) != canonical(allowed):
                raise RuntimeError("Restore requires an empty destination; existing data was not changed")
        conn.execute("DELETE FROM settings")
        conn.execute("DELETE FROM action_policy")
        # Bypass queue admission only for restoration; all other constraints remain active.
        conn.raw.execute("SELECT set_config('a4g.queue_limit','2147483647',true)")
        for table in TABLES + ["artifact_objects"]:
            rows = tables.get(table, [])
            for row in rows:
                names = list(row)
                statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, names)),
                    sql.SQL(",").join(sql.Placeholder() for _ in names),
                )
                conn.raw.execute(statement, [row[n] for n in names])
            if canonical(records(conn, table)) != canonical(rows):
                raise RuntimeError("Restored records differ from the source")
        from .memory import initialize as initialize_memory

        initialize_memory(conn)
        for table in ["events", "policy_usage"]:
            conn.raw.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence(%s,'id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM {}"
                ).format(sql.Identifier(table)),
                (table,),
            )
    return {table: len(rows) for table, rows in tables.items()}


def migrate_sqlite(source, backup, db):
    source, backup = Path(source).resolve(), Path(backup).resolve()
    if source == backup or backup.exists():
        raise ValueError("Use a new backup path outside the source database")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(backup) as dst:
        if src.execute("PRAGMA user_version").fetchone()[0] not in {5, 6, 7, 8, 9, 10, 11, 12}:
            raise ValueError(
                "Migration supports SQLite schema 5 through 12; upgrade the source offline first"
            )
        src.backup(dst)
        if (
            dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or dst.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("SQLite backup failed integrity checks")
    sqlite = Database(backup)
    tables = {table: sqlite.all(f'SELECT * FROM "{table}"') for table in TABLES}
    return load_tables(db, tables)


def backup_postgres(db, destination):
    with db.connect() as conn:
        tables = {table: records(conn, table) for table in TABLES + ["artifact_objects"]}
        objects = {}
        store = ObjectStore(db.config)
        for row in tables["artifact_objects"]:
            objects[row["object_key"]] = store.read(row["object_key"], row["sha256"])
        document = {"version": 8, "tables": tables, "objects": objects}
        data = json.dumps(document, default=encode).encode()
    # Exclusive create prevents accidentally replacing a previous backup.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {table: len(rows) for table, rows in tables.items()}


def restore_postgres(db, source):
    document = json.loads(
        Path(source).read_text(), object_hook=lambda x: decode(x) if set(x) == {"base64"} else x
    )
    workers = {"worker_nodes", "worker_context", "worker_messages", "worker_resources"}
    memory = {"memory_records", "memory_migrations"}
    expected = set(TABLES + ["artifact_objects"])
    missing = {
        1: workers | memory | set(MISSION_TABLES) | set(SCHEDULE_TABLES),
        2: memory | set(MISSION_TABLES) | set(SCHEDULE_TABLES),
        3: set(MISSION_TABLES) | set(SCHEDULE_TABLES),
        4: set(SCHEDULE_TABLES),
        5: set(),
    }.get(document.get("version"))
    if missing is not None:
        missing |= set(QUALITY_TABLES)
    if document.get("version") == 6:
        missing = set()
    if missing is not None:
        missing |= set(LEARNING_TABLES)
    if document.get("version") == 7:
        missing = set()
    if missing is not None:
        missing |= set(CONVERSATION_TABLES)
    if document.get("version") == 8:
        missing = set()
    if missing is None or set(document["tables"]) != expected - missing:
        raise ValueError("Unsupported backup format")
    document["tables"].update({table: [] for table in missing})
    # Object keys are content addressed. Never write unverified or arbitrary keys.
    store = ObjectStore(db.config)
    for row in document["tables"]["artifact_objects"]:
        content = document["objects"][row["object_key"]]
        import hashlib

        expected = store.prefix + row["artifact_id"] + "/" + hashlib.sha256(content.encode()).hexdigest()
        if row["object_key"] != expected or row["sha256"] != expected.rsplit("/", 1)[1]:
            raise ValueError("Backup object integrity or domain mismatch")
    # Empty-target refusal happens before any object mutation.
    with db.connect() as conn:
        if any(records(conn, t) for t in TABLES if t not in {"settings", "action_policy"}):
            raise RuntimeError("Restore requires an empty destination")
    for row in document["tables"]["artifact_objects"]:
        store.put(row["artifact_id"], document["objects"][row["object_key"]])
    return load_tables(db, document["tables"])
