"""Wiring tests for the ``hermes users`` argparse subcommand group (Task C).

These verify that ``register_users_subcommands`` attaches a ``users`` group with
``list/add/update/delete`` subparsers that parse correctly and dispatch to the
Task-B handlers (through a thin exit-code adapter).

The ``users`` group is registered inside ``hermes_cli.main.main`` (after
``build_top_level_parser`` returns), so we exercise ``register_users_subcommands``
directly against a fresh ``argparse.ArgumentParser`` + subparsers — the same
subparsers-action type main.py hands it. The registration is self-contained, so
this seam faithfully covers parsing, dispatch, and exit-code propagation without
reconstructing the full top-level parser.
"""

import argparse

import pytest
import yaml

from hermes_cli.users import (
    handle_users_add,
    handle_users_delete,
    handle_users_list,
    handle_users_update,
    register_users_subcommands,
)


def _build_parser():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    register_users_subcommands(subparsers)
    return parser


# --- tmp HERMES_HOME fixture (mirrors tests/cli/test_users_cli.py) ----------

CONFIG_WITH_SLACK = """\
# Hermes config — managed by hand for this test.
slack:
  bot_token: xoxb-test
  require_mention: true
"""


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(None)
    from hermes_cli import config as config_mod

    config_mod._RAW_CONFIG_CACHE.clear()
    yield tmp_path
    reset_hermes_home_override(token)
    config_mod._RAW_CONFIG_CACHE.clear()


def _write_config(home, text):
    path = home / "config.yaml"
    path.write_text(text, encoding="utf-8")
    from hermes_cli import config as config_mod

    config_mod._RAW_CONFIG_CACHE.clear()
    return path


def _load_slack(home):
    path = home / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("slack") or {}


# --- parsing ---------------------------------------------------------------


def test_add_parses_user_id_role_and_name():
    parser = _build_parser()
    args = parser.parse_args(["users", "add", "U1", "operator", "--name", "Alice"])
    assert args.user_id == "U1"
    assert args.role == "operator"
    assert args.name == "Alice"
    assert hasattr(args, "func")


def test_update_role_optional_name_only():
    parser = _build_parser()
    args = parser.parse_args(["users", "update", "U1", "--name", "Bob"])
    assert args.user_id == "U1"
    assert args.role is None
    assert args.name == "Bob"


def test_list_json_flag():
    parser = _build_parser()
    args = parser.parse_args(["users", "list", "--json"])
    assert args.json is True


def test_delete_parses_user_id():
    parser = _build_parser()
    args = parser.parse_args(["users", "delete", "U1"])
    assert args.user_id == "U1"


def test_func_targets_resolve_to_real_handlers():
    """The adapters must wrap the genuine Task-B handlers (not something else)."""
    parser = _build_parser()
    for argv, handler in [
        (["users", "list"], handle_users_list),
        (["users", "add", "U1", "operator"], handle_users_add),
        (["users", "update", "U1", "readonly"], handle_users_update),
        (["users", "delete", "U1"], handle_users_delete),
    ]:
        args = parser.parse_args(argv)
        assert getattr(args.func, "__wrapped_handler__", None) is handler


# --- end-to-end dispatch ----------------------------------------------------


def test_add_dispatch_writes_user_to_disk(hermes_home, capsys):
    _write_config(hermes_home, CONFIG_WITH_SLACK)
    parser = _build_parser()
    args = parser.parse_args(["users", "add", "U1", "operator", "--name", "Alice"])

    rc = args.func(args)  # success path returns the (zero) rc, no SystemExit
    assert not rc

    extra = _load_slack(hermes_home)
    assert extra["user_roles"]["U1"] == "operator"
    assert extra["user_names"]["U1"] == "Alice"


def test_duplicate_add_exits_nonzero(hermes_home, capsys):
    _write_config(hermes_home, CONFIG_WITH_SLACK)
    parser = _build_parser()

    args1 = parser.parse_args(["users", "add", "U1", "operator"])
    assert not args1.func(args1)

    args2 = parser.parse_args(["users", "add", "U1", "operator"])
    with pytest.raises(SystemExit) as excinfo:
        args2.func(args2)
    assert excinfo.value.code == 1


def test_users_no_action_prints_help_and_exits_nonzero(hermes_home, capsys):
    parser = _build_parser()
    args = parser.parse_args(["users"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "users" in out.lower()
