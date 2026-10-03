"""GraphQL field names come from the FastAPI endpoint function's name.

The function that implements a route carries the developer's own vocabulary —
``async def list_active_users(...)`` is a better field name than anything
reconstructed from ``/users?active=true``. Python identifiers are always legal
GraphQL names, so no sanitization is needed; the path/query/body parameters
still become the field's arguments (see ``scanner``).

Because function names are only unique per module, two routes CAN map onto the
same field name — that fails fast with ``DuplicateFieldError``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.routing import APIRoute


class DuplicateFieldError(ValueError):
    """Two routes map onto the same GraphQL field name in one namespace."""


def field_name_for(route: APIRoute) -> str:
    """Derive the GraphQL field name for one route: the endpoint function name."""
    name = route.name
    if not name or not name.isidentifier():  # pragma: no cover - defensive
        raise ValueError(
            f"Route {route.path} has no usable endpoint function name ({name!r})"
        )
    return name


def validate_field_names(fields: Sequence[tuple[str, str, str]]) -> None:
    """Fail fast on duplicate field names within one namespace.

    Args:
        fields: ``(field_name, method, path)`` triples for one operation type.
    """
    seen: dict[str, tuple[str, str]] = {}
    for field_name, method, path in fields:
        previous = seen.get(field_name)
        if previous is not None:
            raise DuplicateFieldError(
                f"GraphQL field name {field_name!r} is used by both "
                f"{previous[0]} {previous[1]} and {method} {path} — both "
                f"endpoints are implemented by functions of the same name. "
                f"Rename one endpoint function, or filter one route out with "
                f"include=/exclude= globs."
            )
        seen[field_name] = (method, path)
