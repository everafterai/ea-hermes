"""Tests for the ``hermes users`` I/O layer + argparse handlers (Task B).

These exercise the file-touching layer in ``hermes_cli/users.py``:
``handle_users_{list,add,update,delete}`` plus ``_mutate_slack_extra`` /
``_read_slack_extra``. They point Hermes at a temporary HERMES_HOME (via the
``monkeypatch.setenv("HERMES_HOME", ...)`` mechanism the rest of the suite
uses) seeded with a ``config.yaml`` that contains a *commented* ``slack:``
block, so we can assert comment preservation across writes.
"""

import types

import pytest
import yaml


CONFIG_WITH_SLACK = """\
# Hermes config — managed by hand for this test.
slack:
  # IMPORTANT: keep this comment — it proves comment preservation.
  bot_token: xoxb-test
  extra:
    foo: bar
"""

CONFIG_NO_SLACK = """\
# Hermes config with no slack platform.
model: gpt-test
"""

COMMENT_NEEDLE = "keep this comment — it proves comment preservation"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and clear any sticky override.

    Also clears the read_raw_config cache between tests so on-disk reads
    reflect what each test just wrote (the cache keys on path+mtime+size,
    but a fresh tmp_path per test plus an explicit clear keeps it honest).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Belt-and-suspenders: make sure no contextvar override shadows the env.
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(None)
    # Clear read_raw_config cache so reads see fresh writes.
    from hermes_cli import config as config_mod

    config_mod._RAW_CONFIG_CACHE.clear()
    yield tmp_path
    reset_hermes_home_override(token)
    config_mod._RAW_CONFIG_CACHE.clear()


def _write_config(home, text):
    path = home / "config.yaml"
    path.write_text(text, encoding="utf-8")
    # Clear cache so the next read_raw_config sees the new content.
    from hermes_cli import config as config_mod

    config_mod._RAW_CONFIG_CACHE.clear()
    return path


def _load_extra(home):
    """yaml.safe_load the on-disk config and return slack.extra (or {})."""
    path = home / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (data.get("slack") or {}).get("extra") or {}


def _ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


# --- add -------------------------------------------------------------------


def test_add_writes_user_roles_and_preserves_comment(hermes_home, capsys):
    from hermes_cli.users import handle_users_add

    path = _write_config(hermes_home, CONFIG_WITH_SLACK)

    rc = handle_users_add(_ns(user_id="U1", role="operator", name=None))
    assert rc == 0

    extra = _load_extra(hermes_home)
    assert extra["user_roles"]["U1"] == "operator"

    # The pre-existing comment survives the round-trip write.
    text = path.read_text(encoding="utf-8")
    assert COMMENT_NEEDLE in text


def test_add_with_name_writes_user_names(hermes_home, capsys):
    from hermes_cli.users import handle_users_add

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    rc = handle_users_add(_ns(user_id="U1", role="operator", name="Alice"))
    assert rc == 0

    extra = _load_extra(hermes_home)
    assert extra["user_names"]["U1"] == "Alice"


def test_add_admin_lands_in_allow_admin_from(hermes_home, capsys):
    from hermes_cli.users import handle_users_add

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    rc = handle_users_add(_ns(user_id="U1", role="admin", name=None))
    assert rc == 0

    extra = _load_extra(hermes_home)
    assert "U1" in extra["allow_admin_from"]
    out = capsys.readouterr().out
    assert "slash-admin" in out


def test_first_add_prints_rbac_activation_warning(hermes_home, capsys):
    from hermes_cli.users import handle_users_add

    _write_config(hermes_home, CONFIG_WITH_SLACK)

    handle_users_add(_ns(user_id="U1", role="operator", name=None))
    first_out = capsys.readouterr().out
    assert "RBAC is now ACTIVE" in first_out
    assert "DENIED" in first_out

    # A second add must NOT re-print the activation warning.
    handle_users_add(_ns(user_id="U2", role="readonly", name=None))
    second_out = capsys.readouterr().out
    assert "RBAC is now ACTIVE" not in second_out


# --- update ----------------------------------------------------------------


def test_update_demote_admin_removes_from_allow_admin_from(hermes_home, capsys):
    from hermes_cli.users import handle_users_add, handle_users_update

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    handle_users_add(_ns(user_id="U1", role="admin", name=None))
    capsys.readouterr()

    rc = handle_users_update(_ns(user_id="U1", role="operator", name=None))
    assert rc == 0

    extra = _load_extra(hermes_home)
    assert extra["user_roles"]["U1"] == "operator"
    assert "U1" not in extra.get("allow_admin_from", [])
    out = capsys.readouterr().out
    assert "Revoked slash-admin" in out


def test_update_name_only(hermes_home, capsys):
    from hermes_cli.users import handle_users_add, handle_users_update

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    handle_users_add(_ns(user_id="U1", role="operator", name=None))
    capsys.readouterr()

    rc = handle_users_update(_ns(user_id="U1", role=None, name="Bob"))
    assert rc == 0

    extra = _load_extra(hermes_home)
    assert extra["user_names"]["U1"] == "Bob"
    assert extra["user_roles"]["U1"] == "operator"


# --- delete ----------------------------------------------------------------


def test_delete_removes_everywhere_and_deactivates(hermes_home, capsys):
    from hermes_cli.users import handle_users_add, handle_users_delete

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    handle_users_add(_ns(user_id="U1", role="admin", name="Alice"))
    capsys.readouterr()

    rc = handle_users_delete(_ns(user_id="U1"))
    assert rc == 0

    extra = _load_extra(hermes_home)
    assert "U1" not in (extra.get("user_roles") or {})
    assert "U1" not in (extra.get("user_names") or {})
    assert "U1" not in (extra.get("allow_admin_from") or [])

    out = capsys.readouterr().out
    assert "Revoked slash-admin" in out
    assert "RBAC is now INACTIVE" in out


def test_delete_non_last_does_not_print_deactivation(hermes_home, capsys):
    from hermes_cli.users import handle_users_add, handle_users_delete

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    handle_users_add(_ns(user_id="U1", role="operator", name=None))
    handle_users_add(_ns(user_id="U2", role="operator", name=None))
    capsys.readouterr()

    handle_users_delete(_ns(user_id="U1"))
    out = capsys.readouterr().out
    assert "RBAC is now INACTIVE" not in out


# --- list ------------------------------------------------------------------


def test_list_table_reflects_state_and_admin_marker(hermes_home, capsys):
    from hermes_cli.users import handle_users_add, handle_users_list

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    handle_users_add(_ns(user_id="U1", role="admin", name="Alice"))
    handle_users_add(_ns(user_id="U2", role="operator", name=None))
    capsys.readouterr()

    rc = handle_users_list(_ns(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ACTIVE" in out
    assert "USER_ID" in out and "ROLE" in out and "ADMIN" in out
    assert "U1" in out and "Alice" in out and "admin" in out
    # Admin marker present for U1's row.
    u1_line = next(line for line in out.splitlines() if line.startswith("U1"))
    assert "✓" in u1_line
    u2_line = next(line for line in out.splitlines() if line.startswith("U2"))
    assert "✓" not in u2_line


def test_list_json_reflects_state(hermes_home, capsys):
    import json as _json
    from hermes_cli.users import handle_users_add, handle_users_list

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    handle_users_add(_ns(user_id="U1", role="admin", name="Alice"))
    capsys.readouterr()

    rc = handle_users_list(_ns(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["rbac_active"] is True
    assert payload["allow_admin_from"] == ["U1"]
    u1 = payload["users"][0]
    assert u1["user_id"] == "U1"
    assert u1["role"] == "admin"
    assert u1["name"] == "Alice"
    assert u1["admin"] is True


def test_list_inactive_header_when_empty(hermes_home, capsys):
    from hermes_cli.users import handle_users_list

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    rc = handle_users_list(_ns(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "INACTIVE" in out
    assert "No Slack users configured." in out


# --- error paths -----------------------------------------------------------


def test_add_without_slack_block_returns_1(hermes_home, capsys):
    from hermes_cli.users import handle_users_add

    _write_config(hermes_home, CONFIG_NO_SLACK)
    rc = handle_users_add(_ns(user_id="U1", role="operator", name=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "No `slack:` platform configured" in err


def test_add_when_config_missing_returns_1(hermes_home, capsys):
    from hermes_cli.users import handle_users_add

    # No config.yaml written at all.
    rc = handle_users_add(_ns(user_id="U1", role="operator", name=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "No `slack:` platform configured" in err


def test_add_existing_user_returns_1(hermes_home, capsys):
    from hermes_cli.users import handle_users_add

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    handle_users_add(_ns(user_id="U1", role="operator", name=None))
    capsys.readouterr()

    rc = handle_users_add(_ns(user_id="U1", role="admin", name=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "already exists" in err


def test_update_missing_user_returns_1(hermes_home, capsys):
    from hermes_cli.users import handle_users_update

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    rc = handle_users_update(_ns(user_id="U1", role="operator", name=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_add_invalid_role_returns_1(hermes_home, capsys):
    from hermes_cli.users import handle_users_add

    _write_config(hermes_home, CONFIG_WITH_SLACK)
    rc = handle_users_add(_ns(user_id="U1", role="wizard", name=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown role" in err
