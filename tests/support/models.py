"""Shared sample models — one definition per concept, imported by suites.

Every model here replaces at least two identical local copies that had
drifted across files. Keep them minimal: only fields that tests assert on.
Suites needing a richer shape keep their own local model on purpose.
"""

from pydantic import BaseModel


class ItemOut(BaseModel):
    id: int
    name: str


class ItemCreate(BaseModel):
    name: str
    price: float = 9.9


class Ok(BaseModel):
    ok: bool


class Err(BaseModel):
    code: int
    message: str


class Node(BaseModel):
    name: str
    children: list["Node"] = []


Node.model_rebuild()


class UserOut(BaseModel):
    id: int
    name: str
    email: str | None = None


class UserCreate(BaseModel):
    name: str
    email: str | None = None


class ThingOut(BaseModel):
    id: int


class WhoOut(BaseModel):
    user: str


class ProbeOut(BaseModel):
    user: str
    x_internal_token_seen: bool


class NoteIn(BaseModel):
    text: str


class NoteOut(BaseModel):
    author: str
    text: str
