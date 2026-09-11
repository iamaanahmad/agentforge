"""Consistent online SQLite backup. Usage: uv run python scripts/backup.py /secure/path/backup.sqlite3"""

import os
import sqlite3
import sys
from pathlib import Path

from agent4good.config import Settings

if len(sys.argv) != 2:
    raise SystemExit("Provide a new backup file path")
destination = Path(sys.argv[1]).resolve()
source = (Settings().data_dir / "agent4good.sqlite3").resolve()
if not source.exists():
    raise SystemExit("Source database does not exist")
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(fd)
try:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
except Exception:
    destination.unlink(missing_ok=True)
    raise
print("Backup complete. Keep this file private; it contains workspace data and sessions.")
