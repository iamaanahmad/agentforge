"""Trusted, separate Docker controller. Generated commands never execute on this host."""

import base64
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import tarfile
import threading
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field


class SandboxError(RuntimeError):
    pass


def safe_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(x in {"", ".", "..", ".git"} for x in value.split("/")):
        raise SandboxError("Invalid workspace path")
    if "\\" in value or any(ord(c) < 32 for c in value) or len(value) > 200:
        raise SandboxError("Invalid workspace path")
    return value


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    files: dict[str, str] = Field(max_length=128)
    script: str = Field(min_length=1, max_length=20000)
    artifacts: list[str] = Field(max_length=8)

    def checked(self):
        if len(json.dumps(self.model_dump()).encode()) > 800000:
            raise SandboxError("Workspace input exceeds limit")
        for path in [*self.files, *self.artifacts]:
            safe_path(path)
        return self


class DockerSandbox:
    """One fresh container per call; no host mounts, networking, secrets, or privileged flags."""

    def __init__(self, image, seconds=120, runtime="runc"):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise SandboxError("Pin the sandbox image to a local sha256 image ID")
        if runtime not in {"runc", "runsc"} or not 1 <= seconds <= 300:
            raise SandboxError("Invalid sandbox runtime or deadline")
        self.image, self.seconds, self.runtime = image, seconds, runtime
        self.lock = threading.Lock()
        # Inherit no cloud/provider credentials, proxy settings, or Docker endpoint overrides.
        self.env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent"}

    def docker(self, args, *, data=None, timeout=15, limit=500000):
        # Temporary output lives in RAM-backed bounded pipes drained by communicate below.
        # All invocations except job execution have bounded server-generated output.
        result = subprocess.run(
            ["docker", *args], input=data, capture_output=True, timeout=timeout, env=self.env
        )
        if result.returncode or len(result.stdout) > limit:
            raise SandboxError("Container control failed; inspect the broker host")
        return result.stdout

    def preflight(self):
        info = json.loads(self.docker(["info", "--format", "{{json .}}"]))
        if not info.get("MemoryLimit") or not info.get("PidsLimit") or not info.get("CpuCfsQuota"):
            raise SandboxError("Container resource enforcement is unavailable")
        image = json.loads(self.docker(["image", "inspect", self.image]))[0]
        if image["Config"].get("Volumes"):
            raise SandboxError("Sandbox image must not declare volumes")

    def recover(self):
        # Called once before listening. An exclusive broker process is required per Docker daemon.
        for name in self.docker(["ps", "-aq", "--filter", "label=agent4good.sandbox=true"]).decode().split():
            self.docker(["rm", "-f", "-v", name])

    def run(self, job, cancelled=lambda: False):
        job.checked()
        if not self.lock.acquire(blocking=False):
            raise SandboxError("Sandbox capacity reached")
        name = "a4g-sandbox-" + job.id
        proc = None
        try:
            if cancelled():
                raise SandboxError("Sandbox cancelled")
            self.preflight()
            args = [
                "create",
                "--name",
                name,
                "--label",
                "agent4good.sandbox=true",
                "--runtime",
                self.runtime,
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
                "64",
                "--memory",
                "256m",
                "--memory-swap",
                "256m",
                "--cpus",
                "1",
                "--ulimit",
                "nofile=128:128",
                "--ulimit",
                "fsize=16777216:16777216",
                "--log-driver",
                "none",
                "--tmpfs",
                "/workspace:rw,nosuid,nodev,size=134217728,uid=10001,gid=10001,mode=0700",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16777216,uid=10001,gid=10001,mode=0700",
                "--workdir",
                "/workspace",
                "--entrypoint",
                "/bin/sleep",
                self.image,
                str(self.seconds + 15),
            ]
            self.docker(args)
            self.docker(["start", name])
            payload = json.dumps(job.model_dump()).encode()
            proc = subprocess.Popen(
                ["docker", "exec", "-i", name, "python3", "-I", "/opt/a4g/run.py"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.env,
            )
            proc.stdin.write(payload)
            proc.stdin.close()
            deadline = time.monotonic() + self.seconds
            while proc.poll() is None:
                if cancelled() or time.monotonic() >= deadline:
                    raise SandboxError("Sandbox cancelled or timed out")
                time.sleep(0.1)
            if proc.returncode:
                raise SandboxError("Sandbox runner failed or exceeded resources")
            # docker cp only returns a fixed small result, never extracts untrusted archives on the host.
            raw = self.docker(["cp", name + ":/workspace/result.json", "-"], limit=250000)
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                members = archive.getmembers()
                if len(members) != 1 or not members[0].isfile() or members[0].size > 200000:
                    raise SandboxError("Invalid sandbox result")
                result = json.load(archive.extractfile(members[0]))
            if set(result) != {"exit_code", "log", "artifacts"} or type(result["exit_code"]) is not int:
                raise SandboxError("Invalid sandbox result")
            if not isinstance(result["log"], str) or len(result["log"]) > 12000:
                raise SandboxError("Invalid sandbox log")
            if not isinstance(result["artifacts"], dict) or not set(result["artifacts"]) <= set(
                job.artifacts
            ):
                raise SandboxError("Invalid sandbox artifacts")
            for value in result["artifacts"].values():
                if not isinstance(value, str) or len(base64.b64decode(value, validate=True)) > 80000:
                    raise SandboxError("Invalid sandbox artifact")
            return {**result, "job_id": job.id, "input_sha256": hashlib.sha256(payload).hexdigest()}
        finally:
            try:
                # rm -f stops ALL processes in the namespace, including background grandchildren.
                self.docker(["rm", "-f", "-v", name])
            finally:
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)
                self.lock.release()


class SandboxClient:
    def __init__(self, socket):
        self.socket = str(socket)

    def run(self, job, active):
        transport = httpx.HTTPTransport(uds=self.socket)
        with httpx.Client(
            transport=transport, base_url="http://sandbox", timeout=10, trust_env=False
        ) as client:
            response = client.post("/jobs", json=job.model_dump())
            response.raise_for_status()
            try:
                deadline = time.monotonic() + 320
                while time.monotonic() < deadline:
                    if not active():
                        raise SandboxError("Task stopped during isolated execution")
                    response = client.get("/jobs/" + job.id)
                    response.raise_for_status()
                    body = response.json()
                    if body["status"] == "done":
                        return body["result"]
                    if body["status"] == "failed":
                        raise SandboxError("Isolated execution failed; inspect broker receipt")
                    time.sleep(0.25)
                raise SandboxError("Sandbox broker deadline exceeded")
            finally:
                client.delete("/jobs/" + job.id).raise_for_status()


def serve():
    """Run a separate broker with a mode-0600 Unix socket. Never put Docker in the model worker."""
    import argparse
    import fcntl
    from pathlib import Path
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request

    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime", choices=["runc", "runsc"], default="runc")
    args = parser.parse_args()
    os.umask(0o077)
    socket_path = Path(args.socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if socket_path.parent.stat().st_mode & 0o077:
        raise SandboxError("Socket directory must be owner-only")
    guard = open(socket_path.parent / "broker.lock", "w")
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    backend = DockerSandbox(args.image, runtime=args.runtime)
    backend.preflight()
    backend.recover()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    jobs, mutex = {}, threading.Lock()

    def run(job, cancel):
        try:
            result = backend.run(job, cancel.is_set)
            with mutex:
                jobs[job.id].update(status="done", result=result)
        except Exception:
            with mutex:
                jobs[job.id].update(status="failed")

    @app.post("/jobs", status_code=202)
    async def submit(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 800000:
                raise HTTPException(413, "Workspace too large")
        try:
            job = Job.model_validate_json(raw).checked()
        except Exception:
            raise HTTPException(400, "Invalid workspace request") from None
        with mutex:
            if job.id in jobs or len(jobs) >= 32 or any(j["status"] == "running" for j in jobs.values()):
                raise HTTPException(409, "Duplicate job or capacity reached")
            cancel = threading.Event()
            thread = threading.Thread(target=run, args=(job, cancel), daemon=True)
            jobs[job.id] = {"status": "running", "cancel": cancel, "thread": thread}
            thread.start()
        return {"id": job.id}

    @app.get("/jobs/{job_id}")
    def status(job_id: str):
        with mutex:
            if job_id not in jobs:
                raise HTTPException(404)
            return {k: v for k, v in jobs[job_id].items() if k in {"status", "result"}}

    @app.delete("/jobs/{job_id}")
    def cancel(job_id: str):
        with mutex:
            job = jobs.get(job_id)
            if not job:
                return {"removed": True}
            job["cancel"].set()
        job["thread"].join(timeout=30)
        if job["thread"].is_alive():
            raise HTTPException(503, "Cleanup is still running")
        with mutex:
            jobs.pop(job_id, None)
        return {"removed": True}

    # SIGKILL recovery removes old labelled containers before accepting new work.
    uvicorn.run(app, uds=str(socket_path), log_level="warning")


if __name__ == "__main__":
    serve()
