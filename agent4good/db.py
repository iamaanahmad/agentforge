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
                CREATE TABLE IF NOT EXISTS action_policy (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS policy_approvals (approval_id TEXT PRIMARY KEY REFERENCES approvals(id), fingerprint TEXT NOT NULL, expires REAL NOT NULL, signature TEXT NOT NULL, effect TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS policy_legacy (approval_id TEXT PRIMARY KEY REFERENCES approvals(id));
                CREATE TABLE IF NOT EXISTS policy_usage (id INTEGER PRIMARY KEY, bucket TEXT NOT NULL, created_at REAL NOT NULL, calls INTEGER NOT NULL, cost INTEGER NOT NULL, recipients INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS policy_usage_window ON policy_usage(bucket,created_at);
                CREATE TABLE IF NOT EXISTS security_domain (id INTEGER PRIMARY KEY CHECK(id=1), tenant TEXT NOT NULL, environment TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS credentials (name TEXT PRIMARY KEY, key_id TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS executions (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id), version INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT 'executing',
                    started_at TEXT NOT NULL, recoveries INTEGER NOT NULL DEFAULT 0,
                    model_failures INTEGER NOT NULL DEFAULT 0, final_text TEXT, verification TEXT);
                CREATE TABLE IF NOT EXISTS plan_steps (
                    task_id TEXT NOT NULL REFERENCES tasks(id), action_id TEXT NOT NULL,
                    action_key TEXT NOT NULL, fingerprint TEXT NOT NULL, tool TEXT NOT NULL,
                    revision INTEGER NOT NULL, depends_on TEXT, status TEXT NOT NULL DEFAULT 'pending',
                    observation TEXT, PRIMARY KEY(task_id,action_id), UNIQUE(task_id,action_key));
                CREATE TRIGGER IF NOT EXISTS execution_task_phase AFTER UPDATE OF status ON tasks
                WHEN NEW.status IN ('queued','waiting_approval','failed','cancelled')
                BEGIN
                    UPDATE executions SET phase=NEW.status WHERE task_id=NEW.id;
                END;
                CREATE TABLE IF NOT EXISTS webhook_receipts (delivery_id TEXT PRIMARY KEY, received REAL NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id));

            """)
            conn.execute("BEGIN IMMEDIATE")
            if "owner_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}:
                conn.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'owner'")
            if not conn.execute("SELECT 1 FROM action_policy").fetchone():
                conn.execute("INSERT INTO action_policy VALUES (1,1,'{}')")
                conn.execute("INSERT INTO policy_legacy SELECT id FROM approvals")
            if "attempts" not in {row["name"] for row in conn.execute("PRAGMA table_info(tool_runs)")}:
                conn.execute("ALTER TABLE tool_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")
            conn.execute("PRAGMA user_version=4")
            for key, value in {"name": "Agent4Good", "goal": "", "autonomy": "supervised"}.items():
                conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value))

    def bind_security_domain(self, settings):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM security_domain WHERE id=1").fetchone()
            if row and (row["tenant"], row["environment"]) != (settings.tenant_id, settings.environment):
                raise RuntimeError("Database belongs to another tenant or environment")
            if not row:
                conn.execute(
                    "INSERT INTO security_domain VALUES (1,?,?)", (settings.tenant_id, settings.environment)
                )

    def require_owner(self, task):
        if not task or task["owner_id"] != "owner":
            raise RuntimeError("Task is outside the owner boundary")

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
        sql = "SELECT * FROM approvals WHERE task_id IN (SELECT id FROM tasks WHERE owner_id='owner')"
        rows = self.all(
            sql + (" AND task_id=?" if task_id else "") + " ORDER BY created_at DESC",
            (task_id,) if task_id else (),
        )
        for row in rows:
            row["arguments"] = json.loads(row["arguments"])
            binding = self.one(
                "SELECT effect,expires FROM policy_approvals WHERE approval_id=?", (row["id"],)
            )
            row["policy"] = binding
        return rows
