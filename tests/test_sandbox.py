import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import threading
import time

import pytest

from agent4good.sandbox import DockerSandbox, Job, SandboxError, safe_path
from agent4good.tools import ToolError
from test_tools import permit, runnable


@pytest.mark.parametrize("path", ["/etc/passwd", "../a", "a/../b", ".git/config", "a//b", "a\\b", "x\x00"])
def test_paths_are_confined(path):
    with pytest.raises(SandboxError):
        safe_path(path)


def test_fixed_backend_and_payload_limits():
    with pytest.raises(SandboxError):
        DockerSandbox("python:latest")
    with pytest.raises(SandboxError):
        DockerSandbox("sha256:" + "a" * 64, runtime="unconfined")
    with pytest.raises(SandboxError):
        Job(id="a" * 64, files={"x": "a" * 800000}, script="true", artifacts=[]).checked()


def test_registry_approval_source_snapshot_artifact_and_replay(settings, monkeypatch):
    settings.github_token, settings.github_repo, settings.sandbox_socket = (
        "test-token",
        "owner/repo",
        "/test.sock",
    )
    r, task = runnable(settings)
    args = {"ref": "a" * 40, "script": "true", "artifacts": "proof.txt"}
    calls = []

    def github(method, endpoint, payload=None):
        if endpoint.startswith("/git/trees/"):
            return {"tree": [{"type": "blob", "mode": "100644", "path": "a.txt", "sha": "b" * 40, "size": 2}]}
        return {"content": base64.b64encode(b"hi").decode()}

    monkeypatch.setattr(r, "_github", github)

    def run(self, job, active):
        assert active() and job.files == {"a.txt": "hi"}
        calls.append(job.id)
        return {
            "exit_code": 0,
            "log": "passed",
            "input_sha256": "c" * 64,
            "artifacts": {"proof.txt": base64.b64encode(b"proof").decode()},
        }

    monkeypatch.setattr("agent4good.sandbox.SandboxClient.run", run)
    with pytest.raises(ToolError):
        r.execute("sandbox_run", args, task_id=task, call_id="check")
    permit(r, task, "sandbox_run", args)
    result = r.execute("sandbox_run", args, task_id=task, call_id="check")
    assert result["source_sha"] == args["ref"] and result["exit_code"] == 0
    assert result["artifacts"][0]["sha256"] == hashlib.sha256(b"proof").hexdigest()
    assert r.execute("sandbox_run", args, task_id=task, call_id="check") == result
    assert len(calls) == 1


def test_restricted_image_is_real_docker_archive(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("image_builder", Path("sandbox/image.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = Path.cwd()
    try:
        os.chdir(tmp_path)
        Path("hello").write_text("hello")
        Path("Dockerfile").write_text('FROM scratch\nCOPY hello /hello\nCMD ["/hello"]\n')
        module.build("Dockerfile", "image.tar")
        with tarfile.open("image.tar") as archive:
            manifest = json.load(archive.extractfile("manifest.json"))
            config = json.load(archive.extractfile(manifest[0]["Config"]))
            layer = archive.extractfile("layer.tar").read()
            assert config["rootfs"]["diff_ids"] == ["sha256:" + hashlib.sha256(layer).hexdigest()]
            with tarfile.open(fileobj=io.BytesIO(layer)) as members:
                assert members.extractfile("hello").read() == b"hello"
        for instruction in [
            "RUN id",
            "ADD https://example.com /x",
            "COPY /etc/passwd /x",
            "COPY hello /../x",
        ]:
            Path("Dockerfile").write_text("FROM scratch\n" + instruction)
            with pytest.raises(ValueError):
                module.build("Dockerfile", "rejected.tar")
    finally:
        os.chdir(original)


@pytest.fixture
def real_backend():
    image = os.getenv("A4G_TEST_SANDBOX_IMAGE")
    if not image:
        pytest.skip("Real Docker sandbox acceptance runs in dedicated CI VM")
    backend = DockerSandbox(image, seconds=30)
    backend.preflight()
    yield backend
    assert not backend.docker(["ps", "-aq", "--filter", "label=agent4good.sandbox=true"]).strip()


def job(script, files=None, artifacts=None):
    return Job(
        id=hashlib.sha256(os.urandom(32)).hexdigest(),
        files=files or {"README.md": "Controlled test"},
        script=script,
        artifacts=artifacts or [],
    )


def test_real_clone_edit_install_test_lint_build_and_container_image(real_backend, tmp_path):
    script = """
git status --short
python3 -m venv /workspace/venv
/workspace/venv/bin/pip install --no-index --find-links=/opt/wheels setuptools wheel
printf 'def add(a, b):\n    return a + b\n' > app.py
python3 -c 'from app import add; assert add(2, 3) == 5'
python3 -m py_compile app.py
printf 'int main(void) {return 0;}' > hello.c
gcc -nostdlib -static -Wl,-e,main -o hello hello.c
printf 'FROM scratch\nCOPY app.py /app.py\nCMD ["/app.py"]\n' > Dockerfile
python3 -I /opt/a4g/image.py Dockerfile image.tar
"""
    result = real_backend.run(job(script, artifacts=["app.py", "image.tar"]))
    assert result["exit_code"] == 0, result["log"]
    image = tmp_path / "image.tar"
    image.write_bytes(base64.b64decode(result["artifacts"]["image.tar"]))
    # Load only the controlled test image in this disposable CI VM, never arbitrary job artifacts in production.
    subprocess.run(["docker", "load", "-i", str(image)], check=True, capture_output=True)
    loaded = json.loads(
        subprocess.check_output(["docker", "image", "inspect", "agent4good-sandbox:artifact"])
    )[0]
    assert loaded["Config"]["Cmd"] == ["/app.py"]
    subprocess.run(["docker", "image", "rm", "agent4good-sandbox:artifact"], check=True, capture_output=True)


def test_real_host_secret_filesystem_network_and_privileges(real_backend):
    script = """
test "$(id -u)" = 10001
test ! -e /var/run/docker.sock
test ! -e /home/agent/.iteration/secrets.env
test ! -e /host
test -z "${GITHUB_TOKEN:-}"
! touch /etc/forbidden
! mount -t tmpfs tmpfs /mnt
! unshare -Ur true
python3 - <<'CHECK'
import socket
for host in ['1.1.1.1', '169.254.169.254', '172.17.0.1']:
    s=socket.socket(); s.settimeout(1)
    try:
        s.connect((host, 80))
    except OSError:
        pass
    else:
        raise AssertionError('Network escaped')
    finally:
        s.close()
status=open('/proc/self/status').read()
assert 'CapEff:\\t0000000000000000' in status
assert 'NoNewPrivs:\\t1' in status
CHECK
"""
    result = real_backend.run(job(script))
    assert result["exit_code"] == 0, result["log"]


def test_real_process_limit_and_child_cleanup(real_backend):
    script = """python3 - <<'CHECK'
import subprocess
children=[]
try:
    for i in range(100):
        children.append(subprocess.Popen(['sleep','100']))
except OSError:
    assert len(children)<64
else:
    raise AssertionError('Process limit absent')
CHECK
"""
    result = real_backend.run(job(script))
    assert result["exit_code"] == 0


def test_real_memory_and_storage_limits(real_backend):
    result = real_backend.run(job("python3 -c 'x=bytearray(600*1024*1024)'"))
    assert result["exit_code"] != 0
    result = real_backend.run(job("dd if=/dev/zero of=/workspace/full bs=1M count=200"))
    assert result["exit_code"] != 0


def test_real_cancellation_and_recovery(real_backend):
    event = threading.Event()
    timer = threading.Timer(3, event.set)
    timer.start()
    try:
        with pytest.raises(SandboxError, match="cancelled"):
            real_backend.run(job("sleep 100 & wait"), event.is_set)
    finally:
        timer.cancel()
    real_backend.docker(
        ["create", "--name", "a4g-abandoned", "--label", "agent4good.sandbox=true", real_backend.image, "100"]
    )
    real_backend.recover()


def test_real_symlink_artifact_denied(real_backend):
    with pytest.raises(SandboxError):
        real_backend.run(job("ln -s /etc/passwd proof.txt", artifacts=["proof.txt"]))


def test_real_timeout(real_backend):
    real_backend.seconds = 2
    started = time.monotonic()
    with pytest.raises(SandboxError, match="timed out"):
        real_backend.run(job("sleep 100"))
    assert time.monotonic() - started < 20
