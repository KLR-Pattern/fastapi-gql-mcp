"""Tag-based domain grouping for progressive disclosure.

Domains form a tree via the ``"a:b"`` separator convention (``tags=["billing:invoice"]``
→ ``("billing", "invoice")``). The tree only shapes *discovery* (which slice of
the schema an agent looks at); execution always runs against the full schema.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_GENERAL: tuple[str, ...] = ("general",)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _sanitize_segment(segment: str) -> str:
    snake = _CAMEL_BOUNDARY.sub("_", segment).replace("-", "_").lower()
    return re.sub(r"[^a-z0-9_]", "_", snake) or "_"


def domains_for(tags: Sequence[str], path: str) -> frozenset[tuple[str, ...]]:
    """Compute the domain paths a route belongs to.

    Rules (in order):
    1. Each tag becomes one domain path, split on ``:``.
    2. Without tags, the first non-parameter path segment is the domain.
    3. Fallback: ``general``.
    """
    if tags:
        return frozenset(
            tuple(_sanitize_segment(seg) for seg in tag.split(":")) for tag in tags
        )
    for segment in path.split("/"):
        if segment and not (segment.startswith("{") and segment.endswith("}")):
            return frozenset({(_sanitize_segment(segment),)})
    return frozenset({_GENERAL})
