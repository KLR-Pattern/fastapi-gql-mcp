"""Tag-based domain grouping for progressive disclosure.

Domains form a tree via the ``"a:b"`` separator convention (``tags=["billing:invoice"]``
→ ``("billing", "invoice")``). The tree only shapes *discovery* (which slice of
the schema an agent looks at); execution always runs against the full schema.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from routerql.scanner import RouteInfo

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


class DomainNode:
    """One node of the domain tree aggregating its routes' field names."""

    def __init__(self, path: tuple[str, ...]) -> None:
        self.path = path
        self.query_fields: set[str] = set()
        self.mutation_fields: set[str] = set()

    @property
    def name(self) -> str:
        return self.path[-1]

    def add(self, route: RouteInfo) -> None:
        if route.is_mutation:
            self.mutation_fields.add(route.field_name)
        else:
            self.query_fields.add(route.field_name)

    def summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": list(self.path),
            "queries": len(self.query_fields),
            "mutations": len(self.mutation_fields),
        }


class DomainRegistry:
    """Indexes routes by domain path and answers tree-shaped queries.

    A route with several tags (or whose tag path has depth) is registered at
    every level of each of its domain paths, so ``list_domains`` can expose
    both coarse and fine groupings.
    """

    def __init__(self, routes: Sequence[RouteInfo]) -> None:
        self._nodes: dict[tuple[str, ...], DomainNode] = {}
        for route in routes:
            for domain in route.domains:
                for depth in range(1, len(domain) + 1):
                    node = self._node(domain[:depth])
                    # Only the leaf level carries the field; parents aggregate
                    # counts through their subtree at query time.
                    if depth == len(domain):
                        node.add(route)

    def _node(self, path: tuple[str, ...]) -> DomainNode:
        node = self._nodes.get(path)
        if node is None:
            node = DomainNode(path)
            self._nodes[path] = node
        return node

    def node(self, path: tuple[str, ...]) -> DomainNode | None:
        return self._nodes.get(path)

    def children(self, path: tuple[str, ...] = ()) -> list[str]:
        """Direct child segment names under ``path``."""
        depth = len(path)
        found: set[str] = set()
        for node_path in self._nodes:
            if len(node_path) == depth + 1 and node_path[:depth] == path:
                found.add(node_path[-1])
        return sorted(found)

    def subtree_fields(
        self, path: tuple[str, ...]
    ) -> tuple[set[str], set[str]]:
        """All query/mutation field names at or below ``path``."""
        queries: set[str] = set()
        mutations: set[str] = set()
        for node_path, node in self._nodes.items():
            if node_path[: len(path)] == path:
                queries |= node.query_fields
                mutations |= node.mutation_fields
        return queries, mutations

    def summary(self) -> list[dict[str, object]]:
        """Top-level domain summaries (for the ``list_domains`` MCP tool)."""
        roots: set[tuple[str, ...]] = set()
        for node_path in self._nodes:
            roots.add(node_path[:1])
        result: list[dict[str, object]] = []
        for root in sorted(roots):
            queries, mutations = self.subtree_fields(root)
            children = self.children(root)
            result.append(
                {
                    "name": root[0],
                    "path": list(root),
                    "queries": len(queries),
                    "mutations": len(mutations),
                    **({"subdomains": children} if children else {}),
                }
            )
        return result
