"""Real loopback side effects and process death; model responses remain scripted."""

import json
import multiprocessing
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent4good.db import Database
from agent4good.engine import Engine
from test_engine import FakeProvider, answer, approve, call, make


@pytest.fixture
def receiver(tmp_path):
    ledger = tmp_path / "remote.sqlite3"
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE effects (identity TEXT, body TEXT)")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            # Deliberately no deduplication: every request leaves a durable side effect.
            with sqlite3.connect(ledger) as conn:
                conn.execute(
                    "INSERT INTO effects VALUES (?, ?)",
                    (self.headers["Idempotency-Key"], body.decode()),
                )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"id":"controlled-receipt"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/effects", ledger
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def crash_worker(settings, task, endpoint, boundary):
    db = Database(settings.data_dir / "engine.sqlite3")
    engine = Engine(db, settings, FakeProvider())
    # Keep real HTTP parsing/timeouts and adapter/approval/receipt paths.
    # Only redirect the fixed provider destination to the controlled loopback server.
    request = engine.registry._request

    def controlled_request(method, url, headers=None, payload=None):
        assert url == "https://api.resend.com/emails"
        if boundary == "before_request":
            os._exit(17)
        result = request(method, endpoint, headers, payload)
        if boundary == "after_acceptance":
            os._exit(17)
        return result

    engine.registry._request = controlled_request
    if boundary == "after_receipt":
        engine.journal.observe = lambda *a, **k: os._exit(17)
    engine.run(task)


@pytest.mark.parametrize("boundary", ["before_request", "after_acceptance", "after_receipt"])
def test_process_crash_compares_external_effect_and_durable_receipt(settings, receiver, boundary):
    endpoint, ledger = receiver
    settings.resend_api_key = "test-only-not-a-provider-key"
    settings.mail_from = "owner@example.com"
    args = {"to": "test@example.com", "subject": "Tagged test", "body": "controlled side effect"}
    db, engine, task = make(settings, FakeProvider(answer(calls=[call("send_email", args)])))
    engine.run(task)
    assert db.task(task)["status"] == "waiting_approval"
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    approve(db, task)
    assert engine.claim() == task

    process = multiprocessing.get_context("spawn").Process(
        target=crash_worker, args=(settings, task, endpoint, boundary)
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("Crash probe did not terminate")
    assert process.exitcode == 17
    receipt = db.one("SELECT * FROM tool_runs WHERE task_id=?", (task,))
    assert receipt["status"] == ("done" if boundary == "after_receipt" else "started")
    provider = FakeProvider(answer(calls=[call("send_email", args, "new-id")]), answer("Recovered"))
    restarted = Engine(db, settings, provider)

    def forbidden_request(*args, **kwargs):
        pytest.fail("Recovery must not repeat any external request")

    restarted.registry._request = forbidden_request
    restarted.recover()
    if boundary == "after_receipt":
        assert restarted.claim() == task
        restarted.run(task)
        assert db.task(task)["status"] == "done"
    else:
        assert restarted.claim() is None
        assert db.task(task)["status"] == "failed"
        assert provider.calls == []
    assert len(db.all("SELECT * FROM tool_runs WHERE task_id=?", (task,))) == 1
    with sqlite3.connect(ledger) as conn:
        effects = conn.execute("SELECT * FROM effects").fetchall()
    assert len(effects) == (0 if boundary == "before_request" else 1)
    if effects:
        assert len(effects[0][0]) == 64
        assert json.loads(effects[0][1])["text"] == args["body"]
