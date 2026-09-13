"""Real Chromium + controlled HTTP server; no model/provider claims or external customer writes."""

import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from agent4good.browser import (
    BrowserBackend,
    BrowserError,
    BrowserJob,
    DockerBrowser,
    Journey,
    Relay,
    SessionStore,
    body_digest,
    exchange,
)
from agent4good.tools import ToolError
from test_tools import runnable, permit


BASE = "https://fixture.example"


def job(steps, writes=None, scope="a" * 64, reset=False):
    return BrowserJob(
        id=hashlib.sha256(os.urandom(32)).hexdigest(),
        scope=scope,
        journey=Journey(steps=steps, writes=writes or [], reset_session=reset),
    )


def write(url, body, method="POST", content_type=""):
    return {"method": method, "url": url, "body_sha256": body_digest(body, content_type)}


@pytest.fixture
def store(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    return SessionStore(tmp_path / "sessions", key)


def test_encrypted_state_replay_and_scope_binding(store):
    j = job([{"action": "inspect"}])
    assert store.begin(j) == (None, {})
    with pytest.raises(BrowserError, match="Ambiguous"):
        store.begin(j)
    store.finish(j, {"cookie": "private-test-cookie"}, {"status": "ok"})
    assert store.begin(j) == ({"status": "ok"}, None)
    assert b"private-test-cookie" not in store.path.read_bytes()
    other = job([{"action": "inspect"}], scope="b" * 64)
    assert store.begin(other) == (None, {})
    assert store.begin(job([{"action": "inspect"}]))[1]["cookie"] == "private-test-cookie"
    assert store.begin(job([{"action": "inspect"}], reset=True)) == (None, {})
    with store.connect() as conn:
        encrypted = conn.execute("SELECT value FROM sessions").fetchone()[0]
    with pytest.raises(Exception):
        store.decrypt("b" * 64, encrypted)


@pytest.mark.parametrize(
    "url",
    [
        "http://fixture.example",
        "https://127.0.0.1",
        "https://fixture.example:444",
        "https://user:password@fixture.example",
        "file:///etc/passwd",
        "https://other.example",
    ],
)
def test_relay_denies_destinations(url):
    with pytest.raises(BrowserError):
        Relay(["fixture.example"], Journey(steps=[{"action": "inspect"}])).fetch(
            {"url": url, "method": "GET"}
        )


def test_write_permit_is_exact_and_consumed_before_io(store, monkeypatch):
    monkeypatch.setattr("agent4good.browser.public_addresses", lambda host: (_ for _ in ()).throw(OSError()))
    data = b"name=tagged-test"
    j = job([{"action": "inspect"}], [write(BASE + "/submit", data)])
    relay = Relay(["fixture.example"], j.journey, store, j.scope)
    with pytest.raises(BrowserError):
        relay.fetch(
            {"url": BASE + "/submit", "method": "POST", "body": base64.b64encode(b"changed").decode()}
        )
    request = {"url": BASE + "/submit", "method": "POST", "body": base64.b64encode(data).decode()}
    with pytest.raises(OSError):
        relay.fetch(request)
    # Even another journey ID cannot repeat the same uncertain request inside this task.
    with pytest.raises(BrowserError, match="already attempted"):
        Relay(["fixture.example"], j.journey, store, j.scope).fetch(request)


def test_registry_approval_artifact_and_owner_scope(settings, monkeypatch):
    settings.browser_socket = "/controlled.sock"
    r, task = runnable(settings)
    args = {"journey": json.dumps({"steps": [{"action": "inspect"}]})}
    jobs = []

    def run(self, j, active):
        jobs.append(j)
        assert active()
        return {
            "status": "ok",
            "observations": [],
            "network": [],
            "blocked": [],
            "dialogs": [],
            "untrusted_source": True,
            "artifacts": {"screenshot-0.png": base64.b64encode(b"proof").decode()},
        }

    monkeypatch.setattr("agent4good.browser.BrowserClient.run", run)
    with pytest.raises(ToolError):
        r.execute("browser_run", args, task_id=task, call_id="check")
    permit(r, task, "browser_run", args)
    result = r.execute("browser_run", args, task_id=task, call_id="check")
    assert result["artifacts"][0]["sha256"] == hashlib.sha256(b"proof").hexdigest()
    assert r.execute("browser_run", args, task_id=task, call_id="check") == result
    assert len(jobs) == 1
    assert (
        jobs[0].scope
        == hashlib.sha256(
            json.dumps([settings.tenant_id, settings.environment, "owner", task]).encode()
        ).hexdigest()
    )


PAGE = b"""<!doctype html><html><head><title>Agent4Good browser acceptance</title></head>
<body style="font:20px system-ui;max-width:850px;margin:60px auto;color:#133528;background:#f8faf7">
<p>AGENT4GOOD / CONTROLLED TEST</p><h1>Browser journey</h1>
<p>Use a marked test identity to save a form and transfer a file.</p>
<form action="/submit" method="post"><label>Name <input name="name" id="name"></label>
<button id="submit">Save form</button></form><p><input id="upload" type="file"></p>
<form action="/upload" method="post" enctype="multipart/form-data"><input id="network-file" name="file" type="file"><button id="upload-submit">Upload file</button></form>
<form action="/lost-response" method="post"><button id="lost-submit">Test lost response</button></form>
<p><a id="download" href="/download">Download test receipt</a></p>
<p><a id="tab" target="_blank" href="/second">Open second tab</a></p>
<button id="dialog" onclick="alert('test dialog')">Open dialog</button>
<script>setTimeout(()=>{let e=document.createElement('p');e.id='late';e.textContent='Ready';document.body.append(e)},200);
const f=document.querySelector('#name');setTimeout(()=>{f.replaceWith(f.cloneNode(true))},100);
</script></body></html>"""


@pytest.fixture
def fixture_server(monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(("GET", self.path, self.headers.get("Cookie", "")))
            if self.path == "/private-redirect":
                self.send_response(302)
                self.send_header("Location", "https://127.0.0.1/secret")
                self.end_headers()
                return
            self.send_response(200)
            if self.path == "/download":
                self.send_header("Content-Disposition", 'attachment; filename="receipt.txt"')
                body = b"Tagged browser test receipt"
            elif self.path == "/second":
                body = b"<h1>Second tab</h1><p>Marked test session resumed.</p>"
            elif self.path == "/subresource":
                body = b'<h1>Restricted image</h1><img src="https://127.0.0.1/secret">'
            else:
                body = PAGE
            self.send_header("Content-Type", "text/html" if self.path != "/download" else "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(("POST", self.path, data))
            if self.path == "/lost-response":
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "test_session=marked-owner; Secure; HttpOnly; Path=/")
            self.end_headers()
            self.wfile.write(
                b'<body style="font:20px system-ui;margin:60px;color:#133528;background:#f8faf7"><p>AGENT4GOOD / CONTROLLED TEST</p><h1>Form saved</h1><p>Tagged browser journey completed.</p><a id="download" href="/download">Download receipt</a></body>'
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Test-only transport injection. Production still requires public DNS + verified HTTPS at port 443.
    monkeypatch.setattr("agent4good.browser.public_addresses", lambda host: ["fixture-address"])
    monkeypatch.setattr(
        "agent4good.browser.PinnedHTTPSConnection",
        lambda host, address: http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3),
    )
    yield requests
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def renderer():
    image = os.getenv("A4G_TEST_BROWSER_IMAGE")
    if image:

        class VerifiedDockerBrowser(DockerBrowser):
            def docker(self, args, **kwargs):
                result = super().docker(args, **kwargs)
                if args[0] == "start":
                    inspected = json.loads(super().docker(["inspect", args[1]]))[0]
                    config = inspected["HostConfig"]
                    assert config["NetworkMode"] == "none"
                    assert config["ReadonlyRootfs"] and not config["Binds"]
                    assert config["Memory"] == 1073741824 and config["PidsLimit"] == 256
                    assert inspected["Config"]["User"] == "10001:10001"
                    assert all(m["Type"] != "bind" for m in inspected["Mounts"])
                    super().docker(
                        [
                            "exec",
                            args[1],
                            "python",
                            "-I",
                            "-c",
                            "import os,socket; assert not os.path.exists('/workspace'); "
                            "s=socket.socket(); s.settimeout(1); "
                            "assert s.connect_ex(('1.1.1.1',443)) != 0; "
                            "assert not any(k.endswith('_API_KEY') for k in os.environ)",
                        ]
                    )
                return result

        renderer = VerifiedDockerBrowser(image)
        yield renderer
        assert not renderer.docker(["ps", "-aq", "--filter", "label=agent4good.browser=true"]).strip()
    elif os.getenv("A4G_TEST_LOCAL_BROWSER"):
        # Trusted controlled fixture only. NOT a production isolation fallback.
        class LocalFixture:
            def run_browser(self, job, state, relay, cancelled=lambda: False):
                proc = subprocess.Popen(
                    [sys.executable, "-I", "browser/runner.py"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={
                        "PATH": os.environ["PATH"],
                        "HOME": os.environ["HOME"],
                        "PLAYWRIGHT_BROWSERS_PATH": os.getenv("PLAYWRIGHT_BROWSERS_PATH", "0"),
                    },
                )
                try:
                    return exchange(proc, job, state, relay, cancelled)
                finally:
                    proc.kill()
                    _, err = proc.communicate()
                    if err:
                        print(err.decode()[-2000:])

        yield LocalFixture()
    else:
        pytest.skip("Real browser acceptance requires the isolated CI image")


def test_real_journey_upload_download_tabs_and_session_resume(renderer, store, fixture_server):
    backend = BrowserBackend(store, renderer, ["fixture.example"])
    body = b"name=tagged-browser-test"
    steps = [
        {"action": "navigate", "target": BASE},
        {"action": "wait", "target": "#late"},
        {"action": "fill", "target": "#name", "value": "tagged-browser-test"},
        {
            "action": "upload",
            "target": "#upload",
            "value": json.dumps(
                {
                    "name": "test.txt",
                    "mimeType": "text/plain",
                    "base64": base64.b64encode(b"marked file").decode(),
                }
            ),
        },
        {"action": "click", "target": "#tab"},
        {"action": "switch_tab", "target": "1"},
        {"action": "inspect"},
        {"action": "close_tab"},
        {"action": "click", "target": "#dialog"},
        {"action": "click", "target": "#submit"},
        {"action": "inspect"},
        {"action": "download", "target": "#download"},
        {"action": "screenshot"},
    ]
    j = job(steps, [write(BASE + "/submit", body)])
    result = backend.run(j)
    assert result["status"] == "ok", result
    assert "Form saved" in result["observations"][10]["text"]
    assert len(result["observations"]) == 13
    assert result["dialogs"] == [{"type": "alert", "action": "dismissed"}]
    assert base64.b64decode(result["artifacts"]["download-11.bin"]) == b"Tagged browser test receipt"
    Path("/tmp/a4g-browser-proof.png").write_bytes(base64.b64decode(result["artifacts"]["screenshot-12.png"]))
    assert backend.run(j) == result
    assert len([r for r in fixture_server if r[0] == "POST"]) == 1
    resumed = BrowserBackend(
        SessionStore(store.directory, store.directory.parent / "key"), renderer, ["fixture.example"]
    )
    assert resumed.run(job([{"action": "navigate", "target": BASE + "/second"}]))["status"] == "ok"
    assert fixture_server[-1][2] == "test_session=marked-owner"
    assert (
        resumed.run(job([{"action": "navigate", "target": BASE + "/second"}], scope="b" * 64))["status"]
        == "ok"
    )
    assert fixture_server[-1][2] == ""


@pytest.mark.parametrize("path", ["/private-redirect", "/subresource"])
def test_real_redirect_and_subresource_blocked(renderer, store, fixture_server, path):
    result = BrowserBackend(store, renderer, ["fixture.example"]).run(
        job([{"action": "navigate", "target": BASE + path}, {"action": "wait", "target": "img"}])
    )
    assert result["blocked"]
    assert result["status"] != "ok"
    assert not any(r[1] == "/secret" for r in fixture_server)


def test_real_timeout_and_unapproved_form_do_not_repeat(renderer, store, fixture_server):
    backend = BrowserBackend(store, renderer, ["fixture.example"])
    result = backend.run(
        job([{"action": "navigate", "target": BASE}, {"action": "wait", "target": "#missing"}])
    )
    assert result["status"] == "failed"
    assert result["observations"][-1]["attempts"] == 2
    j = job([{"action": "navigate", "target": BASE}, {"action": "click", "target": "#submit"}])
    result = backend.run(j)
    assert result["status"] != "ok"
    assert result["blocked"]
    assert not any(r[0] == "POST" for r in fixture_server)


def test_real_cancellation_keeps_ambiguous_receipt(renderer, store, fixture_server):
    backend = BrowserBackend(store, renderer, ["fixture.example"])
    j = job([{"action": "navigate", "target": BASE}])
    with pytest.raises(BrowserError, match="cancelled"):
        backend.run(j, lambda: True)
    with pytest.raises(BrowserError, match="Ambiguous"):
        backend.run(j)


def test_real_runtime_approval_to_receipt_through_private_socket(
    renderer, store, fixture_server, settings, tmp_path
):
    import multiprocessing
    import time
    from agent4good.sandbox import serve_backend
    from test_engine import FakeProvider, answer, call, make, approve

    socket_dir = tmp_path / "socket"
    socket_dir.mkdir(mode=0o700)
    socket_path = socket_dir / "browser.sock"
    settings.browser_socket = str(socket_path)
    backend = BrowserBackend(store, renderer, ["fixture.example"])
    process = multiprocessing.get_context("fork").Process(
        target=serve_backend, args=(str(socket_path), backend, BrowserJob, lambda: None, 100000)
    )
    process.start()
    try:
        deadline = time.monotonic() + 15
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert socket_path.exists()
        assert socket_path.stat().st_mode & 0o077 == 0
        args = {
            "journey": json.dumps(
                {
                    "steps": [
                        {"action": "navigate", "target": BASE},
                        {"action": "wait", "target": "#late"},
                        {"action": "fill", "target": "#name", "value": "tagged-runtime-test"},
                        {
                            "action": "upload",
                            "target": "#upload",
                            "value": json.dumps(
                                {
                                    "name": "runtime.txt",
                                    "mimeType": "text/plain",
                                    "base64": base64.b64encode(b"marked runtime file").decode(),
                                }
                            ),
                        },
                        {"action": "click", "target": "#tab"},
                        {"action": "switch_tab", "target": "1"},
                        {"action": "inspect"},
                        {"action": "close_tab"},
                        {"action": "click", "target": "#submit"},
                        {"action": "inspect"},
                        {"action": "download", "target": "#download"},
                        {"action": "screenshot"},
                    ],
                    "writes": [write(BASE + "/submit", b"name=tagged-runtime-test")],
                }
            )
        }
        provider = FakeProvider(
            answer(calls=[call("browser_run", args)]), answer("Captured the controlled browser page.")
        )
        db, engine, task = make(settings, provider)
        engine.run(task)
        assert db.task(task)["status"] == "waiting_approval"
        assert not fixture_server
        approve(db, task)
        assert engine.claim() == task
        engine.run(task)
        assert db.task(task)["status"] == "done", db.task(task)
        receipt = db.all("SELECT * FROM tool_runs")[0]
        result = json.loads(receipt["result"])
        assert receipt["status"] == "done" and result["status"] == "ok"
        assert "Form saved" in result["observations"][9]["text"]
        assert any(a["name"].endswith(".png.b64") for a in db.all("SELECT * FROM artifacts"))
        assert db.all("SELECT * FROM events WHERE kind='tool_verified'")
    finally:
        process.terminate()
        process.join(timeout=30)
        if process.is_alive():
            process.kill()
            process.join()


def test_multipart_digest_binds_file_content_and_ignores_boundary():
    def body(boundary, data):
        return (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="proof.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n{data}\r\n--{boundary}--\r\n"
        ).encode()

    assert body_digest(body("one", "proof"), "multipart/form-data; boundary=one") == body_digest(
        body("two", "proof"), "multipart/form-data; boundary=two"
    )
    assert body_digest(body("one", "proof"), "multipart/form-data; boundary=one") != body_digest(
        body("one", "changed"), "multipart/form-data; boundary=one"
    )


def test_real_file_upload_and_uncertain_submission(renderer, store, fixture_server):
    backend = BrowserBackend(store, renderer, ["fixture.example"])
    multipart = (
        b'--known\r\nContent-Disposition: form-data; name="file"; filename="proof.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\nmarked file\r\n--known--\r\n"
    )
    result = backend.run(
        job(
            [
                {"action": "navigate", "target": BASE},
                {
                    "action": "upload",
                    "target": "#network-file",
                    "value": json.dumps(
                        {
                            "name": "proof.txt",
                            "mimeType": "text/plain",
                            "base64": base64.b64encode(b"marked file").decode(),
                        }
                    ),
                },
                {"action": "click", "target": "#upload-submit"},
                {"action": "inspect"},
            ],
            [write(BASE + "/upload", multipart, content_type="multipart/form-data; boundary=known")],
        )
    )
    assert result["status"] == "ok", result
    assert b"marked file" in next(r[2] for r in fixture_server if r[1] == "/upload")
    steps = [{"action": "navigate", "target": BASE}, {"action": "click", "target": "#lost-submit"}]
    permit = [write(BASE + "/lost-response", b"")]
    first = backend.run(job(steps, permit))
    assert first["status"] == "uncertain", first
    second = backend.run(job(steps, permit))
    assert second["status"] != "ok"
    assert len([r for r in fixture_server if r[0] == "POST" and r[1] == "/lost-response"]) == 1
