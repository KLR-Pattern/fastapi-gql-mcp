"""Response/request models for the notes demo."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GitHubUser(BaseModel):
    """The logged-in identity (from GitHub)."""

    login: str = Field(description="GitHub username")
    name: str | None = Field(default=None, description="display name")
    avatar_url: str | None = Field(default=None, description="profile picture URL")


class TokenOut(BaseModel):
    """A Bearer token minted from the browser session (for MCP clients)."""

    access_token: str = Field(
        description="HMAC-signed session token — pass as Authorization: Bearer <token>"
    )
    token_type: str = Field(default="Bearer", description="how to present the token")


class NoteOut(BaseModel):
    """A note owned by the logged-in user."""

    id: int = Field(description="stable note identifier")
    title: str = Field(description="note headline")
    body: str = Field(description="note content")
    owner: str = Field(description="GitHub login of the owner")


class NoteCreate(BaseModel):
    """Payload for creating a note."""

    title: str = Field(description="note headline")
    body: str = Field(default="", description="note content")


class StatsOut(BaseModel):
    """Public aggregate counters."""

    notes: int = Field(description="notes stored across all users")
    users: int = Field(description="distinct note owners")


class NoteSummary(BaseModel):
    """Sparse note view for list widgets — deliberately optional-heavy."""

    id: int = Field(description="stable note identifier")
    title: str = Field(description="note headline")
    pinned: bool = Field(default=False, description="pinned to the top")
    color: str | None = Field(default=None, description="accent color, when themed")
