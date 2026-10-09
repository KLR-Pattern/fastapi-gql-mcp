"""Regression suite for issue #3: type-mapping gaps (fastapi-gql-mcp#3).

Tiered by necessity — each tier is the acceptance net for one fix batch.
Every test asserts the TARGET (post-fix) behavior, so the whole file is
red until its batch lands:

- Tier 1 (P0, batch 1) — failure isolation: one bad model must skip only
  its own routes; the schema build must never crash globally.
- Tier 2 (P1, batches 2-3) — mapping capabilities: set collections,
  Literal with enum members, TypedDict.
- Tier 3 (P2, batch 4) — input-union JSON fallback (encodes option A,
  symmetric with the output side) and unbound-TypeVar diagnostics.

The issue's characterization tests (which pass on 0.9.0 by reproducing
the bugs) invert into this suite. Two existing pins flip alongside their
batches: TypeBuilder set[int] raises (test_type_builder) and input-side
unions skip (test_handler_schema.TestUnionFallback).
"""

from enum import Enum, IntEnum
from typing import Generic, Literal, Optional, TypeVar

import pytest
from fastapi import FastAPI, Query
from graphql import GraphQLInputObjectType, GraphQLObjectType
from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict

from fastapi_gql_mcp.handler import RouterGraphQLHandler
from fastapi_gql_mcp.type_builder import (
    TypeBuilder,
    UnsupportedFieldTypeError,
    describe_literal_values,
)


class Opaque:
    """Arbitrary class: pydantic accepts it (is-instance schema, with
    arbitrary_types_allowed) but GraphQL cannot express it — and it stays
    unmapped through every planned fix batch, unlike set/Literal[enum]."""


class SharedBad(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    payload: Opaque


class InnerBad(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    payload: Opaque


class OuterGood(BaseModel):
    inner: InnerBad
    ok: int


class FirstMode(Enum):
    FIRST = "first"


class SecondMode(Enum):
    SECOND = "second"


class Level(IntEnum):
    LOW = 1
    HIGH = 2


class TagBag(BaseModel):
    values: set[str]


class FlatRecord(TypedDict):
    value: str


class PartialRecord(TypedDict, total=False):
    value: str


class NestedRecord(TypedDict):
    value: str


class RecordContainer(BaseModel):
    nested: NestedRecord


class Chain(TypedDict):
    label: str
    child: Optional["Chain"]  # noqa: UP045


T = TypeVar("T")


class GenericEnvelope(BaseModel, Generic[T]):
    value: T


def baseline_app() -> FastAPI:
    """One always-mappable route: the canary that must survive every skip."""
    app = FastAPI()

    @app.get("/baseline", response_model=str)
    async def baseline() -> str:
        return "ok"

    return app


def _paths(handler: RouterGraphQLHandler) -> list[str]:
    return [r.path for r in handler.routes]


def _reasons(handler: RouterGraphQLHandler) -> list[str]:
    return [s.reason for s in handler.skips]


# ---------------------------------------------------------------------- tier 1


class TestTier1FailureIsolation:
    """P0 (issue #1): a failed model build poisons the shared type cache, a
    later route reusing the model passes scanning, and the whole schema
    build dies. Target: registration is transactional — every reuse of a
    bad model gets its own skip, and unrelated routes survive."""

    def test_shared_bad_model_skips_both_routes_schema_survives(self):
        app = baseline_app()

        @app.get("/case-one", response_model=SharedBad)
        async def case_one() -> dict:
            return {"payload": Opaque()}

        @app.get("/case-two", response_model=SharedBad)
        async def case_two() -> dict:
            return {"payload": Opaque()}

        handler = RouterGraphQLHandler(app)  # 0.9.0: TypeError here
        assert "/baseline" in _paths(handler)
        assert "/case-one" not in _paths(handler)
        assert "/case-two" not in _paths(handler)
        # each reuse gets its OWN skip record naming the offending field
        assert sum("SharedBad.payload" in r for r in _reasons(handler)) == 2

    def test_nested_bad_model_skips_every_dependent_route(self):
        """Rollback must cover every recursion frame: OuterGood registers,
        its InnerBad field fails, both frames roll back — so a second
        OuterGood route and a direct InnerBad route each fail afresh."""
        app = baseline_app()

        @app.get("/outer-a", response_model=OuterGood)
        async def outer_a() -> dict:
            return {"inner": {"payload": Opaque()}, "ok": 1}

        @app.get("/outer-b", response_model=OuterGood)
        async def outer_b() -> dict:
            return {"inner": {"payload": Opaque()}, "ok": 2}

        @app.get("/inner", response_model=InnerBad)
        async def inner() -> dict:
            return {"payload": Opaque()}

        handler = RouterGraphQLHandler(app)
        assert "/baseline" in _paths(handler)
        assert {"/outer-a", "/outer-b", "/inner"}.isdisjoint(_paths(handler))
        assert sum("InnerBad.payload" in r for r in _reasons(handler)) == 3

    def test_shared_bad_input_model_is_skipped_per_route(self):
        """The input side registers before building fields too — same
        poisoning path, same isolation contract."""
        app = baseline_app()

        @app.post("/case-one", response_model=str)
        async def case_one(payload: SharedBad) -> str:
            return "ok"

        @app.post("/case-two", response_model=str)
        async def case_two(payload: SharedBad) -> str:
            return "ok"

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert "/baseline" in _paths(handler)
        assert "/case-one" not in _paths(handler)
        assert "/case-two" not in _paths(handler)
        assert sum("SharedBad.payload" in r for r in _reasons(handler)) == 2

    def test_failed_build_is_not_cached(self):
        """The rollback mechanism at builder level: a failed build leaves
        nothing behind, so a retry fails afresh — never a cached half-built
        type that detonates the schema at GraphQLSchema time."""
        builder = TypeBuilder()
        with pytest.raises(UnsupportedFieldTypeError):
            builder.output_type(SharedBad)
        with pytest.raises(UnsupportedFieldTypeError):
            builder.output_type(SharedBad)
        with pytest.raises(UnsupportedFieldTypeError):
            builder.input_type(SharedBad)
        with pytest.raises(UnsupportedFieldTypeError):
            builder.input_type(SharedBad)


# ---------------------------------------------------------------------- tier 2


class TestTier2SetCollections:
    """P1 (issue #3): set-like collections map onto GraphQL lists — the
    JSON wire format pydantic emits for a set IS an array."""

    def test_set_output_type(self):
        assert str(TypeBuilder().output_type(set[str])) == "[String!]!"

    def test_frozenset_output_type(self):
        assert str(TypeBuilder().output_type(frozenset[int])) == "[Int!]!"

    def test_optional_set_output_type(self):
        assert str(TypeBuilder().output_type(set[str] | None)) == "[String!]"

    def test_set_input_type(self):
        assert str(TypeBuilder().input_type(set[str])) == "[String!]"

    def test_set_response_and_field_build(self):
        app = baseline_app()

        @app.get("/case", response_model=set[str])
        async def case() -> set[str]:
            return {"value"}

        @app.get("/bag", response_model=TagBag)
        async def bag() -> TagBag:
            return TagBag(values={"a"})

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        sdl = handler.get_sdl()
        assert "case: [String!]" in sdl
        assert "values: [String!]!" in sdl

    async def test_set_response_executes_as_json_array(self):
        app = baseline_app()

        @app.get("/case", response_model=set[str], tags=["g"])
        async def case() -> set[str]:
            return {"value"}

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ g { case } }")
        assert result == {"data": {"g": {"case": ["value"]}}}
        await handler.aclose()

    @staticmethod
    def _set_query_app() -> FastAPI:
        app = baseline_app()

        @app.get("/q", response_model=str, tags=["g"])
        async def q(tags: set[str] = Query()) -> str:
            return ",".join(sorted(tags))

        return app

    def test_set_query_param_builds_list_argument(self):
        handler = RouterGraphQLHandler(self._set_query_app())
        assert handler.skips == []
        assert "q(tags: [String!]!): String" in handler.get_sdl()

    async def test_set_query_param_executes(self):
        handler = RouterGraphQLHandler(self._set_query_app())
        result = await handler.execute('{ g { q(tags: ["a", "b"]) } }')
        assert result == {"data": {"g": {"q": "a,b"}}}
        await handler.aclose()


class TestTier2LiteralEnumMembers:
    """P1 (issue #2): a Literal member that is an enum instance normalizes
    to its underlying value, then rides the existing scalar path."""

    def test_str_enum_member_literal_maps_to_string(self):
        assert str(TypeBuilder().output_type(Literal[FirstMode.FIRST])) == "String!"

    def test_int_enum_member_literal_maps_to_int(self):
        assert str(TypeBuilder().output_type(Literal[Level.HIGH])) == "Int!"

    def test_enum_member_mixed_with_raw_value(self):
        # FirstMode.FIRST normalizes to "first": one member type, String
        assert str(
            TypeBuilder().output_type(Literal[FirstMode.FIRST, "extra"])
        ) == "String!"

    def test_description_renders_normalized_value(self):
        assert (
            describe_literal_values(Literal[FirstMode.FIRST])
            == "Allowed values: 'first'"
        )

    def test_literal_enum_input(self):
        assert str(TypeBuilder().input_type(Literal[FirstMode.FIRST])) == "String"

    def test_literal_enum_response_builds(self):
        app = baseline_app()

        @app.get("/case", response_model=Literal[FirstMode.FIRST], tags=["g"])
        async def case() -> FirstMode:
            return FirstMode.FIRST

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        sdl = handler.get_sdl()
        assert "case: String" in sdl
        assert "Allowed values: 'first'" in sdl

    async def test_literal_enum_response_executes(self):
        app = baseline_app()

        @app.get("/case", response_model=Literal[FirstMode.FIRST], tags=["g"])
        async def case() -> FirstMode:
            return FirstMode.FIRST

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ g { case } }")
        assert result == {"data": {"g": {"case": "first"}}}
        await handler.aclose()


class TestTier2TypedDict:
    """P1 (issue #6): TypedDict is a structural declaration — it maps onto
    real object/input types, with nullability from __required_keys__ /
    __optional_keys__ (total=False => all optional)."""

    def test_flat_typeddict_output(self):
        obj = TypeBuilder().output_type(FlatRecord).of_type
        assert isinstance(obj, GraphQLObjectType)
        assert obj.name == "FlatRecord"
        assert str(obj.fields["value"].type) == "String!"

    def test_total_false_fields_nullable(self):
        obj = TypeBuilder().output_type(PartialRecord).of_type
        assert str(obj.fields["value"].type) == "String"

    def test_typeddict_input(self):
        inp = TypeBuilder().input_type(FlatRecord)
        assert isinstance(inp, GraphQLInputObjectType)
        assert inp.name == "FlatRecordInput"
        assert str(inp.fields["value"].type) == "String!"

    def test_nested_typeddict_inside_model(self):
        obj = TypeBuilder().output_type(RecordContainer).of_type
        assert str(obj.fields["nested"].type) == "NestedRecord!"

    def test_recursive_typeddict(self):
        obj = TypeBuilder().output_type(Chain).of_type
        assert str(obj.fields["label"].type) == "String!"
        assert str(obj.fields["child"].type) == "Chain"

    def test_typeddict_routes_build(self):
        app = baseline_app()

        @app.get("/case-flat", response_model=FlatRecord, tags=["g"])
        async def case_flat() -> dict:
            return {"value": "flat"}

        @app.get("/case-nested", response_model=RecordContainer, tags=["g"])
        async def case_nested() -> dict:
            return {"nested": {"value": "nested"}}

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        sdl = handler.get_sdl()
        assert "type FlatRecord" in sdl
        assert "nested: NestedRecord!" in sdl

    @staticmethod
    def _typeddict_app() -> FastAPI:
        app = baseline_app()

        @app.get("/case-nested", response_model=RecordContainer, tags=["g"])
        async def case_nested() -> dict:
            return {"nested": {"value": "nested"}}

        @app.get("/partial", response_model=PartialRecord, tags=["g"])
        async def partial() -> dict:
            return {}

        @app.post("/submit", response_model=str, tags=["g"])
        async def submit(payload: FlatRecord) -> str:
            return payload["value"]

        return app

    async def test_nested_typeddict_executes(self):
        handler = RouterGraphQLHandler(self._typeddict_app(), allow_mutation=True)
        result = await handler.execute("{ g { case_nested { nested { value } } } }")
        assert result == {"data": {"g": {"case_nested": {"nested": {"value": "nested"}}}}}
        await handler.aclose()

    async def test_total_false_missing_key_resolves_null(self):
        handler = RouterGraphQLHandler(self._typeddict_app(), allow_mutation=True)
        result = await handler.execute("{ g { partial { value } } }")
        assert result == {"data": {"g": {"partial": {"value": None}}}}
        await handler.aclose()

    async def test_typeddict_body_param_executes(self):
        handler = RouterGraphQLHandler(self._typeddict_app(), allow_mutation=True)
        result = await handler.execute(
            'mutation { g { submit(payload: {value: "x"}) } }'
        )
        assert result == {"data": {"g": {"submit": "x"}}}
        await handler.aclose()


# ---------------------------------------------------------------------- tier 3


class TestTier3InputUnionFallback:
    """P2 (issue #4): input unions bridge as the JSON scalar — option A,
    symmetric with the output side's union fallback. NOTE: this reverses
    TestUnionFallback.test_input_side_union_still_unsupported in
    test_handler_schema.py (a documented 0.9.0 decision); that pin flips
    when this batch lands. Encodes the recommended option — flip both if
    the design call goes the other way (merged input enum)."""

    def test_union_of_enums_query_param_bridges_as_json(self):
        app = baseline_app()

        @app.get("/case", response_model=str, tags=["g"])
        async def case(value: FirstMode | SecondMode = Query()) -> str:
            return value.value

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        assert "case(value: JSON!): String" in handler.get_sdl()

    @staticmethod
    def _union_app() -> FastAPI:
        app = baseline_app()

        @app.get("/case", response_model=str, tags=["g"])
        async def case(value: FirstMode | SecondMode = Query()) -> str:
            return value.value

        @app.post("/echo", response_model=str, tags=["g"])
        async def echo(payload: FirstMode | SecondMode) -> str:
            return payload.value

        return app

    async def test_union_query_param_executes(self):
        handler = RouterGraphQLHandler(self._union_app(), allow_mutation=True)
        result = await handler.execute('{ g { case(value: "first") } }')
        assert result == {"data": {"g": {"case": "first"}}}
        await handler.aclose()

    async def test_union_body_param_bridges_and_executes(self):
        handler = RouterGraphQLHandler(self._union_app(), allow_mutation=True)
        sdl = handler.get_sdl()
        assert handler.skips == []
        assert "echo(payload: JSON!): String" in sdl
        result = await handler.execute('mutation { g { echo(payload: "second") } }')
        assert result == {"data": {"g": {"echo": "second"}}}
        await handler.aclose()


class TestTier3UnboundTypeVar:
    """P2 (issue #5): an unparameterized generic already skips only its own
    route — what's missing is a diagnostic an author can act on: name the
    model and the way out, not just the bare `~T`."""

    def test_unparameterized_generic_skips_with_actionable_reason(self):
        app = baseline_app()

        @app.get("/case", response_model=GenericEnvelope, tags=["g"])
        async def case() -> dict:
            return {"value": "unbound"}

        handler = RouterGraphQLHandler(app)
        assert "/baseline" in _paths(handler)
        assert "/case" not in _paths(handler)
        assert any(
            "GenericEnvelope" in r and "unbound" in r.lower()
            for r in _reasons(handler)
        )
