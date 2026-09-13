"""Actual daemon, scripted planning responses, real artifact storage. No network."""

import json
import sys
import time
from pathlib import Path

from agent4good.config import Settings
from agent4good.credentials import TASK_CONTEXT
from agent4good.db import Database
from agent4good.worker import main


class Scripted:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database.from_settings(settings)

    def respond(self, instructions, items, tools):
        title = self.db.task(TASK_CONTEXT.get())["title"]
        calls = [i for i in items if i.get("type") == "function_call"]
        if calls:
            return {"output": [], "output_text": "Saved the evidence", "usage": {}}
        if title.startswith("Plan:"):
            steps = [
                {
                    "key": key,
                    "title": key,
                    "prompt": "Save checked fact",
                    "agent": "research_analyst",
                    "tools": ["artifact_write"],
                    "depends_on": deps,
                }
                for key, deps in [("source", []), ("report", ["source"])]
            ]
            name, args = (
                "mission_plan",
                {
                    "request": json.dumps(
                        {"expected_revision": 0, "reason": "Evidence before report", "steps": steps}
                    )
                },
            )
        else:
            if title == "report":
                (self.settings.data_dir / "report-entered").touch()
                deadline = time.monotonic() + 30
                while not (self.settings.data_dir / "release").exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("Acceptance latch expired")
                    time.sleep(0.03)
            name, args = "artifact_write", {"name": title + ".md", "content": "checked fact"}
        return {
            "output": [
                {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": title}
            ],
            "output_text": "",
            "usage": {},
        }


if __name__ == "__main__":
    settings = Settings(
        _env_file=None,
        data_dir=Path(sys.argv[1]),
        admin_password="test-only-owner-password",
        session_secret="test-only-session-secret-12345678901234567890",
        secure_cookies=False,
        worker_poll_seconds=0.2,
        max_concurrent_runs=2,
    )
    main(settings, Scripted(settings))
