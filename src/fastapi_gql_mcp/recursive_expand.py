"""Recursive-chain unrolling: recursive fields return their true depth.

GraphQL documents are finite, so a selection on a recursive type silently
truncates the data at the selected depth — and INVISIBLY: ``children: []``
on a leaf looks exactly like a truncated subtree, so an agent cannot tell
a complete answer from a partial one. The route already computed the full
tree (routes are called once; nested resolution is pure in-memory
projection) — the document is the only thing limiting what gets walked.

Unrolling removes that limitation: the selection set where the agent stops
becomes the repeating template, stamped back under the recursive edge up
to ``unroll_limit()`` levels. Data still stops where it stops — empty lists
terminate the walk — so responses carry the tree's true depth while
per-level field filtering stays the agent's own native selections, at any
position in the document (the chain need not start at the root).

``unroll_limit()`` is a function of the process recursion budget, not a
fixed bound — it scales automatically when an operator raises
``sys.setrecursionlimit`` to serve deeper trees. If a response's recursive
nesting still reaches the floor, the handler appends a notice to the
GraphQL ``errors`` channel (data stays; truncation is never silent).
"""

from __future__ import annotations

import sys
from typing import Any

from graphql import GraphQLSchema
from graphql.language import FieldNode
from graphql.language.ast import DocumentNode, NameNode, SelectionSetNode
from graphql.language.visitor import Visitor, visit
from graphql.type.definition import GraphQLList, GraphQLNonNull, GraphQLObjectType
from graphql.utilities import TypeInfo


def _unwrap(field_type: Any) -> Any:
    while isinstance(field_type, (GraphQLNonNull, GraphQLList)):
        field_type = field_type.of_type
    return field_type


def unroll_limit() -> int:
    """How deep the unrolled document may go — a function of the process's
    recursion budget, not a fixed number.

    Measured under Python's default recursion limit (1000 frames): trees die
    at ~90 levels through the real app (FastAPI/pydantic serialization) and
    at ~82 under pure graphql-core projection (~8 frames per level) — no
    stage is "the" chokepoint; the universal constraint is recursion budget
    divided by per-level frame cost. ``max(100, recursionlimit // 10)`` sits
    at that ceiling under defaults and scales up automatically when an
    operator raises ``sys.setrecursionlimit`` to serve deeper trees, while
    validation (~2 frames per unrolled level) stays at a fifth of budget.
    """
    return max(100, sys.getrecursionlimit() // 10)


def hit_unroll_floor(data: Any, edges: frozenset[tuple[str, str]], limit: int) -> bool:
    """True when the response's recursive nesting reaches the unroll limit —
    i.e. deeper data MAY have been truncated (a true leaf at exactly that
    depth is indistinguishable, hence the conditional wording upstream).

    Iterative on purpose: the whole point is to inspect a tree as deep as
    the limit without spending recursion on it."""
    edge_names = {fname for _, fname in edges}
    stack: list[tuple[Any, int]] = [(data, 0)]
    deepest = 0
    while stack:
        value, depth = stack.pop()
        if depth > deepest:
            deepest = depth
        if isinstance(value, dict):
            for key, child in value.items():
                if key in edge_names and isinstance(child, list):
                    if child:
                        stack.append((child, depth + 1))
                else:
                    stack.append((child, depth))
        elif isinstance(value, list):
            for item in value:
                stack.append((item, depth))
    return deepest >= limit


def recursive_edges(schema: GraphQLSchema) -> frozenset[tuple[str, str]]:
    """(type name, field name) pairs whose field type IS the owning type —
    direct self-reference (``Node.children: [Node!]``).

    Introspection meta-types are excluded; mutual cycles (``A.b: B``,
    ``B.a: A``) are deliberately not marked — template stamping is only
    defined for direct self-reference (v1 boundary)."""
    return frozenset(
        (t.name, fname)
        for t in schema.type_map.values()
        if isinstance(t, GraphQLObjectType) and not t.name.startswith("__")
        for fname, field in t.fields.items()
        if _unwrap(field.type) is t
    )


def expand_recursive_chains(
    document: DocumentNode,
    schema: GraphQLSchema,
    edges: frozenset[tuple[str, str]],
    depth: int,
) -> DocumentNode:
    """Return ``document`` with every recursive chain unrolled to ``depth``.

    A chain end is the deepest selection of a back-edge field along a path
    (detected anywhere in the document); its selection set becomes the
    template repeated under the back-edge. Chains whose template carries
    fragments are left untouched — GraphQL forbids self-referencing
    fragments, so nobody expresses recursion that way, and degrading to the
    unexpanded document is the honest fallback."""
    if not edges or depth <= 0:
        return document

    class ChainMarker(Visitor):
        def __init__(self, type_info: TypeInfo) -> None:
            Visitor.__init__(self)
            self.type_info = type_info
            self.chain_ends: dict[int, str] = {}

        def enter(self, node: Any, *_args: Any) -> None:
            self.type_info.enter(node)
            if not isinstance(node, FieldNode):
                return
            parent = self.type_info.get_parent_type()
            field_def = self.type_info.get_field_def()
            if parent is None or field_def is None:
                return
            if (parent.name, node.name.value) not in edges:
                return
            own = _unwrap(field_def.type).name
            selections = (
                node.selection_set.selections if node.selection_set else ()
            )
            if any(
                isinstance(s, FieldNode) and (own, s.name.value) in edges
                for s in selections
            ):
                return  # a deeper edge selection exists: not the chain end
            if any(not isinstance(s, FieldNode) for s in selections):
                return  # fragments inside the template: skip this chain
            self.chain_ends[id(node)] = node.name.value

        def leave(self, node: Any, *_args: Any) -> None:
            self.type_info.leave(node)

    class Expander(Visitor):
        def __init__(self, chain_ends: dict[int, str], levels: int) -> None:
            Visitor.__init__(self)
            self.chain_ends = chain_ends
            self.levels = levels

        def leave(self, node: Any, *_args: Any) -> Any:
            if not (isinstance(node, FieldNode) and id(node) in self.chain_ends):
                return None
            template = node.selection_set
            assert template is not None  # chain ends always carry one
            current = template
            for _ in range(self.levels):
                inner = FieldNode(
                    name=NameNode(value=node.name.value), selection_set=current
                )
                # AST child collections must be tuples, not lists.
                current = SelectionSetNode(
                    selections=(*template.selections, inner)
                )
            return FieldNode(
                alias=node.alias,
                name=node.name,
                arguments=node.arguments,
                directives=node.directives,
                selection_set=current,
            )

    marker = ChainMarker(TypeInfo(schema))
    marked = visit(document, marker)  # no edits: same tree, ids stay valid
    assert marked is document
    expanded = visit(document, Expander(marker.chain_ends, depth))
    assert isinstance(expanded, DocumentNode)
    return expanded
