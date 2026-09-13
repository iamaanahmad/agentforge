"""Runs INSIDE the isolated container; all output is untrusted task evidence."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

job = json.load(sys.stdin)
seed = Path("/workspace/seed")
seed.mkdir()
for name, content in job["files"].items():
    path = seed / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
env = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/workspace",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "Agent4Good",
    "GIT_AUTHOR_EMAIL": "test@localhost",
    "GIT_COMMITTER_NAME": "Agent4Good",
    "GIT_COMMITTER_EMAIL": "test@localhost",
    "PIP_NO_INDEX": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}


def git(*args, cwd=seed):
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


git("init")
git("add", ".")
git("commit", "--allow-empty", "-m", "Controlled source snapshot")
git("clone", "--no-hardlinks", str(seed), "/workspace/repo")
repo = Path("/workspace/repo")
git("switch", "-c", "agent4good/work", cwd=repo)
# The log has a file-size rlimit inherited from Docker; noisy commands cannot fill the host disk.
with open("/workspace/output.log", "wb") as log:
    result = subprocess.run(
        ["/bin/sh", "-eu", "-c", job["script"]], cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT
    )
artifacts = {}
total = 0
for name in job["artifacts"]:
    path = repo / name
    if not path.exists():
        continue
    if not path.resolve().is_relative_to(repo) or not path.is_file() or path.is_symlink():
        raise ValueError("Artifact path escapes workspace")
    # Race-safe against background writers: open relative to repo, reject symlinks at each component.
    fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = name.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        import stat

        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            raise ValueError("Artifact is not a regular file")
        with os.fdopen(file_fd, "rb") as stream:
            data = stream.read(80001)
        total += len(data)
        if total > 80000:
            raise ValueError("Artifact quota exceeded")
        artifacts[name] = base64.b64encode(data).decode()
    finally:
        os.close(fd)
with open("/workspace/output.log", "rb") as log:
    text = log.read(12000).decode(errors="replace")
Path("/workspace/result.json").write_text(
    json.dumps({"exit_code": result.returncode, "log": text, "artifacts": artifacts})
)
