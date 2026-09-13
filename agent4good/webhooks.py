"""A fixed, versioned signature contract; credentials and scope are server-owned."""

import hashlib
import hmac
import re
import time

from fastapi import HTTPException


def authenticate_webhook(broker, headers, raw):
    if len(raw) > 32000:
        raise HTTPException(413, "Webhook exceeds 32 KB")
    if not broker.configured("webhook_secret"):
        raise HTTPException(404, "Webhook is not configured")
    timestamp = headers.get("x-a4g-timestamp", "")
    delivery = headers.get("x-a4g-delivery", "")
    signature = headers.get("x-a4g-signature", "")
    if not re.fullmatch(r"[0-9]{10}", timestamp) or abs(time.time() - int(timestamp)) > 300:
        raise HTTPException(401, "Webhook timestamp expired or invalid")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", delivery):
        raise HTTPException(401, "Invalid webhook delivery identifier")
    if not re.fullmatch(r"v1=[a-f0-9]{64}", signature):
        raise HTTPException(401, "Invalid webhook signature")
    # Bind method, route, tenant and environment, not just caller-controlled bytes.
    prefix = f"v1\nPOST\n/api/webhooks/tasks\n{broker.settings.tenant_id}\n{broker.settings.environment}\n{timestamp}\n{delivery}\n"
    key = broker.get("webhook_secret", "webhook")
    expected = "v1=" + hmac.new(key.encode(), prefix.encode() + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid webhook signature")
    return delivery
