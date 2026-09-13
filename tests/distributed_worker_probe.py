"""Acceptance-only scripted model through the actual worker entry point.

No production setting enables this provider. The test must launch this file.
"""

import json
import os
import time

from agent4good.config import Settings
from agent4good.worker import main


class ScriptedProvider:
    def respond(self, instructions, items, tools):
        if any(item.get("type") == "function_call_output" for item in items):
            time.sleep(float(os.getenv("A4G_TEST_FINAL_DELAY", "0")))
            return {"output": [], "output_text": "Scripted model; real stored artifact verified", "usage": {}}
        return {
            "output": [
                {
                    "type": "function_call",
                    "name": "artifact_write",
                    "call_id": "proof",
                    "arguments": json.dumps(
                        {"name": "proof.txt", "content": "Real worker and object store acceptance"}
                    ),
                }
            ],
            "output_text": "",
            "usage": {},
        }


if __name__ == "__main__":
    # Constructor injection avoids a production runtime switch for simulated providers.
    settings = Settings(**json.loads(os.environ["A4G_TEST_SETTINGS"]))
    main(settings=settings, provider=ScriptedProvider())
