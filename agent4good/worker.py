"""One durable worker per SQLite volume. Never run multiple replicas against this volume."""

import fcntl
import logging
import signal
import threading
import time

from .config import Settings
from .db import Database, now
from .engine import Engine


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock = (settings.data_dir / "worker.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another worker owns this data directory") from None
    db = Database(settings.data_dir / "agent4good.sqlite3")
    engine = Engine(db, settings)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    def heartbeat():
        while not stop.is_set():
            db.execute(
                "INSERT INTO settings VALUES ('worker_last_seen',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now(),),
            )
            stop.wait(5)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    engine.recover()
    logging.info("Worker started")
    try:
        while not stop.is_set():
            engine.schedule_due()
            task_id = engine.claim()
            if task_id:
                engine.run(task_id)
            else:
                stop.wait(settings.worker_poll_seconds)
            # Retention for expired security records, not user artifacts.
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM rate_limits WHERE resets<?", (time.time(),))
    finally:
        stop.set()
        thread.join(timeout=6)
        db.execute("DELETE FROM settings WHERE key='worker_last_seen'")
        lock.close()


if __name__ == "__main__":
    main()
