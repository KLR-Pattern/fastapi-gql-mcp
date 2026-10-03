"""Credential carriers — how a caller proves identity, resolved in one place.

The app accepts interchangeable carriers for the same signed identity: the
browser's session cookie, a session-string Bearer (``POST /auth/token``),
and Bearer tokens verified by pluggable verifiers — the MCP OAuth proxy
registers one at assembly time. Route dependencies only call
``resolve_user``; they stay ignorant of carriers and of any transport
(MCP or otherwise).

Registering order is evaluation order: the cheap session-string verifier
first, external verifiers (network-backed) after. With no external
verifier registered their token class is simply unverifiable — a clean 401.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.security.utils import get_authorization_scheme_param

from app.session import SESSION_COOKIE, decode_session

# async: token -> user payload, or None when this verifier doesn't
# recognize the token.
BearerVerifier = Callable[[str], Awaitable[dict[str, Any] | None]]
_bearer_verifiers: list[BearerVerifier] = []


def register_bearer_verifier(verifier: BearerVerifier) -> None:
    """Register a Bearer verifier (idempotent per function)."""
    if verifier not in _bearer_verifiers:
        _bearer_verifiers.append(verifier)


async def session_string_verifier(token: str) -> dict[str, Any] | None:
    """The app's own signed session string (what POST /auth/token hands out)."""
    return decode_session(token)


async def resolve_user(request: Request) -> dict[str, Any] | None:
    """The caller's identity from whichever carrier it presented, or None."""
    scheme, param = get_authorization_scheme_param(
        request.headers.get("authorization", "")
    )
    if scheme.lower() == "bearer":
        for verify in _bearer_verifiers:
            user = await verify(param)
            if user is not None:
                return user
        return None
    return decode_session(request.cookies.get(SESSION_COOKIE))
