"""Real worker daemon acceptance. Scripted responses, no provider calls or spend."""

import json
import sys
import time
from pathlib import Path

from agent4good.config import Settings
from agent4good.credentials import TASK_CONTEXT
from agent4good.db import Database
from agent4good.worker import main


def call(name, args, cid):
    return {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": cid}


def reply(calls=None, text=""):
    return {"output": calls or [], "output_text": text, "usage": {}}


class Scripted:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database.from_settings(settings)

    def respond(self, instructions, items, tools):
        task = TASK_CONTEXT.get()
        title = self.db.task(task)["title"]
        calls = [i["name"] for i in items if i.get("type") == "function_call"]
        if title == "parent":
            if not calls:
                return reply(
                    [
                        call(
                            "worker_spawn",
                            {
                                "request": json.dumps(
                                    {
                                        "title": name,
                                        "prompt": "Tagged " + name,
                                        "agent": "research_analyst",
                                        "reason": "Concurrent evidence",
                                        "tools": ["worker_message", "worker_context"],
                                        "priority": 0,
                                    }
                                )
                            },
                            name,
                        )
                        for name in ["alpha", "beta"]
                    ]
                )
            if "worker_wait" not in calls:
                return reply([call("worker_wait", {}, "wait")])
            if "worker_results" not in calls:
                return reply([call("worker_results", {}, "results")])
            outputs = [json.loads(i["output"]) for i in items if i.get("type") == "function_call_output"]
            children = outputs[-1]["data"]["children"]
            assert len(children) == 2 and all(c["status"] == "done" for c in children)
            return reply(text="Collected alpha and beta: " + ", ".join(c["result"] for c in children))
        if not calls:
            return reply(
                [
                    call(
                        "worker_context",
                        {
                            "scope": "private",
                            "operation": "write",
                            "revision": "0",
                            "content": "private " + title,
                        },
                        "private",
                    )
                ]
            )
        # Both real threads must enter before either can complete. The test kills
        # the process here, then restarts it and releases this external test latch.
        (self.settings.data_dir / (title + ".entered")).write_text("entered")
        other = "beta" if title == "alpha" else "alpha"
        deadline = time.monotonic() + 20
        while (
            not (self.settings.data_dir / (other + ".entered")).exists()
            or not (self.settings.data_dir / "release").exists()
        ):
            if time.monotonic() > deadline:
                raise RuntimeError("Concurrent acceptance latch timed out")
            time.sleep(0.02)
        if "worker_message" not in calls:
            parent = self.db.one("SELECT parent_id FROM worker_nodes WHERE task_id=?", (task,))["parent_id"]
            return reply(
                [call("worker_message", {"recipient": parent, "content": title + " finished"}, "message")]
            )
        return reply(text=title + " evidence")


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
