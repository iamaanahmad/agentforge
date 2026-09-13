"""Disposable real daemon with scripted models, used only by acceptance tests."""

import sys
import time
from pathlib import Path

from agent4good.config import Settings
from agent4good.credentials import TASK_CONTEXT
from agent4good.db import Database
from agent4good.worker import main
from test_quality import Provider

root = Path(sys.argv[1])
settings = Settings(
    _env_file=None,
    data_dir=root,
    admin_password="test-owner-password-only",
    session_secret="test-session-secret-not-for-deployment-1234",
    secure_cookies=False,
    public_origin="http://localhost:8000",
    max_daily_runs=100,
    worker_poll_seconds=0.2,
)
db = Database.from_settings(settings)


class PausingProvider(Provider):
    def respond(self, *args):
        if self.db.one("SELECT 1 FROM quality_reviews WHERE task_id=?", (TASK_CONTEXT.get(),)):
            (root / "review.entered").touch()
            deadline = time.monotonic() + 20
            while not (root / "release").exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("Test gate timed out")
                time.sleep(0.02)
        return super().respond(*args)


main(settings, PausingProvider(db, defective=False))
