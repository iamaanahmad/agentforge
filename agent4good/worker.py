"""One worker per SQLite volume, or independent leased PostgreSQL workers."""

import fcntl
import logging
import signal
import threading
import time

from .config import Settings
from .db import Database, now
from .engine import Engine


def main(settings=None, provider=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock = None
    if not settings.database_url:
        lock = (settings.data_dir / "worker.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another worker owns this data directory") from None
    db = Database.from_settings(settings)
    engine = Engine(db, settings, provider=provider)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    heartbeat_stop = threading.Event()

    def heartbeat():
        while not heartbeat_stop.is_set():
            try:
                if db.distributed:
                    db.heartbeat()
                else:
                    db.execute(
                        "INSERT INTO settings VALUES ('worker_last_seen',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (now(),),
                    )
            except Exception:
                logging.error("Worker heartbeat failed; leases will expire if storage stays unavailable")
            heartbeat_stop.wait(5)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    engine.recover()
    logging.info("Worker started")
    db.event(
        None, "worker_started", "Distributed worker online" if db.distributed else "SQLite worker online"
    )
    try:
        while not stop.is_set():
            if db.distributed:
                engine.recover()
            engine.schedule_due()
            if stop.is_set():
                break
            task_id = engine.claim()
            if task_id:
                try:
                    engine.run(task_id)
                except Exception:
                    logging.error("Worker execution interrupted; recovery will inspect saved receipts")
            else:
                stop.wait(settings.worker_poll_seconds)
            # Retention for expired security records, not user artifacts.
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM rate_limits WHERE resets<?", (time.time(),))
    finally:
        heartbeat_stop.set()
        thread.join(timeout=6)
        if db.distributed:
            db.execute("DELETE FROM workers WHERE id=?", (db.worker_id,))
        else:
            db.execute("DELETE FROM settings WHERE key='worker_last_seen'")
        if lock:
            lock.close()
        db.event(None, "worker_stopped", "Worker drained and stopped")
        logging.info("Worker drained and stopped")


if __name__ == "__main__":
    main()
