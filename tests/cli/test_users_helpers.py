import pytest
from hermes_cli.users import (
    valid_roles,
    apply_add,
    apply_update,
    apply_delete,
    UsersError,
)
from gateway.tool_access import BUILTIN_ROLES


def test_valid_roles_includes_builtins():
    assert set(BUILTIN_ROLES) <= valid_roles({})


def test_valid_roles_includes_custom():
    extra = {"roles": {"auditor": {"toolsets": ["web"]}}}
    assert "auditor" in valid_roles(extra)


def test_add_sets_role():
    extra = {}
    result = apply_add(extra, "U1", "operator", name=None)
    assert extra["user_roles"]["U1"] == "operator"
    assert result.rbac_activated is True  # first entry activates RBAC


def test_add_with_name():
    extra = {}
    apply_add(extra, "U1", "operator", name="Alice")
    assert extra["user_names"]["U1"] == "Alice"


def test_add_admin_grants_allow_admin_from():
    extra = {}
    apply_add(extra, "U1", "admin", name=None)
    assert "U1" in extra["allow_admin_from"]


def test_add_non_admin_not_in_allow_admin_from():
    extra = {}
    apply_add(extra, "U1", "operator", name=None)
    assert "U1" not in extra.get("allow_admin_from", [])


def test_add_existing_raises():
    extra = {"user_roles": {"U1": "operator"}}
    with pytest.raises(UsersError):
        apply_add(extra, "U1", "admin", name=None)


def test_add_invalid_role_raises():
    with pytest.raises(UsersError):
        apply_add({}, "U1", "wizard", name=None)


def test_add_second_user_not_reactivating():
    extra = {"user_roles": {"U1": "operator"}}
    result = apply_add(extra, "U2", "readonly", name=None)
    assert result.rbac_activated is False


def test_update_role_and_admin_sync_add():
    extra = {"user_roles": {"U1": "operator"}}
    apply_update(extra, "U1", role="admin", name=None)
    assert extra["user_roles"]["U1"] == "admin"
    assert "U1" in extra["allow_admin_from"]


def test_update_demote_removes_admin():
    extra = {"user_roles": {"U1": "admin"}, "allow_admin_from": ["U1"]}
    apply_update(extra, "U1", role="operator", name=None)
    assert "U1" not in extra["allow_admin_from"]


def test_update_name_only():
    extra = {"user_roles": {"U1": "operator"}}
    apply_update(extra, "U1", role=None, name="Bob")
    assert extra["user_names"]["U1"] == "Bob"
    assert extra["user_roles"]["U1"] == "operator"


def test_update_missing_raises():
    with pytest.raises(UsersError):
        apply_update({}, "U1", role="operator", name=None)


def test_update_requires_role_or_name():
    extra = {"user_roles": {"U1": "operator"}}
    with pytest.raises(UsersError):
        apply_update(extra, "U1", role=None, name=None)


def test_update_invalid_role_raises():
    extra = {"user_roles": {"U1": "operator"}}
    with pytest.raises(UsersError):
        apply_update(extra, "U1", role="wizard", name=None)


def test_delete_removes_everywhere():
    extra = {
        "user_roles": {"U1": "admin", "U2": "operator"},
        "user_names": {"U1": "Alice"},
        "allow_admin_from": ["U1"],
    }
    result = apply_delete(extra, "U1")
    assert "U1" not in extra["user_roles"]
    assert "U1" not in extra.get("user_names", {})
    assert "U1" not in extra.get("allow_admin_from", [])
    assert result.rbac_deactivated is False  # U2 remains


def test_delete_last_user_deactivates():
    extra = {"user_roles": {"U1": "operator"}}
    result = apply_delete(extra, "U1")
    assert result.rbac_deactivated is True


def test_delete_missing_raises():
    with pytest.raises(UsersError):
        apply_delete({"user_roles": {}}, "U1")
