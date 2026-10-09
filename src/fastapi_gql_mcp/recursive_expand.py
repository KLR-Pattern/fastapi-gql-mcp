"""Recursive-chain unrolling: recursive fields return their true depth.

GraphQL documents are finite, so a selection on a recursive type silently
truncates the data at the selected depth — and INVISIBLY: ``children: []``
on a leaf looks exactly like a truncated subtree, so an agent cannot tell
a complete answer from a partial one. The route already computed the full
tree (routes are called once; nested resolution is pure in-memory
projection) — the document is the only thing limiting what gets walked.

Unrolling removes that limitation: the selection set where the agent stops
becomes the repeating template, stamped back under the recursive edge up
to ``UNROLL_LIMIT`` levels. Data still stops where it stops — empty lists
terminate the walk — so responses carry the tree's true depth while
per-level field filtering stays the agent's own native selections, at any
position in the document (the chain need not start at the root).

``UNROLL_LIMIT`` is an implementation constant, not a user-facing bound:
it stays well inside Python's recursion budget (AST validation walks the
unrolled document recursively; runtime execution cost is bounded by actual
data depth, since empty lists stop the walk). Trees deeper than it cannot
practically be served through GraphQL execution anyway — graphql-core
itself recurses per level.
"""

from __future__ import annotations

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


# Implementation ceiling for the unrolled document, not a data limit:
# validation recurses over the document (~2 frames per level), so this
# must fit Python's default recursion budget with room to spare.
UNROLL_LIMIT = 100


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
