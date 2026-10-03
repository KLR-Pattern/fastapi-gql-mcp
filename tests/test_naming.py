"""naming: field names from endpoint function names."""

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_gql_mcp.naming import DuplicateFieldError, field_name_for, validate_field_names


class Out(BaseModel):
    id: int


def make_route(app: FastAPI, path: str, func_name: str):
    """Register a GET route whose endpoint carries ``func_name``."""

    def endpoint() -> Out:
        return Out(id=1)

    endpoint.__name__ = func_name
    app.get(path, response_model=Out)(endpoint)
    return next(r for r in app.routes if getattr(r, "path", None) == path)


class TestFieldNameFor:
    def test_function_name_used_verbatim(self):
        app = FastAPI()
        route = make_route(app, "/users/{user_id}", "get_user_by_id")
        assert field_name_for(route) == "get_user_by_id"

    def test_no_verb_prefix_or_param_suffix(self):
        app = FastAPI()
        route = make_route(app, "/items", "list")
        assert field_name_for(route) == "list"

    def test_private_style_names_allowed(self):
        app = FastAPI()
        route = make_route(app, "/x", "_internal")
        assert field_name_for(route) == "_internal"


class TestValidateFieldNames:
    def test_ok(self):
        validate_field_names(
            [("list_users", "GET", "/users"), ("get_order", "GET", "/orders")]
        )

    def test_duplicate_raises_with_both_routes_and_rename_hint(self):
        with pytest.raises(DuplicateFieldError) as exc:
            validate_field_names(
                [("get_item", "GET", "/items/{id}"), ("get_item", "GET", "/products/{id}")]
            )
        msg = str(exc.value)
        assert "GET /items/{id}" in msg and "GET /products/{id}" in msg
        assert "Rename one endpoint function" in msg

    def test_namespaces_are_independent(self):
        # Same name in Query and Mutation namespaces is legal GraphQL.
        validate_field_names([("users", "GET", "/users")])
        validate_field_names([("users", "POST", "/users")])
