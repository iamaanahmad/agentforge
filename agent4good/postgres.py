"""PostgreSQL persistence and durable queue, with short serialized control transactions.

The transaction advisory lock preserves the existing SQLite budget/policy contract.
It is never held during provider or tool calls. Task session locks and expiring,
monotonic leases fence runners; every scoped transaction checks its fence.
"""

import hashlib
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import psycopg

from .db import Database, now, uid

CONTROL_LOCK = 41004001
RUN = ContextVar("distributed_run", default=None)


class LeaseLost(RuntimeError):
    def __init__(self):
        super().__init__("Worker lease lost; saved work requires recovery")


class QueueFull(RuntimeError):
    def __init__(self):
        super().__init__("Work queue is full; wait for current work to finish")


class Row(dict):
    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self.values_tuple = values

    def __getitem__(self, key):
        return self.values_tuple[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


class Connection:
    """Small adapter for the application's fixed SQL, never user-authored SQL."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        if sql == "BEGIN IMMEDIATE":
            # connect() has already acquired the transaction lock.
            return self.raw.execute("SELECT 1")
        if sql.startswith("INSERT OR IGNORE INTO "):
            sql = sql.replace("INSERT OR IGNORE", "INSERT", 1) + " ON CONFLICT DO NOTHING"
        if sql.startswith("INSERT OR REPLACE INTO "):
            table = sql.split()[4]
            columns = {
                "credentials": ("name", ["key_id", "nonce", "ciphertext", "metadata"]),
                "rate_limits": ("key", ["attempts", "resets"]),
            }
            key, fields = columns[table]
            sql = sql.replace("INSERT OR REPLACE", "INSERT", 1)
            sql += f" ON CONFLICT ({key}) DO UPDATE SET " + ",".join(f"{f}=excluded.{f}" for f in fields)
        for field in ("provider", "model"):
            sql = sql.replace(f"json_extract(r.profile,'$.{field}')", f"(r.profile::jsonb->>'{field}')")
        return self.raw.execute(sql.replace("?", "%s"), args or None)


class PostgresDatabase(Database):
    distributed = True

    def __init__(self, settings, initialize=True):
        self.config = settings
        self.worker_id = socket.gethostname() + "-" + uid("worker")
        self.claims = {}
        if initialize:
            with self.connect() as conn:
                exists = conn.execute("SELECT to_regclass('schema_version')").fetchone()[0]
                if exists is None:
                    conn.raw.execute((Path(__file__).parent / "postgres.sql").read_text())
                version = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()[0]
                if version not in {1, 2, 3, 4, 5}:
                    raise RuntimeError("Unsupported PostgreSQL schema version")
                from .coordination import initialize

                initialize(conn)
                from .memory import initialize as initialize_memory

                initialize_memory(conn)
                from .missions import initialize as initialize_missions

                initialize_missions(conn)
                from .scheduling import initialize as initialize_scheduling

                initialize_scheduling(conn)
                conn.execute("UPDATE schema_version SET version=5 WHERE id=1")
                for key, value in {"name": "Agent4Good", "goal": "", "autonomy": "supervised"}.items():
                    conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value))
                conn.execute("INSERT OR IGNORE INTO action_policy VALUES (1,1,'{}')")

    def raw(self, **kwargs):
        from psycopg.conninfo import conninfo_to_dict

        existing = conninfo_to_dict(self.config.database_url).get("options", "")
        return psycopg.connect(
            self.config.database_url,
            connect_timeout=5,
            options=existing
            + " -c statement_timeout=15000 -c lock_timeout=10000 -c idle_in_transaction_session_timeout=30000",
            row_factory=row_factory,
            **kwargs,
        )

    def _fence(self, raw):
        scope = RUN.get()
        if scope is None:
            return
        task_id, owner, fence = scope
        if not raw.execute(
            "SELECT 1 FROM worker_leases WHERE task_id=%s AND owner=%s AND fence=%s "
            "AND expires>EXTRACT(EPOCH FROM clock_timestamp())",
            (task_id, owner, fence),
        ).fetchone():
            raise LeaseLost()

    @contextmanager
    def connect(self):
        with self.raw() as raw:
            raw.execute("SELECT pg_advisory_xact_lock(%s)", (CONTROL_LOCK,))
            self._fence(raw)
            raw.execute("SELECT set_config('a4g.queue_limit',%s,true)", (str(self.config.max_queued_tasks),))
            try:
                yield Connection(raw)
                self._fence(raw)
            except psycopg.errors.RaiseException:
                raise QueueFull() from None

    @contextmanager
    def task_lock(self, task_id):
        key = int.from_bytes(hashlib.sha256(task_id.encode()).digest()[:8], "big", signed=True)
        with self.raw(autocommit=True) as conn:
            acquired = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
            try:
                yield acquired
            finally:
                if acquired and not conn.closed:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (key,))

    @contextmanager
    def run_scope(self, task_id):
        fence = self.claims.get(task_id)
        if fence is None:
            raise LeaseLost()
        token = RUN.set((task_id, self.worker_id, fence))
        try:
            with self.connect():
                pass
            yield
        finally:
            RUN.reset(token)
            self.claims.pop(task_id, None)
            with self.connect() as conn:
                conn.execute(
                    "UPDATE worker_leases SET expires=0 WHERE task_id=? AND owner=? AND fence=?",
                    (task_id, self.worker_id, fence),
                )

    def claim(self, settings):
        with self.connect() as conn:
            from .coordination import settle

            settle(conn)
            conn.execute(
                "UPDATE tasks SET status='failed',error='Task is outside the owner boundary',updated_at=? "
                "WHERE status='queued' AND owner_id!='owner'",
                (now(),),
            )
            running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
            daily = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind='started' AND created_at>=?", (now()[:10],)
            ).fetchone()[0]
            if running >= settings.max_concurrent_runs or daily >= settings.max_daily_runs:
                return None
            row = conn.execute(
                "SELECT t.id FROM tasks t LEFT JOIN worker_nodes n ON t.id=n.task_id WHERE t.status='queued' AND t.owner_id='owner' AND NOT EXISTS (SELECT 1 FROM mission_nodes mn JOIN missions mm ON mm.id=mn.mission_id JOIN worker_nodes wn ON wn.root_id=mn.task_id WHERE wn.task_id=t.id AND mm.status!='running') "
                "ORDER BY COALESCE(n.priority,0) DESC,t.created_at,t.id LIMIT 1 FOR UPDATE OF t SKIP LOCKED"
            ).fetchone()
            if not row:
                return None
            task_id = row["id"]
            lease = conn.execute(
                "INSERT INTO worker_leases(task_id,owner,fence,expires) "
                "VALUES (?,?,1,EXTRACT(EPOCH FROM clock_timestamp())+?) "
                "ON CONFLICT(task_id) DO UPDATE SET owner=excluded.owner, "
                "fence=worker_leases.fence+1,expires=excluded.expires RETURNING fence",
                (task_id, self.worker_id, settings.worker_lease_seconds),
            ).fetchone()
            conn.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (now(), task_id))
            conn.execute(
                "INSERT INTO events(task_id,kind,message,created_at) VALUES (?,?,?,?)",
                (task_id, "started", "Agent run started", now()),
            )
        self.claims[task_id] = lease["fence"]
        return task_id

    def heartbeat(self):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO workers(id,last_seen) VALUES (?,EXTRACT(EPOCH FROM clock_timestamp())) "
                "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
                (self.worker_id,),
            )
            for task_id, fence in list(self.claims.items()):
                conn.execute(
                    "UPDATE worker_leases SET expires=EXTRACT(EPOCH FROM clock_timestamp())+? "
                    "WHERE task_id=? AND owner=? AND fence=? AND expires>EXTRACT(EPOCH FROM clock_timestamp())",
                    (self.config.worker_lease_seconds, task_id, self.worker_id, fence),
                )
            conn.execute(
                "INSERT INTO settings VALUES ('worker_last_seen',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now(),),
            )
            conn.execute("DELETE FROM workers WHERE last_seen<EXTRACT(EPOCH FROM clock_timestamp())-86400")

    def expire_claim(self, task_id):
        # Caller holds the session task lock. Never recover a live lease.
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM worker_leases WHERE task_id=?", (task_id,)).fetchone()
            ts = conn.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())").fetchone()[0]
            if row and row["expires"] > ts:
                return False
            conn.execute("UPDATE worker_leases SET fence=fence+1,expires=0 WHERE task_id=?", (task_id,))
            return True

    def metrics(self):
        with self.connect() as conn:
            return {
                "queue_depth": conn.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0],
                "running": conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0],
                "failed": conn.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'").fetchone()[0],
                "workers_online": conn.execute(
                    "SELECT COUNT(*) FROM workers WHERE last_seen>EXTRACT(EPOCH FROM clock_timestamp())-30"
                ).fetchone()[0],
                "expired_leases": conn.execute(
                    "SELECT COUNT(*) FROM worker_leases l JOIN tasks t ON t.id=l.task_id "
                    "WHERE t.status='running' AND l.expires<EXTRACT(EPOCH FROM clock_timestamp())"
                ).fetchone()[0],
            }
