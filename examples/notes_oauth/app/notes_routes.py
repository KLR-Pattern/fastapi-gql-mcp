"""Protected notes API — the domain the agent queries via GraphQL/MCP."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query

from app import store
from app.auth_routes import require_user
from app.models import NoteCreate, NoteOut, NoteSummary, StatsOut

# No router-level tag: each route carries exactly one domain tag, so every
# field has a single address in the GraphQL schema (router-level tags would
# mirror routes into extra domains).
router = APIRouter(prefix="/api")


@router.get("/notes", response_model=list[NoteOut], tags=["notes:mine"])
async def list_notes(
    user: Annotated[dict, Depends(require_user)],
    q: Annotated[str | None, Query(description="substring over title/body")] = None,
) -> list[NoteOut]:
    """List YOUR notes (login required).

    ``q`` filters by substring over title and body.
    """
    notes = store.owned_notes(user["login"])
    if q:
        ql = q.lower()
        notes = [n for n in notes if ql in n["title"].lower() or ql in n["body"].lower()]
    return [NoteOut.model_validate(n) for n in notes]


@router.get("/notes/{note_id}", response_model=NoteOut, tags=["notes:mine"])
async def get_note(
    note_id: int, user: Annotated[dict, Depends(require_user)]
) -> NoteOut:
    """Fetch one of YOUR notes by id (login required)."""
    note = store.NOTES.get(note_id)
    if note is None or note["owner"] != user["login"]:
        raise HTTPException(status_code=404, detail="note not found")
    return NoteOut.model_validate(note)


@router.post("/notes", response_model=NoteOut, tags=["notes:mine"])
async def create_note(
    payload: NoteCreate, user: Annotated[dict, Depends(require_user)]
) -> NoteOut:
    """Create a note owned by you (login required)."""
    note = store.create_note(user["login"], payload.title, payload.body)
    return NoteOut.model_validate(note)


@router.delete("/notes/{note_id}", response_model=NoteOut, tags=["notes:mine"])
async def delete_note(note_id: int, user: Annotated[dict, Depends(require_user)]) -> NoteOut:
    """Delete one of YOUR notes (login required)."""
    note = store.NOTES.get(note_id)
    if note is None or note["owner"] != user["login"]:
        raise HTTPException(status_code=404, detail="note not found")
    del store.NOTES[note_id]
    return NoteOut.model_validate(note)


@router.get("/stats", response_model=StatsOut, tags=["meta"])
async def stats() -> StatsOut:
    """Public counters — no login needed."""
    return StatsOut(
        notes=len(store.NOTES),
        users=len({n["owner"] for n in store.NOTES.values()}),
    )


@router.get(
    "/notes/{note_id}/summary",
    response_model=NoteSummary,
    response_model_exclude_unset=True,
    tags=["notes:mine"],
)
async def note_summary(
    note_id: int, user: Annotated[dict, Depends(require_user)]
) -> NoteSummary:
    """Sparse summary of YOUR note — how response filtering is bridged.

    ``pinned``/``color`` are only emitted when actually set; with
    ``response_model_exclude_unset`` the JSON may lack those keys, so the
    bridge types this field as a raw ``JSON`` scalar (no field selection)
    and explains why in the field description.
    """
    note = store.NOTES.get(note_id)
    if note is None or note["owner"] != user["login"]:
        raise HTTPException(status_code=404, detail="note not found")
    return NoteSummary(id=note["id"], title=note["title"])


@router.get("/legacy/stats", response_model=StatsOut, tags=["meta"], deprecated=True)
async def legacy_stats() -> StatsOut:
    """Old stats endpoint kept for bookmarks — how deprecation is bridged.

    OpenAPI ``deprecated: true`` becomes GraphQL-native ``@deprecated``:
    hidden from default introspection, still executable. Use ``stats``.
    """
    return stats()


@router.post("/notes/{note_id}/attach", tags=["notes:mine"])
async def attach_file(
    note_id: int,
    user: Annotated[dict, Depends(require_user)],
    filename: Annotated[str, Form(description="name to store the attachment under")],
    blob: Annotated[bytes, File(description="attachment bytes")],
) -> dict[str, int | str]:
    """Attach a file to YOUR note — how Form/File routes are handled.

    Multipart bodies have no MCP input channel (tool arguments are JSON;
    SEP-2631 is still draft), so the scanner skips this route with an
    explicit logged reason rather than exposing a field that would always
    422. The route keeps working over plain HTTP.
    """
    note = store.NOTES.get(note_id)
    if note is None or note["owner"] != user["login"]:
        raise HTTPException(status_code=404, detail="note not found")
    return {"note_id": note_id, "filename": filename, "size": len(blob)}


@router.get("/overview", tags=["meta"])
async def overview() -> dict[str, Any]:
    """Runtime overview — deliberately dynamic, assembled at call time.

    ``dict[str, Any]`` declares "shape is dynamic", so the bridge passes it
    through as the JSON scalar instead of demanding a fixed type — the
    agent sees the live keys and can explore them.
    """
    return {
        "app": "notes-oauth example",
        "notes": {
            "total": len(store.NOTES),
            "owners": sorted({n["owner"] for n in store.NOTES.values()}),
        },
        "now": datetime.now(timezone.utc).isoformat(),
    }
