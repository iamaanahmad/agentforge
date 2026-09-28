"""Minimal, private event capture for an owner's installation."""

import json
import logging
import hmac
import hashlib
import urllib.request

log = logging.getLogger(__name__)


def capture(settings, event, properties):
    if not settings.posthog_public_key or event not in {"owner_pageview", "first_useful_result"}:
        return
    # Fixed properties only. No URLs, titles, prompts, mission IDs, or credentials leave the host.
    payload = {
        "api_key": settings.posthog_public_key,
        "event": event,
        "distinct_id": hmac.new(
            settings.session_secret.encode(),
            (settings.tenant_id + ":" + settings.environment).encode(),
            hashlib.sha256,
        ).hexdigest(),
        "properties": {**properties, "test_run": settings.environment != "production"},
    }
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
    except (OSError, TimeoutError):
        log.warning("Analytics capture failed")
