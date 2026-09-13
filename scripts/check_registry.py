"""Bounded live HTTPS adapter acceptance. No model call, credential or external write."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent4good.config import Settings  # noqa: E402
from agent4good.db import Database  # noqa: E402
from agent4good.tools import ToolRegistry  # noqa: E402


def main():
    with TemporaryDirectory(prefix="a4g-registry-check-") as directory:
        settings = Settings(
            _env_file=None,
            data_dir=Path(directory),
            admin_password="tagged-temporary-test-owner",
            session_secret="tagged-test-session-" * 3,
            secure_cookies=False,
            public_origin="http://localhost:8000",
            openai_api_key="",
            github_token="",
            github_repo="",
            resend_api_key="",
            mail_from="",
            search_api_key="",
            allowed_read_hosts=["example.com"],
        )
        db = Database(Path(directory) / "check.sqlite3")
        task = db.create_task("Tagged registry acceptance", "Read a public example page", "strategist", True)
        db.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
        registry = ToolRegistry(settings, db)
        args = {"url": "https://example.com/"}
        result = registry.execute("web_fetch", args, task_id=task, call_id="live-read")
        assert "Example Domain" in result["text"]
        assert registry.execute("web_fetch", args, task_id=task, call_id="live-read") == result
        assert len(db.all("SELECT * FROM tool_runs WHERE status='done'")) == 1
        print(
            json.dumps(
                {
                    "tool": "web_fetch",
                    "source": args["url"],
                    "validated": True,
                    "receipt_reused": True,
                    "text_characters": len(result["text"]),
                    "cleanup": "temporary database and test records removed on exit",
                }
            )
        )


if __name__ == "__main__":
    main()
