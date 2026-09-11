import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f"{prefix}_{uuid4().hex}"


class Database:
    def __init__(self, path):
        self.path = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL, agent TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', steps INTEGER NOT NULL DEFAULT 0,
                    items TEXT NOT NULL DEFAULT '[]', pending TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(id), kind TEXT NOT NULL,
                    message TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), call_id TEXT NOT NULL,
                    tool TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL, decided_at TEXT, UNIQUE(task_id,call_id));
                CREATE TABLE IF NOT EXISTS tool_runs (
                    task_id TEXT NOT NULL REFERENCES tasks(id), call_id TEXT NOT NULL,
                    tool TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '', PRIMARY KEY(task_id,call_id));
                CREATE TABLE IF NOT EXISTS memory (key TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), name TEXT NOT NULL,
                    content TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL, agent TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS rate_limits (
                    key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, resets REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status,created_at);
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id,id);
                PRAGMA user_version=1;
            """)
            for key, value in {"name": "Agent4Good", "goal": "", "autonomy": "supervised"}.items():
                conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def all(self, sql, args=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connect() as conn:
            return conn.execute(sql, args).rowcount

    def event(self, task_id, kind, message):
        self.execute(
            "INSERT INTO events(task_id,kind,message,created_at) VALUES (?,?,?,?)",
            (task_id, kind, message[:12000], now()),
        )

    def settings(self):
        return {row["key"]: row["value"] for row in self.all("SELECT * FROM settings")}

    def task(self, task_id):
        return self.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def create_task(self, title, prompt, agent, start=False, conn=None):
        task_id, ts = uid("task"), now()
        args = (task_id, title, prompt, agent, "queued" if start else "draft", ts, ts)
        sql = "INSERT INTO tasks(id,title,prompt,agent,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)"
        if conn:
            conn.execute(sql, args)
        else:
            self.execute(sql, args)
            self.event(task_id, "created", "Task created" + (" and queued" if start else ""))
        return task_id

    def public_task(self, row):
        return {k: v for k, v in row.items() if k not in {"items", "pending"}}

    def approval_list(self, task_id=None):
        sql = "SELECT * FROM approvals"
        rows = self.all(
            sql + (" WHERE task_id=?" if task_id else "") + " ORDER BY created_at DESC",
            (task_id,) if task_id else (),
        )
        for row in rows:
            row["arguments"] = json.loads(row["arguments"])
        return rows
