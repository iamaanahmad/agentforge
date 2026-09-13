"""Real installed CLI/SDK/server/worker acceptance in disposable directories.

Run after installing a wheel: python scripts/developer_acceptance.py --python /path/to/venv/bin/python
Only the standard library runs this harness. It never imports the source checkout.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    # Resolve parent but preserve venv interpreter symlinks.
    executable = str(Path(args.python).absolute())
    with tempfile.TemporaryDirectory(prefix="a4g-developer-") as directory:
        root = Path(directory)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1"}

        def command(*words, ok=True, alternate=None):
            result = subprocess.run(
                [executable, "-m", "agent4good.cli", *words],
                cwd=root,
                env=alternate or env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert (result.returncode == 0) == ok, (words, result.stdout, result.stderr)
            return json.loads(result.stdout) if ok else result

        command("init")
        command("init", ok=False)
        assert command("config-check")["valid"]
        command("migrate")
        config = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines() if "=" in line)
        env.update(config)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://localhost:{port}"
        env["A4G_PUBLIC_ORIGIN"] = url
        env["A4G_URL"] = url
        processes = []

        def start(*words):
            process = subprocess.Popen(
                [executable, "-m", "agent4good.cli", *words],
                cwd=root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            processes.append(process)

        def stop():
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
            for process in processes:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            processes.clear()

        try:
            start("serve", "--port", str(port))
            start("worker")
            for _ in range(100):
                try:
                    with urllib.request.urlopen(url + "/healthz", timeout=1) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Server did not start")
            for _ in range(30):
                if command("readiness")["worker"]["online"]:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("Worker did not become ready")
            command("health", ok=False)  # No provider key: worker liveness is not execution readiness.
            command("migrate", "--backup", str(root / "unsafe.sqlite3"), ok=False)
            from datetime import datetime, timedelta, timezone

            spec = {
                "title": "[TEST] Installed CLI",
                "objective": "Inspect developer tools",
                "criteria": [{"id": "owner", "description": "Owner checks result", "kind": "owner"}],
                "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            }
            path = root / "mission.json"
            path.write_text(json.dumps(spec))
            mission = command("mission", "create", str(path))
            assert command("mission", "inspect", mission["id"])["status"] == "draft"
            assert command("mission", "cancel", mission["id"])["status"] == "cancelled"
            assert command("approvals", "list") == []
            assert "/api/v1/missions" in command("schema")["paths"]
            sdk_code = """
import json, os
from agent4good.sdk import Client
with Client(os.environ['A4G_URL']) as client:
    client.login(os.environ['A4G_ADMIN_PASSWORD'])
    mission = client.create_mission(json.load(open('mission.json')))
    assert client.mission(mission['id'])['status'] == 'draft'
    assert client.cancel_mission(mission['id'])['status'] == 'cancelled'
    task = client.request('POST', '/tasks', {'title':'[TEST] replay', 'prompt':'No external writes'})
    replay = client.replay(task['id'])
    assert not replay['external_effects'] and replay['receipts'] == []
    assert client.debug(task['id'])['task']['status'] == 'draft'
    assert client.request('POST', '/tasks/' + task['id'] + '/cancel', {})['status'] == 'cancelled'
    print(json.dumps({'task_id': task['id']}))
"""
            result = subprocess.run(
                [executable, "-c", sdk_code], cwd=root, env=env, capture_output=True, text=True, timeout=30
            )
            assert result.returncode == 0, result.stderr
            task_id = json.loads(result.stdout)["task_id"]
            assert command("replay", task_id)["task"]["status"] == "cancelled"
            assert command("debug", task_id)["external_effects"] is False
            assert command("events")["next_cursor"] > 0
            backup = root / "backup.sqlite3"
            command("backup", str(backup))
            stop()
            command("migrate", "--backup", str(root / "pre-upgrade.sqlite3"))
            restored = {**env, "A4G_DATA_DIR": str(root / "restored")}
            command("restore", str(backup), alternate=restored)
            command("restore", str(backup), alternate=restored, ok=False)
            env.update(restored)
            start("serve", "--port", str(port))
            for _ in range(100):
                try:
                    with urllib.request.urlopen(url + "/healthz", timeout=1):
                        break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.1)
            assert command("mission", "inspect", mission["id"])["status"] == "cancelled"
            assert command("replay", task_id)["task"]["status"] == "cancelled"
            assert not command("readiness")["worker"]["online"]
            print(
                json.dumps(
                    {
                        "passed": True,
                        "interfaces": ["installed CLI", "installed SDK", "HTTP API", "real worker"],
                        "checks": [
                            "private bootstrap",
                            "configuration",
                            "migration",
                            "mission lifecycle",
                            "schema",
                            "read-only replay",
                            "worker readiness",
                            "backup",
                            "restore",
                            "overwrite refusal",
                        ],
                        "live_model": False,
                        "cleanup": "All processes stopped and temporary data removed",
                    }
                )
            )
        finally:
            stop()


if __name__ == "__main__":
    main()
