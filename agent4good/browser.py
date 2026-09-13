"""Trusted browser broker: offline renderer, pinned HTTPS relay, encrypted task sessions."""

import base64
import hashlib
import json
import os
import select
import stat
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from email.parser import BytesParser
from email.policy import default

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field

from .sandbox import DockerSandbox, SandboxError, SandboxClient
from .tools import PinnedHTTPSConnection, public_addresses


class BrowserError(SandboxError):
    pass


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal[
        "navigate",
        "click",
        "fill",
        "select",
        "check",
        "press",
        "scroll",
        "inspect",
        "screenshot",
        "upload",
        "download",
        "new_tab",
        "switch_tab",
        "close_tab",
        "wait",
    ]
    target: str = Field("", max_length=2000)
    value: str = Field("", max_length=60000)
    # Stable CSS locators are re-resolved by Playwright; no cached element handles or arbitrary JS.


class WritePermit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    url: str = Field(max_length=2000)
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Journey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    steps: list[Step] = Field(min_length=1, max_length=20)
    writes: list[WritePermit] = Field(default_factory=list, max_length=8)
    reset_session: bool = False


class BrowserJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope: str = Field(pattern=r"^[a-f0-9]{64}$")
    journey: Journey

    def checked(self):
        if len(self.model_dump_json()) > 95000:
            raise BrowserError("Browser input exceeds limit")
        return self


class SessionStore:
    """Key lives outside state; session and receipt ciphertext bind their exact scope and row."""

    def __init__(self, directory, key_file):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_file = Path(key_file)
        if key_file.resolve().is_relative_to(self.directory.resolve()):
            raise BrowserError("Browser key must be outside state")
        if self.directory.stat().st_mode & 0o077 or key_file.stat().st_mode & 0o077:
            raise BrowserError("Browser state and key require private permissions")
        if self.directory.is_symlink() or self.directory.stat().st_uid != os.getuid():
            raise BrowserError("Browser state must be an owned real directory")
        fd = os.open(key_file, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BrowserError("Browser key must be an owned private file")
            key = stream.read(33)
        if len(key) != 32:
            raise BrowserError("Browser key must contain 32 raw random bytes")
        self.cipher = AESGCM(key)
        self.path = self.directory / "browser.sqlite3"
        if self.path.is_symlink():
            raise BrowserError("Browser database symlink denied")
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS writes(scope TEXT, fingerprint TEXT, status TEXT, PRIMARY KEY(scope,fingerprint))"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS sessions(scope TEXT PRIMARY KEY, value BLOB NOT NULL)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, scope TEXT, digest TEXT, value BLOB)"
            )
        os.chmod(self.path, 0o600)

    def connect(self):
        return sqlite3.connect(self.path)

    def encrypt(self, scope, value):
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, json.dumps(value).encode(), scope.encode())

    def decrypt(self, scope, value):
        return json.loads(self.cipher.decrypt(value[:12], value[12:], scope.encode()))

    def begin(self, job):
        digest = hashlib.sha256(job.model_dump_json().encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT scope,digest,value FROM receipts WHERE id=?", (job.id,)).fetchone()
            if row:
                if row[:2] != (job.scope, digest) or row[2] is None:
                    raise BrowserError("Ambiguous or mismatched browser receipt; inspect before retrying")
                return self.decrypt(job.scope + job.id, row[2]), None
            conn.execute("INSERT INTO receipts VALUES (?,?,?,NULL)", (job.id, job.scope, digest))
            row = conn.execute("SELECT value FROM sessions WHERE scope=?", (job.scope,)).fetchone()
            state = self.decrypt(job.scope, row[0]) if row and not job.journey.reset_session else {}
            return None, state

    def finish(self, job, state, result):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?)", (job.scope, self.encrypt(job.scope, state))
            )
            conn.execute(
                "UPDATE receipts SET value=? WHERE id=?", (self.encrypt(job.scope + job.id, result), job.id)
            )

    def claim_write(self, scope, fingerprint):
        with self.connect() as conn:
            try:
                conn.execute("INSERT INTO writes VALUES (?,?,'started')", (scope, fingerprint))
            except sqlite3.IntegrityError:
                raise BrowserError(
                    "Submission already attempted in this task; inspect before repeating"
                ) from None

    def complete_write(self, scope, fingerprint):
        with self.connect() as conn:
            conn.execute(
                "UPDATE writes SET status='received' WHERE scope=? AND fingerprint=?", (scope, fingerprint)
            )


def body_digest(body, content_type=""):
    """Multipart boundaries vary per browser. Bind ordered part names, filenames, MIME, and bytes."""
    if content_type.lower().startswith("multipart/form-data"):
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        if not message.is_multipart():
            raise BrowserError("Invalid multipart upload")
        parts = []
        for part in message.iter_parts():
            if part.is_multipart() or len(parts) >= 32:
                raise BrowserError("Multipart upload exceeds limit")
            parts.append(
                [
                    part.get_param("name", header="content-disposition"),
                    part.get_filename(),
                    part.get_content_type(),
                    base64.b64encode(part.get_payload(decode=True) or b"").decode(),
                ]
            )
        body = json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(body).hexdigest()


class Relay:
    """The only egress path. Renderer has no network and cannot select connection addresses."""

    def __init__(self, hosts, journey, store=None, scope=""):
        self.store, self.scope = store, scope
        self.hosts = set(hosts)
        self.permits = [p.model_dump() for p in journey.writes]
        self.calls, self.bytes = 0, 0
        self.receipts = []
        self.uncertain = False

    def fetch(self, request):
        self.calls += 1
        if self.calls > 100:
            raise BrowserError("Browser request limit exceeded")
        url, method = request["url"], request["method"]
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or len(url) > 2000
            or "\\" in url
        ):
            raise BrowserError("Browser destination denied")
        body = base64.b64decode(request.get("body", ""), validate=True)
        if len(body) > 80000:
            raise BrowserError("Browser upload exceeds limit")
        digest = body_digest(body, request.get("headers", {}).get("content-type", ""))
        mutating = method not in {"GET", "HEAD"}
        if mutating:
            permit = {"method": method, "url": url, "body_sha256": digest}
            if permit not in self.permits:
                raise BrowserError("Browser write lacks an exact unused permit")
            self.permits.remove(permit)  # Consume BEFORE I/O; no automatic retry of an uncertain submission.
        elif body:
            raise BrowserError("Read request body denied")
        fingerprint = hashlib.sha256(json.dumps([method, url, digest]).encode()).hexdigest()
        if mutating and self.store:
            self.store.claim_write(self.scope, fingerprint)
        address = public_addresses(parsed.hostname)[0]
        connection = PinnedHTTPSConnection(parsed.hostname, address)
        connection.timeout = 5
        # No host environment, provider credentials, arbitrary proxy or authorization header forwarding.
        headers = {
            k.lower(): v
            for k, v in request.get("headers", {}).items()
            if k.lower() in {"accept", "content-type", "cookie", "origin", "referer"}
        }
        headers["Accept-Encoding"] = "identity"
        receipt = {"method": method, "url": url, "body_sha256": digest, "status": "started"}
        self.receipts.append(receipt)
        try:
            connection.request(
                method,
                (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                body or None,
                headers,
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                receipt["status"] = "redirect_held"
                raise BrowserError("Redirect held; navigate explicitly after inspection")
            content = response.read(1000001)
            self.bytes += len(content)
            if len(content) > 1000000 or self.bytes > 8000000:
                raise BrowserError("Browser response exceeds limit")
            receipt["status"] = response.status
            if mutating and self.store:
                self.store.complete_write(self.scope, fingerprint)
            # Redirects never reach Chromium; it could follow them without another route callback.
            pairs = [
                (k.lower(), v)
                for k, v in response.getheaders()
                if k.lower()
                not in {"connection", "transfer-encoding", "content-length", "content-encoding", "alt-svc"}
            ]
            return {"status": response.status, "headers": pairs, "body": base64.b64encode(content).decode()}
        except Exception:
            if mutating:
                self.uncertain = True
                receipt["status"] = "uncertain"
            raise BrowserError("Browser request failed; inspect submission receipt") from None
        finally:
            connection.close()


class DockerBrowser(DockerSandbox):
    def run_browser(self, job, state, relay, cancelled=lambda: False):
        self.preflight()
        name = "a4g-browser-" + job.id
        proc = None
        # No host mounts or network; Playwright's request interception speaks bounded JSON over pipes.
        self.docker(
            [
                "create",
                "--name",
                name,
                "--label",
                "agent4good.browser=true",
                "--init",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--user",
                "10001:10001",
                "--pids-limit",
                "256",
                "--memory",
                "1g",
                "--memory-swap",
                "1g",
                "--cpus",
                "1",
                "--shm-size",
                "128m",
                "--ulimit",
                "fsize=16777216:16777216",
                "--log-driver",
                "none",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=268435456,uid=10001,gid=10001,mode=0700",
                "--entrypoint",
                "/bin/sleep",
                self.image,
                "200",
            ]
        )
        try:
            self.docker(["start", name])
            proc = subprocess.Popen(
                ["docker", "exec", "-i", name, "python", "-I", "/opt/a4g/browser_runner.py"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.env,
            )
            return exchange(proc, job, state, relay, cancelled)
        finally:
            self.docker(["rm", "-f", "-v", name])
            if proc:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
                proc.stdin.close()
                proc.stdout.close()

    def recover(self):
        for name in self.docker(["ps", "-aq", "--filter", "label=agent4good.browser=true"]).decode().split():
            self.docker(["rm", "-f", "-v", name])


def exchange(proc, job, state, relay, cancelled):
    def send(value):
        proc.stdin.write(json.dumps(value).encode() + b"\n")
        proc.stdin.flush()

    send({"journey": job.journey.model_dump(), "state": state})
    deadline, buffer = time.monotonic() + 180, bytearray()
    while time.monotonic() < deadline:
        if cancelled():
            raise BrowserError("Browser cancelled; inspect receipt before resuming")
        if not select.select([proc.stdout], [], [], 0.2)[0]:
            continue
        data = os.read(proc.stdout.fileno(), 65536)
        if not data:
            raise BrowserError("Browser exited without a result")
        buffer.extend(data)
        if len(buffer) > 3000000:
            raise BrowserError("Browser output exceeds limit")
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            message = json.loads(line)
            if message["type"] == "request":
                try:
                    send(relay.fetch(message["request"]))
                except Exception:
                    send({"denied": True})
            elif message["type"] == "result":
                result = message["result"]
                result["network"] = relay.receipts
                if relay.uncertain:
                    result["status"] = "uncertain"
                return message["state"], result
            else:
                raise BrowserError("Invalid browser protocol")
    raise BrowserError("Browser deadline exceeded; inspect receipt")


class BrowserBackend:
    def __init__(self, store, renderer, hosts):
        self.store, self.renderer, self.hosts = store, renderer, hosts
        self.lock = threading.Lock()

    def run(self, job, cancelled=lambda: False):
        job.checked()
        if not self.lock.acquire(blocking=False):
            raise BrowserError("Browser capacity reached")
        try:
            result, state = self.store.begin(job)
            if result is not None:
                return result
            relay = Relay(self.hosts, job.journey, self.store, job.scope)
            state, result = self.renderer.run_browser(job, state, relay, cancelled)
            if len(json.dumps(state)) > 500000 or len(json.dumps(result)) > 2000000:
                raise BrowserError("Browser session or result exceeds limit")
            self.store.finish(job, state, result)
            return result
        finally:
            self.lock.release()


# Same private asynchronous job protocol and cancellation as the isolated coding client.


class BrowserClient(SandboxClient):
    """Private browser job transport."""


def serve():
    import argparse
    from .sandbox import serve_backend

    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--host", action="append", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    renderer = DockerBrowser(args.image)
    renderer.preflight()
    backend = BrowserBackend(SessionStore(args.state, args.key_file), renderer, args.host)
    serve_backend(args.socket, backend, BrowserJob, renderer.recover, input_limit=100000)


if __name__ == "__main__":
    serve()
