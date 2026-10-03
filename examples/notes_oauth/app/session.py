"""Tiny HMAC-signed session cookie — no extra dependencies.

Cookie format: ``base64url(json_payload).hex_hmac``. The payload carries the
GitHub identity plus an expiry. Not a hardened auth library; just enough for
the demo while keeping the flow real (cookie in -> protected route out).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.config import SESSION_SECRET

SESSION_COOKIE = "demo_session"
_TTL_SECONDS = 24 * 3600


def _sign(data: bytes) -> str:
    return hmac.new(SESSION_SECRET.encode(), data, hashlib.sha256).hexdigest()


def encode_session(payload: dict[str, Any], ttl: int = _TTL_SECONDS) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl
    raw = json.dumps(body, separators=(",", ":")).encode()
    return f"{base64.urlsafe_b64encode(raw).decode()}.{_sign(raw)}"


def decode_session(cookie: str | None) -> dict[str, Any] | None:
    if not cookie or "." not in cookie:
        return None
    encoded, signature = cookie.rsplit(".", 1)
    try:
        raw = base64.urlsafe_b64decode(encoded.encode())
    except Exception:
        return None
    if not hmac.compare_digest(_sign(raw), signature):
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < time.time():
        return None
    return payload
