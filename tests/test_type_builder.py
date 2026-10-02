"""TypeBuilder: Pydantic annotations -> graphql-core types."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Literal, Optional

import pytest
from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLNonNull,
    GraphQLObjectType,
)
from pydantic import BaseModel, Field

from routerql.type_builder import (
    TypeBuilder,
    UnsupportedFieldTypeError,
    describe_literal_values,
)


def render(t) -> str:
    return str(t)


class TestScalars:
    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            (int, "Int!"),
            (str, "String!"),
            (bool, "Boolean!"),
            (float, "Float!"),
            (datetime, "DateTime!"),
            (date, "Date!"),
            (time, "Time!"),
            (uuid.UUID, "UUID!"),
            (Decimal, "Decimal!"),
        ],
    )
    def test_scalar_matrix(self, annotation, expected):
        assert render(TypeBuilder().output_type(annotation)) == expected

    def test_optional_scalar(self):
        assert render(TypeBuilder().output_type(Optional[int])) == "Int"  # noqa: UP045

    def test_int_or_none_union_type(self):
        assert render(TypeBuilder().output_type(int | None)) == "Int"

    def test_list_of_scalars(self):
        assert render(TypeBuilder().output_type(list[int])) == "[Int!]!"

    def test_list_of_optional(self):
        assert render(TypeBuilder().output_type(list[int | None])) == "[Int]!"

    def test_optional_list(self):
        # typing.Optional syntax must keep working alongside `X | None`
        assert render(TypeBuilder().output_type(Optional[list[str]])) == "[String!]"  # noqa: UP045


class TestLiteral:
    def test_str_literal(self):
        assert render(TypeBuilder().output_type(Literal["a", "b"])) == "String!"

    def test_int_literal(self):
        assert render(TypeBuilder().output_type(Literal[1, 2])) == "Int!"

    def test_literal_description(self):
        assert describe_literal_values(Literal["a", "b"]) == "Allowed values: 'a', 'b'"
        assert describe_literal_values(Optional[Literal["a"]]) == (  # noqa: UP045
            "Allowed values: 'a' (or null)"
        )

    def test_mixed_literal_raises(self):
        with pytest.raises(UnsupportedFieldTypeError, match="mixed Literal"):
            TypeBuilder().output_type(Literal["a", 1])

    def test_literal_as_input(self):
        assert render(TypeBuilder().input_type(Literal["a", "b"])) == "String"


class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


class Priority(IntEnum):
    LOW = 1
    HIGH = 2


# Python identifiers are always legal GraphQL names, so the functional API is
# the only way to construct an illegal member name.
BadMember = Enum("BadMember", {"has space": "x"})


class TestEnums:
    def test_enum_output(self):
        t = TypeBuilder().output_type(Color)
        assert isinstance(t, GraphQLNonNull)
        assert isinstance(t.of_type, GraphQLEnumType)
        assert t.of_type.name == "Color"
        assert t.of_type.values["RED"].value == "red"

    def test_int_enum(self):
        t = TypeBuilder().output_type(Priority)
        assert t.of_type.values["LOW"].value == 1

    def test_illegal_member_name_raises(self):
        with pytest.raises(UnsupportedFieldTypeError, match="not a legal GraphQL name"):
            TypeBuilder().output_type(BadMember)


class Address(BaseModel):
    city: str
    zip_code: str = Field(description="postal code")


class UserOut(BaseModel):
    id: int
    name: str
    address: Address
    tags: list[str] = []
    nickname: str | None = None
    status: Literal["active", "frozen"] = Field(description="account status")


class TestObjects:
    def test_nested_model(self):
        t = TypeBuilder().output_type(UserOut)
        inner = t.of_type
        assert isinstance(inner, GraphQLObjectType)
        assert inner.name == "UserOut"
        fields = inner.fields
        assert str(fields["id"].type) == "Int!"
        assert str(fields["address"].type) == "Address!"
        assert str(fields["tags"].type) == "[String!]!"
        assert str(fields["nickname"].type) == "String"
        assert str(fields["status"].type) == "String!"
        assert "Allowed values: 'active', 'frozen'" in (fields["status"].description or "")
        assert fields["address"].type.of_type.fields["zip_code"].description == "postal code"

    def test_recursive_model(self):
        class Node(BaseModel):
            value: int
            child: Optional["Node"] = None
            children: list["Node"] = []

        t = TypeBuilder().output_type(Node)
        assert str(t.of_type.fields["child"].type) == "Node"
        assert str(t.of_type.fields["children"].type) == "[Node!]!"

    def test_alias_output_field_name(self):
        class Aliased(BaseModel):
            user_name: str = Field(alias="userName")
            hidden: str = Field(serialization_alias="shown")

        fields = TypeBuilder().output_type(Aliased).of_type.fields
        assert set(fields) == {"userName", "shown"}

    def test_input_output_split(self):
        b = TypeBuilder()
        out = b.output_type(UserOut)
        inp = b.input_type(UserOut)
        assert isinstance(out.of_type, GraphQLObjectType)
        assert out.of_type.name == "UserOut"
        assert isinstance(inp, GraphQLInputObjectType)
        assert inp.name == "UserOutInput"

    def test_input_field_nullability_follows_required(self):
        inp = TypeBuilder().input_type(UserOut)
        fields = inp.fields
        assert str(fields["id"].type) == "Int!"
        assert str(fields["nickname"].type) == "String"
        assert str(fields["tags"].type) == "[String!]"

    def test_input_field_uses_validation_alias(self):
        class Body(BaseModel):
            page_size: int = Field(validation_alias="pageSize", default=10)

        fields = TypeBuilder().input_type(Body).fields
        assert set(fields) == {"pageSize"}
        assert fields["pageSize"].default_value == 10

    def test_same_name_different_class_qualified(self, caplog):
        import sys
        from types import ModuleType

        mod_a = ModuleType("m_a")
        exec("from pydantic import BaseModel\nclass Item(BaseModel):\n    a: int\n", mod_a.__dict__)
        mod_b = ModuleType("m_b")
        exec("from pydantic import BaseModel\nclass Item(BaseModel):\n    b: int\n", mod_b.__dict__)
        sys.modules.update({"m_a": mod_a, "m_b": mod_b})

        b = TypeBuilder()
        t1 = b.output_type(mod_a.Item)
        t2 = b.output_type(mod_b.Item)
        assert t1.of_type.name == "Item"
        assert t2.of_type.name != "Item"
        assert t2.of_type.name.startswith("Item_")
        with caplog.at_level("WARNING"):
            b.output_type(mod_b.Item)
        assert any("collision" in r.message for r in caplog.records)

    def test_unsupported_type_raises(self):
        with pytest.raises(UnsupportedFieldTypeError):
            TypeBuilder().output_type(dict[str, int])

    def test_unsupported_nested_field_reports_path(self):
        class Bad(BaseModel):
            ok: int
            payload: dict[str, int]

        with pytest.raises(UnsupportedFieldTypeError, match=r"Bad\.payload"):
            TypeBuilder().output_type(Bad)

    def test_model_without_fields_raises(self):
        class Empty(BaseModel):
            pass

        with pytest.raises(UnsupportedFieldTypeError, match="no usable fields"):
            TypeBuilder().output_type(Empty)

    def test_output_list_of_models(self):
        assert str(TypeBuilder().output_type(list[UserOut])) == "[UserOut!]!"

    def test_list_type_detected(self):

        assert str(TypeBuilder().output_type(list[int])) == "[Int!]!"
