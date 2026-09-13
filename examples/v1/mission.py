"""Run from an installed environment. Saves, inspects and cancels a tagged draft."""

import json
import os
from datetime import datetime, timedelta, timezone

from agent4good.sdk import Client

spec = {
    "title": "[TEST] Developer quickstart",
    "objective": "Save a supplied fact as an inspectable artifact",
    "criteria": [{"id": "proof", "description": "Owner checks the saved fact", "kind": "owner"}],
    "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
}
with Client(os.environ.get("A4G_URL", "http://localhost:8000")) as client:
    client.login(os.environ["A4G_ADMIN_PASSWORD"])
    mission = client.create_mission(spec)
    try:
        print(json.dumps(client.mission(mission["id"]), indent=2))
        print(json.dumps(client.events(), indent=2))
    finally:
        client.cancel_mission(mission["id"])
