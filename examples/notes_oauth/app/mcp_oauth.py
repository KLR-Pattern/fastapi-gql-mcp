"""MCP OAuth login: the fastmcp GitHub proxy and how its tokens verify.

An MCP client (Claude Code) connects to ``/mcp`` with no credentials, gets
401 + OAuth discovery metadata, authorizes on the proxy's consent page with
GitHub, and thereafter sends the proxy's HS256 JWT as a Bearer token.

That JWT is a REFERENCE token — the GitHub identity lives server-side in
the proxy's JTI -> upstream-token store, not in the token itself. So
``proxy_token_user`` verifies it by delegating to the provider in-process
(``verify_token`` = signature + store lookup + GitHub API check) and reads
the identity from the resulting access info.

Three-way layout contract (all pieces must agree):
  1. GitHub app's registered callback:  ``http://<BASE_URL>/auth/callback``
  2. ``REDIRECT_PATH`` below — a subdirectory of it, which GitHub allows
  3. ``auth_at_root=True`` in main.py — OAuth routes must live at the host
     root for that callback to be reachable

The SAME GitHub OAuth App serves both flows: the browser login uses its
registered callback, the proxy uses the subdirectory — no second app.
"""

from __future__ import annotations

from typing import Any

from app.config import (
    BASE_URL,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    mcp_oauth_configured,
)

# Subdirectory of the GitHub app's registered /auth/callback (see module
# docstring) — the browser login and the MCP proxy share the app.
REDIRECT_PATH = "/auth/callback/mcp"

_provider: Any | None = None
_provider_built = False


def provider() -> Any | None:
    """The single OAuth proxy instance, or None when GitHub OAuth isn't
    configured (then /mcp is open and Bearer verifiers aren't registered).

    LOAD-BEARING SINGLETON: the proxy's token stores are per-instance
    state. The MCP endpoint gate and ``proxy_token_user`` must share this
    exact instance, or verification silently 401s. Always go through here.
    """
    global _provider, _provider_built
    if not _provider_built:
        if mcp_oauth_configured():
            from fastmcp.server.auth.providers.github import GitHubProvider

            _provider = GitHubProvider(
                client_id=GITHUB_CLIENT_ID,  # same app as the browser login
                client_secret=GITHUB_CLIENT_SECRET,
                base_url=BASE_URL,  # OAuth endpoints at the app root (auth_at_root)
                redirect_path=REDIRECT_PATH,
                cache_ttl_seconds=300,  # cache GitHub API verification per token
            )
        _provider_built = True
    return _provider


async def proxy_token_user(token: str) -> dict[str, Any] | None:
    """The GitHub identity behind a proxy-issued Bearer token, or None.

    Delegates to the provider (full check: signature, store mapping,
    upstream token validity at the GitHub API) — the same trust boundary
    the MCP endpoint itself uses. Registered as a bearer verifier at app
    assembly (see main.py), so route auth never imports this module.
    """
    auth = provider()
    if auth is None:
        return None
    access = await auth.verify_token(token)
    claims = (getattr(access, "claims", None) or {}) if access else {}
    if not claims.get("login"):
        return None
    return {
        "login": claims["login"],
        "name": claims.get("name"),
        "avatar_url": claims.get("avatar_url"),
    }
