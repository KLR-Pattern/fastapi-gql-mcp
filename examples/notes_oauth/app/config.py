"""Environment-driven configuration (.env, see .env.example)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-do-not-use-in-prod")

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8020")
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"


def oauth_configured() -> bool:
    return bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET)


def mcp_oauth_configured() -> bool:
    """The MCP OAuth proxy reuses the same GitHub app as the browser login."""
    return oauth_configured()
