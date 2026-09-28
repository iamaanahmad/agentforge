"""Minimal, private event capture for an owner's installation."""

import json
import logging
import hmac
import hashlib
import urllib.request

log = logging.getLogger(__name__)


def capture(settings, event, properties):
    if not settings.posthog_public_key or event not in {"owner_pageview", "first_useful_result"}:
        return False
    # Fixed properties only. No URLs, titles, prompts, mission IDs, or credentials leave the host.
    identity = hmac.new(
        settings.session_secret.encode(),
        (settings.tenant_id + ":" + settings.environment).encode(),
        hashlib.sha256,
    ).hexdigest()
    payload = {
        "api_key": settings.posthog_public_key,
        "event": event,
        "distinct_id": identity,
        "properties": {**properties, "test_run": settings.environment != "production"},
    }
    if event == "first_useful_result":
        payload["properties"]["$insert_id"] = identity + ":first_useful_result"
    request = urllib.request.Request(
        "https://us.i.posthog.com/capture/",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status != 200:
                log.warning("Analytics capture returned HTTP %s", response.status)
                return False
            return True
    except (OSError, TimeoutError):
        log.warning("Analytics capture failed")
        return False


def capture_first_result(db, settings):
    if not capture(settings, "first_useful_result", {}):
        return
    from .db import now

    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM events WHERE kind='analytics_first_result' LIMIT 1").fetchone():
            conn.execute(
                "INSERT INTO events(task_id,kind,message,created_at) VALUES (NULL,'analytics_first_result','First accepted mission viewed',?)",
                (now(),),
            )
