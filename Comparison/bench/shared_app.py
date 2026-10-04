"""One FastAPI app, wired into BOTH frameworks — the shared benchmark target.

The 5 business endpoints (notes CRUD + stats) are realistic: docstrings,
Field descriptions, query args, domain tags — everything both converters
consume. ``route_count`` scales the surface with filler endpoints spread
over a few tag domains, so tool-count/context-growth curves are meaningful.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# ----------------------------------------------------------------- data

NOTES: dict[int, dict] = {
    i: {
        "id": i,
        "title": f"note {i}",
        "body": f"body of note {i} " + "x" * 40,
        "owner": "bench-user",
    }
    for i in range(1, 21)
}
_next_id = 21


class NoteOut(BaseModel):
    """A note owned by the logged-in user."""

    id: int = Field(description="stable note identifier")
    title: str = Field(description="note headline")
    body: str = Field(description="note content")
    owner: str = Field(description="owner login")


class NoteCreate(BaseModel):
    """Payload for creating a note."""

    title: str = Field(description="note headline")
    body: str = Field(default="", description="note content")


class StatsOut(BaseModel):
    """Public aggregate counters."""

    notes: int = Field(description="notes stored")
    owners: int = Field(description="distinct owners")


def build_app(route_count: int = 5) -> FastAPI:
    """5 business endpoints + (route_count - 5) filler endpoints."""
    global _next_id
    _next_id = 21
    router = APIRouter(prefix="/api")

    @router.get("/notes", response_model=list[NoteOut], tags=["notes:mine"])
    async def list_notes(
        q: Annotated[str | None, Query(description="substring over title/body")] = None,
        active: bool = True,
    ) -> list[NoteOut]:
        """List YOUR notes.

        ``q`` filters by substring over title and body.
        """
        notes = list(NOTES.values()) if active else []
        if q:
            notes = [n for n in notes if q.lower() in n["title"].lower()]
        return [NoteOut.model_validate(n) for n in notes]

    @router.get("/notes/{note_id}", response_model=NoteOut, tags=["notes:mine"])
    async def get_note(note_id: int) -> NoteOut:
        """Fetch one of YOUR notes by id."""
        if note_id not in NOTES:
            raise HTTPException(status_code=404, detail="note not found")
        return NoteOut.model_validate(NOTES[note_id])

    @router.post("/notes", response_model=NoteOut, tags=["notes:mine"])
    async def create_note(payload: NoteCreate) -> NoteOut:
        """Create a note owned by you."""
        note = {"id": _next_id, "title": payload.title, "body": payload.body, "owner": "bench-user"}
        NOTES[note["id"]] = note
        _next_id += 1
        return NoteOut.model_validate(note)

    @router.delete("/notes/{note_id}", response_model=NoteOut, tags=["notes:mine"])
    async def delete_note(note_id: int) -> NoteOut:
        """Delete one of YOUR notes."""
        if note_id not in NOTES:
            raise HTTPException(status_code=404, detail="note not found")
        return NoteOut.model_validate(NOTES.pop(note_id))

    @router.get("/stats", response_model=StatsOut, tags=["meta"])
    async def stats() -> StatsOut:
        """Public counters — no login needed."""
        return StatsOut(notes=len(NOTES), owners=len({n["owner"] for n in NOTES.values()}))

    # ---- filler endpoints: distinct function names (required by our scanner),
    # spread over a few tag domains so the domain tree stays realistic.
    # Registered BEFORE include_router — FastAPI snapshots at include time.
    filler = route_count - 5
    if filler > 0:

        class Item(BaseModel):
            """A filler item."""

            id: int = Field(description="item id")
            name: str = Field(description="item name")

        domains = ["shop:catalog", "shop:orders", "iam"]

        def make_handler(n: int):
            async def handler(item_id: int = n) -> Item:
                """Fetch filler item."""
                return Item(id=item_id, name=f"item-{n}")

            handler.__name__ = f"thing_{n}"
            return handler

        for i in range(filler):
            domain = domains[i % len(domains)]
            router.get(f"/thing{i}", response_model=Item, tags=[domain])(
                make_handler(i)
            )

    app = FastAPI(title="bench-notes", version="1.0.0", description="shared benchmark app")
    app.include_router(router)
    return app
