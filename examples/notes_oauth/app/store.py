"""In-memory note store (single-process demo)."""

from __future__ import annotations

NOTES: dict[int, dict] = {
    1: {"id": 1, "title": "Try the OAuth flow",
        "body": "Login, then query notes via GraphiQL.", "owner": "demo"},
    2: {"id": 2, "title": "Ask an agent",
        "body": "Point an MCP client at /mcp and let it explore.", "owner": "demo"},
}
_next_id = 3


def create_note(owner: str, title: str, body: str) -> dict:
    global _next_id
    note = {"id": _next_id, "title": title, "body": body, "owner": owner}
    NOTES[_next_id] = note
    _next_id += 1
    return note


def owned_notes(owner: str) -> list[dict]:
    return [n for n in NOTES.values() if n["owner"] == owner]
