"""Non-mutating worker health probe for service supervisors."""

import sqlite3
from datetime import datetime, timezone
from .config import Settings


def main():
    settings = Settings()
    if settings.database_url:
        import socket
        from .postgres import PostgresDatabase

        db = PostgresDatabase(settings, initialize=False)
        row = db.one(
            "SELECT 1 FROM workers WHERE id LIKE ? AND last_seen>EXTRACT(EPOCH FROM clock_timestamp())-30",
            (socket.gethostname() + "-%",),
        )
        if not row:
            raise SystemExit(1)
        return
    path = settings.data_dir / "agent4good.sqlite3"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='worker_last_seen'").fetchone()
    if not row or (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() > 30:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
