"""Deterministic GraphQL field names derived from HTTP method + path.

``GET /users`` → ``get_users``; ``GET /users/{user_id}`` → ``get_users_by_user_id``
(path params become ``_by_{param}`` name parts AND arguments, so the idiomatic
REST pair "collection + item" never collides). Collisions inside the Query (or
Mutation) namespace fail fast with an actionable error.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

VERB_PREFIX: dict[str, str] = {
    "GET": "get",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class DuplicateFieldError(ValueError):
    """Two routes map onto the same GraphQL field name in one namespace."""


def _segment_to_snake(segment: str) -> str:
    """camelCase / kebab-case path segment -> snake_case."""
    spaced = _CAMEL_BOUNDARY.sub("_", segment)
    return spaced.replace("-", "_").lower()


def field_name_for(method: str, path: str) -> str:
    """Derive the GraphQL field name for one route.

    Args:
        method: uppercase HTTP verb, one of VERB_PREFIX's keys.
        path: FastAPI route path, e.g. ``/users/{user_id}/orders``.

    Raises:
        ValueError: If ``method`` has no registered verb prefix.
    """
    prefix = VERB_PREFIX.get(method.upper())
    if prefix is None:
        raise ValueError(f"No verb prefix registered for HTTP method {method!r}")
    segments: list[str] = []
    params: list[str] = []
    for seg in path.split("/"):
        if not seg:
            continue
        if seg.startswith("{") and seg.endswith("}"):
            params.append(_segment_to_snake(seg[1:-1]))
        else:
            segments.append(_segment_to_snake(seg))
    parts = [prefix, *segments]
    if params:
        parts.append("by")
        parts.extend(params)
    if len(parts) == 1:
        return f"{prefix}_root"
    return "_".join(parts)


def validate_field_names(
    fields: Sequence[tuple[str, str, str]],
) -> None:
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
                f"{previous[0]} {previous[1]} and {method} {path}. "
                f"Rename one route path, or filter one of them out with "
                f"include=/exclude= globs."
            )
        seen[field_name] = (method, path)
