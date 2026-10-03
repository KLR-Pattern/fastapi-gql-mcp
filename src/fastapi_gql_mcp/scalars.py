"""Custom GraphQL scalars for Python types graphql-core does not ship.

Decimal travels as a string to preserve precision across JSON boundaries;
DateTime/Date/Time serialize to ISO-8601 and accept "Z" as a UTC suffix.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from graphql import (
    GraphQLBoolean,
    GraphQLFloat,
    GraphQLInt,
    GraphQLScalarType,
    GraphQLString,
)

GraphQLDateTime = GraphQLScalarType(
    name="DateTime",
    description="ISO-8601 datetime string, e.g. 2026-01-02T03:04:05+00:00",
    serialize=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
    parse_value=lambda value: datetime.fromisoformat(str(value).replace("Z", "+00:00")),
)

GraphQLDate = GraphQLScalarType(
    name="Date",
    description="ISO-8601 date string, e.g. 2026-01-02",
    serialize=lambda value: value.isoformat() if isinstance(value, date) else str(value),
    parse_value=lambda value: date.fromisoformat(str(value)),
)

GraphQLTime = GraphQLScalarType(
    name="Time",
    description="ISO-8601 time string, e.g. 03:04:05",
    serialize=lambda value: value.isoformat() if isinstance(value, time) else str(value),
    parse_value=lambda value: time.fromisoformat(str(value)),
)

GraphQLUUID = GraphQLScalarType(
    name="UUID",
    description="RFC 4122 UUID string",
    serialize=lambda value: str(value),
    parse_value=lambda value: uuid.UUID(str(value)),
)


def _parse_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - message variance
        raise ValueError(f"Cannot parse {value!r} as Decimal") from exc


GraphQLDecimal = GraphQLScalarType(
    name="Decimal",
    description=(
        "Exact decimal number transported as a string to preserve precision, "
        'e.g. "12.34"'
    ),
    serialize=lambda value: str(value),
    parse_value=_parse_decimal,
)


SCALAR_MAP: dict[Any, GraphQLScalarType] = {
    int: GraphQLInt,
    str: GraphQLString,
    bool: GraphQLBoolean,
    float: GraphQLFloat,
    datetime: GraphQLDateTime,
    date: GraphQLDate,
    time: GraphQLTime,
    uuid.UUID: GraphQLUUID,
    Decimal: GraphQLDecimal,
}
