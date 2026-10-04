"""Build graphql-core types from Pydantic models.

Maps FastAPI's Pydantic annotations onto a programmatic ``GraphQLSchema``:

- ``BaseModel`` → ``GraphQLObjectType`` (output) / ``GraphQLInputObjectType`` (input)
- ``Enum`` → ``GraphQLEnumType``; ``Literal`` → its underlying scalar
- ``Optional``/``X | None`` → nullable; plain annotations → non-null
- Output field names follow the JSON keys FastAPI emits (alias first);
  input field names follow what FastAPI validates (validation_alias first).

Type-name collisions between same-named classes from different modules are
resolved by qualifying with the module tail (with a warning); everything else
fails fast with ``UnsupportedFieldTypeError`` so the scanner can skip the route.
"""

from __future__ import annotations

import inspect
import logging
import re
import types
from enum import Enum
from typing import Any, Literal, Union, cast, get_args, get_origin

from graphql import (
    GraphQLBoolean,
    GraphQLEnumType,
    GraphQLField,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLOutputType,
    GraphQLScalarType,
    GraphQLString,
    Undefined,
)
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from fastapi_gql_mcp.scalars import SCALAR_MAP, json_passthrough

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[_a-zA-Z][_a-zA-Z0-9]*$")

# Literal member types that map onto a built-in GraphQL scalar.
_LITERAL_SCALARS: dict[type, GraphQLScalarType] = {
    str: GraphQLString,
    int: GraphQLInt,
    bool: GraphQLBoolean,
}


class UnsupportedFieldTypeError(TypeError):
    """Raised when an annotation cannot be represented in GraphQL."""

    def __init__(self, annotation: Any, context: str) -> None:
        self.annotation = annotation
        self.context = context
        super().__init__(
            f"Cannot map {annotation!r} to a GraphQL type ({context}). "
            f"Supported: scalars (int/str/bool/float/datetime/date/time/UUID/Decimal), "
            f"Enum, Literal, Optional/list combinations, nested BaseModel, and "
            f"dict/Any (pass-through as the JSON scalar)."
        )


def sanitize_graphql_name(name: str, *, what: str = "name") -> str:
    """Coerce an identifier into a legal GraphQL name (warns when changed)."""
    sanitized = re.sub(r"[^_a-zA-Z0-9]", "_", name)
    if not sanitized or sanitized[0].isdigit():
        sanitized = f"_{sanitized or 'unnamed'}"
    if sanitized != name:
        logger.warning("Sanitized GraphQL %s %r -> %r", what, name, sanitized)
    return sanitized


def is_annotated(annotation: Any) -> bool:
    return hasattr(annotation, "__metadata__")


def strip_annotated(annotation: Any) -> Any:
    while is_annotated(annotation):
        annotation = get_args(annotation)[0]
    return annotation


def is_optional_annotation(annotation: Any) -> bool:
    annotation = strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return type(None) in get_args(annotation)
    return False


def unwrap_optional(annotation: Any) -> Any:
    annotation = strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        remaining = [a for a in get_args(annotation) if a is not type(None)]
        if len(remaining) == 1:
            return remaining[0]
    return annotation


def is_list_annotation(annotation: Any) -> bool:
    return get_origin(strip_annotated(annotation)) in (list, tuple)


def literal_values(annotation: Any) -> tuple[Any, ...] | None:
    """Return Literal values, or None if the annotation is not a Literal."""
    annotation = unwrap_optional(annotation)
    return get_args(annotation) if get_origin(annotation) is Literal else None


def literal_scalar(annotation: Any) -> GraphQLScalarType | None:
    """Map ``Literal["a", "b"]`` onto its underlying scalar, else None."""
    values = literal_values(annotation)
    if values is None:
        return None
    if not values:
        raise UnsupportedFieldTypeError(annotation, "empty Literal")
    member_types = {type(v) for v in values}
    if len(member_types) != 1:
        raise UnsupportedFieldTypeError(
            annotation, f"mixed Literal member types {sorted(t.__name__ for t in member_types)}"
        )
    scalar = _LITERAL_SCALARS.get(next(iter(member_types)))
    if scalar is None:
        raise UnsupportedFieldTypeError(
            annotation, f"Literal of {next(iter(member_types)).__name__} has no GraphQL scalar"
        )
    return scalar


def describe_literal_values(annotation: Any) -> str | None:
    """Human-readable allowed-values suffix for Literal fields (mirrors nexusx)."""
    values = literal_values(annotation)
    if values is None:
        return None
    rendered = ", ".join(repr(v) if isinstance(v, str) else str(v) for v in values)
    nullable = is_optional_annotation(annotation)
    suffix = " (or null)" if nullable else ""
    return f"Allowed values: {rendered}{suffix}"


def _own_doc(cls: type) -> str | None:
    """A class's own docstring only — inherited ones (e.g. BaseModel's) are noise."""
    doc = cls.__dict__.get("__doc__")
    return inspect.cleandoc(doc).strip() if doc else None


def _field_description(field: FieldInfo, annotation: Any) -> str | None:
    parts = [field.description] if field.description else []
    literal_note = describe_literal_values(annotation)
    if literal_note:
        parts.append(literal_note)
    return "\n\n".join(parts) if parts else None


class TypeBuilder:
    """Registry-backed converter from Pydantic annotations to graphql-core types.

    Instances are caches keyed by Python class, so recursive models terminate:
    the type object is registered *before* its fields thunk runs.
    """

    def __init__(self) -> None:
        self._object_types: dict[type, GraphQLObjectType] = {}
        self._input_types: dict[type, GraphQLInputObjectType] = {}
        self._enum_types: dict[type, GraphQLEnumType] = {}
        self._name_owner: dict[str, type] = {}
        self._built_output_fields: dict[type, dict[str, GraphQLField]] = {}
        self._built_input_fields: dict[type, dict[str, GraphQLInputField]] = {}

    # ------------------------------------------------------------------ names

    def _register_name(self, cls: type, base: str) -> str:
        if base in self._name_owner and self._name_owner[base] is not cls:
            module_tail = sanitize_graphql_name(cls.__module__.rsplit(".", 1)[-1])
            qualified = f"{base}_{module_tail}"
            candidate, counter = qualified, 2
            while candidate in self._name_owner and self._name_owner[candidate] is not cls:
                candidate = f"{qualified}_{counter}"
                counter += 1
            logger.warning(
                "GraphQL type name collision: %r already taken by %s; "
                "qualifying %s as %r",
                base,
                self._name_owner[base].__qualname__,
                cls.__qualname__,
                candidate,
            )
            self._name_owner[candidate] = cls
            return candidate
        self._name_owner[base] = cls
        return base

    # ---------------------------------------------------------------- output

    def output_type(self, annotation: Any, *, context: str = "field") -> GraphQLOutputType:
        """Convert an annotation to an output type, applying nullability."""
        if is_optional_annotation(annotation):
            return self._bare_output(unwrap_optional(annotation), context)
        return GraphQLNonNull(self._bare_output(strip_annotated(annotation), context))

    def bare_output_type(
        self, annotation: Any, *, context: str = "field"
    ) -> GraphQLScalarType | GraphQLObjectType | GraphQLEnumType | GraphQLList[Any]:
        """Output type without the outer NonNull — for fields whose errors must
        null only themselves (route responses)."""
        return self._bare_output(strip_annotated(unwrap_optional(annotation)), context)

    def _bare_output(
        self, annotation: Any, context: str
    ) -> GraphQLScalarType | GraphQLObjectType | GraphQLEnumType | GraphQLList[Any]:
        annotation = strip_annotated(annotation)
        if is_list_annotation(annotation):
            inner = get_args(annotation)[0]
            if is_optional_annotation(inner):
                return GraphQLList(self._bare_output(unwrap_optional(inner), context))
            return GraphQLList(GraphQLNonNull(self._bare_output(inner, context)))

        scalar = literal_scalar(annotation)
        if scalar is not None:
            return scalar

        if isinstance(annotation, type):
            if issubclass(annotation, Enum):
                return self._enum_type(annotation)
            if issubclass(annotation, BaseModel):
                return self._object_type(annotation, context)

        if annotation in SCALAR_MAP:
            return SCALAR_MAP[annotation]

        json_scalar = json_passthrough(annotation)
        if json_scalar is not None:
            return json_scalar

        raise UnsupportedFieldTypeError(annotation, context)

    def _object_type(self, model: type[BaseModel], context: str) -> GraphQLObjectType:
        cached = self._object_types.get(model)
        if cached is not None:
            return cached
        name = self._register_name(model, sanitize_graphql_name(model.__name__, what="type name"))
        obj = GraphQLObjectType(
            name=name,
            description=_own_doc(model),
            fields=lambda: self._output_fields_cached(model),
        )
        # Register BEFORE building fields so recursive models resolve the cycle
        # to this same object, then build eagerly so unsupported nested types
        # raise UnsupportedFieldTypeError here — graphql-core's lazy `.fields`
        # would otherwise swallow it into a generic TypeError at schema time.
        self._object_types[model] = obj
        self._output_fields_cached(model)
        return obj

    def _output_fields_cached(self, model: type[BaseModel]) -> dict[str, GraphQLField]:
        cached = self._built_output_fields.get(model)
        if cached is None:
            cached = self._output_fields(model)
            self._built_output_fields[model] = cached
        return cached

    def _output_fields(self, model: type[BaseModel]) -> dict[str, GraphQLField]:
        fields: dict[str, GraphQLField] = {}
        for field_name, info in model.model_fields.items():
            # FastAPI serializes responses by alias, so GraphQL field names must
            # match the JSON keys the resolver will actually see.
            json_name = info.serialization_alias or info.alias or field_name
            if not isinstance(json_name, str):
                json_name = field_name
            gname = sanitize_graphql_name(json_name, what=f"{model.__name__} field")
            annotation = info.annotation
            gtype = self.output_type(annotation, context=f"{model.__name__}.{field_name}")
            fields[gname] = GraphQLField(gtype, description=_field_description(info, annotation))
        if not fields:
            raise UnsupportedFieldTypeError(model, f"{model.__name__} has no usable fields")
        return fields

    # ----------------------------------------------------------------- input

    def input_type(self, annotation: Any, *, context: str = "argument") -> GraphQLInputType:
        """Convert an annotation to an input type WITHOUT outer nullability.

        Whether an argument is required is decided by FastAPI's own `required`
        flag at the argument level (NonNull there), not by the annotation.
        """
        annotation = unwrap_optional(annotation)
        if is_list_annotation(annotation):
            inner = get_args(annotation)[0]
            if is_optional_annotation(inner):
                # Optional items: [T]
                return GraphQLList(self.input_type(unwrap_optional(inner), context=context))
            # Non-null items: [T!]
            return GraphQLList(
                GraphQLNonNull(self.input_type(inner, context=context))
            )

        scalar = literal_scalar(annotation)
        if scalar is not None:
            return scalar

        if isinstance(annotation, type):
            if issubclass(annotation, Enum):
                return self._enum_type(annotation)
            if issubclass(annotation, BaseModel):
                return self._input_object_type(annotation, context)

        if annotation in SCALAR_MAP:
            return SCALAR_MAP[annotation]

        json_scalar = json_passthrough(annotation)
        if json_scalar is not None:
            return json_scalar

        raise UnsupportedFieldTypeError(annotation, context)

    def _input_object_type(
        self, model: type[BaseModel], context: str
    ) -> GraphQLInputObjectType:
        cached = self._input_types.get(model)
        if cached is not None:
            return cached
        base = sanitize_graphql_name(model.__name__, what="type name")
        name = self._register_name(model, f"{base}Input")
        obj = GraphQLInputObjectType(
            name=name,
            description=_own_doc(model),
            fields=lambda: self._input_fields_cached(model),
        )
        self._input_types[model] = obj  # register first: cycles resolve to this object
        self._input_fields_cached(model)  # eager: surface errors during scanning
        return obj

    def _input_fields_cached(self, model: type[BaseModel]) -> dict[str, GraphQLInputField]:
        cached = self._built_input_fields.get(model)
        if cached is None:
            cached = self._input_fields(model)
            self._built_input_fields[model] = cached
        return cached

    def _input_fields(self, model: type[BaseModel]) -> dict[str, GraphQLInputField]:
        fields: dict[str, GraphQLInputField] = {}
        for field_name, info in model.model_fields.items():
            # FastAPI validates bodies against validation_alias/alias/name;
            # AliasPath/AliasChoices are too rich for a flat GraphQL name, so
            # those fall back to the field name.
            req_name = info.validation_alias or info.alias or field_name
            if not isinstance(req_name, str):
                req_name = field_name
            gname = sanitize_graphql_name(req_name, what=f"{model.__name__} input field")
            annotation = info.annotation
            bare = cast(
                "GraphQLScalarType | GraphQLEnumType | GraphQLInputObjectType | GraphQLList[Any]",
                self.input_type(annotation, context=f"{model.__name__}.{field_name}"),
            )
            gtype: GraphQLInputType = GraphQLNonNull(bare) if info.is_required() else bare
            fields[gname] = GraphQLInputField(
                gtype,
                description=_field_description(info, annotation),
                default_value=self._input_default(info),
            )
        if not fields:
            raise UnsupportedFieldTypeError(model, f"{model.__name__} has no usable input fields")
        return fields

    @staticmethod
    def _input_default(info: FieldInfo) -> Any:
        default = info.get_default(call_default_factory=False)
        if default is None or isinstance(default, str | int | float | bool):
            return default
        return Undefined

    # ------------------------------------------------------------------ enum

    def _enum_type(self, enum_cls: type[Enum]) -> GraphQLEnumType:
        cached = self._enum_types.get(enum_cls)
        if cached is not None:
            return cached
        for member_name in enum_cls.__members__:
            if not _NAME_RE.match(member_name):
                raise UnsupportedFieldTypeError(
                    enum_cls, f"enum member {member_name!r} is not a legal GraphQL name"
                )
        name = self._register_name(
            enum_cls, sanitize_graphql_name(enum_cls.__name__, what="enum name")
        )
        enum_type = GraphQLEnumType(
            name=name,
            values={member.name: member.value for member in enum_cls},
            description=_own_doc(enum_cls),
        )
        self._enum_types[enum_cls] = enum_type
        return enum_type
