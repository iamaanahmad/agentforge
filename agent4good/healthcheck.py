"""Non-mutating worker health probe for service supervisors."""

import sqlite3
from datetime import datetime, timezone
from .config import Settings


def main():
    path = Settings().data_dir / "agent4good.sqlite3"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='worker_last_seen'").fetchone()
    if not row or (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() > 30:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
