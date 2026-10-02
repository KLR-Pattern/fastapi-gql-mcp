"""naming: deterministic field-name derivation."""

import pytest

from routerql.naming import DuplicateFieldError, field_name_for, validate_field_names


class TestFieldNameFor:
    def test_get_with_path_param(self):
        assert field_name_for("GET", "/users/{user_id}") == "get_users_by_user_id"

    def test_create(self):
        assert field_name_for("POST", "/users") == "create_users"

    def test_update_verbs_share_prefix(self):
        assert field_name_for("PUT", "/users/{user_id}") == "update_users_by_user_id"
        assert field_name_for("PATCH", "/users/{user_id}") == "update_users_by_user_id"

    def test_delete(self):
        assert field_name_for("DELETE", "/users/{user_id}") == "delete_users_by_user_id"

    def test_multi_segment(self):
        assert field_name_for("GET", "/users/{user_id}/orders") == (
        "get_users_orders_by_user_id"
    )

    def test_camel_and_kebab_segments(self):
        assert field_name_for("GET", "/userProfiles/reset-tokens") == (
            "get_user_profiles_reset_tokens"
        )

    def test_root_path(self):
        assert field_name_for("GET", "/") == "get_root"

    def test_collection_and_item_do_not_collide(self):
        assert field_name_for("GET", "/items") == "get_items"
        assert field_name_for("GET", "/items/{item_id}") == "get_items_by_item_id"

    def test_multiple_params(self):
        assert field_name_for("GET", "/a/{x}/b/{y}") == "get_a_b_by_x_y"

    def test_unknown_verb_raises(self):
        with pytest.raises(ValueError, match="No verb prefix"):
            field_name_for("HEAD", "/users")


class TestValidateFieldNames:
    def test_ok(self):
        validate_field_names([("get_users", "GET", "/users"), ("get_orders", "GET", "/orders")])

    def test_duplicate_raises_with_both_routes(self):
        with pytest.raises(DuplicateFieldError) as exc:
            validate_field_names(
                [("get_users", "GET", "/users"), ("get_users", "GET", "/user-list")]
            )
        msg = str(exc.value)
        assert "GET /users" in msg and "GET /user-list" in msg
        assert "exclude=" in msg

    def test_namespaces_are_independent(self):
        # Same name in Query and Mutation namespaces is legal GraphQL.
        validate_field_names([("users", "GET", "/users")])
        validate_field_names([("users", "POST", "/users")])
