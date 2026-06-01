"""Tests for ``hermes_cli.tools_list`` — RBAC-annotated tool listing.

Calls the handler directly (``--json`` mode) and verifies:
- Known toolsets are present in the output.
- ``admin`` is always among the granting roles for any toolset.
- Floor toolsets (``clarify``, ``todo``) are granted to more roles than
  a side-effecting toolset like ``terminal``.
- The JSON output is valid and structurally correct.

Assertions check for *presence*, not exact full lists, so the tests remain
robust to new tools being added.
"""
from __future__ import annotations

import json
import types

import pytest


def _make_args(use_json: bool = True) -> types.SimpleNamespace:
    """Return a minimal args namespace as argparse would produce."""
    return types.SimpleNamespace(json=use_json)


class TestHandleToolsRbacJson:
    def test_produces_valid_json(self, capsys):
        from hermes_cli.tools_list import handle_tools_rbac

        rc = handle_tools_rbac(_make_args(use_json=True))
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)  # raises ValueError on invalid JSON
        assert isinstance(data, dict)

    def test_known_toolsets_present(self, capsys):
        from hermes_cli.tools_list import handle_tools_rbac

        handle_tools_rbac(_make_args(use_json=True))
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # These toolsets are always registered by builtin tools.
        for expected_toolset in ("terminal", "web", "clarify", "todo"):
            assert expected_toolset in data, (
                f"Expected toolset '{expected_toolset}' not found in output. "
                f"Available: {sorted(data.keys())}"
            )

    def test_each_entry_has_expected_shape(self, capsys):
        from hermes_cli.tools_list import handle_tools_rbac

        handle_tools_rbac(_make_args(use_json=True))
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        for toolset, info in data.items():
            assert "tools" in info, f"Toolset '{toolset}' missing 'tools' key"
            assert "builtin_roles" in info, f"Toolset '{toolset}' missing 'builtin_roles' key"
            assert isinstance(info["tools"], list)
            assert isinstance(info["builtin_roles"], list)

    def test_admin_always_in_granting_roles(self, capsys):
        from hermes_cli.tools_list import handle_tools_rbac

        handle_tools_rbac(_make_args(use_json=True))
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # admin has "*" so it must grant every toolset.
        for toolset, info in data.items():
            assert "admin" in info["builtin_roles"], (
                f"Expected 'admin' to grant toolset '{toolset}' (via '*'), "
                f"but got roles: {info['builtin_roles']}"
            )

    def test_floor_toolsets_granted_to_more_roles_than_terminal(self, capsys):
        from hermes_cli.tools_list import handle_tools_rbac

        handle_tools_rbac(_make_args(use_json=True))
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # clarify and todo are FLOOR_TOOLSETS — granted to every valid-role user.
        # terminal is operator-only (not in readonly / chat_only).
        # Therefore floor toolsets must have at least as many granting roles.
        floor_toolsets = ("clarify", "todo")
        terminal_roles = set(data.get("terminal", {}).get("builtin_roles", []))
        for floor_ts in floor_toolsets:
            if floor_ts in data:
                floor_roles = set(data[floor_ts]["builtin_roles"])
                assert len(floor_roles) >= len(terminal_roles), (
                    f"Floor toolset '{floor_ts}' has {len(floor_roles)} granting roles "
                    f"but 'terminal' has {len(terminal_roles)}; expected floor >= terminal"
                )

    def test_tools_list_non_empty_for_known_toolsets(self, capsys):
        from hermes_cli.tools_list import handle_tools_rbac

        handle_tools_rbac(_make_args(use_json=True))
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        for ts in ("terminal", "web", "clarify", "todo"):
            if ts in data:
                assert len(data[ts]["tools"]) > 0, (
                    f"Expected at least one tool under toolset '{ts}'"
                )


class TestHandleToolsRbacHumanReadable:
    def test_human_readable_output_contains_toolsets(self, capsys):
        from hermes_cli.tools_list import handle_tools_rbac

        rc = handle_tools_rbac(_make_args(use_json=False))
        assert rc == 0
        captured = capsys.readouterr()
        output = captured.out

        assert "terminal" in output
        assert "admin" in output
        # Summary line
        assert "toolset" in output.lower()

    def test_human_readable_mentions_admin_wildcard_note(self, capsys):
        from hermes_cli.tools_list import handle_tools_rbac

        handle_tools_rbac(_make_args(use_json=False))
        captured = capsys.readouterr()
        assert "admin" in captured.out
        assert "mcp-*" in captured.out or "MCP" in captured.out


class TestModuleImport:
    def test_smoke_import(self):
        import hermes_cli.tools_list  # noqa: F401
        import hermes_cli._parser  # noqa: F401

    def test_register_function_exists(self):
        from hermes_cli.tools_list import register_tools_rbac_subcommand
        assert callable(register_tools_rbac_subcommand)

    def test_handler_function_exists(self):
        from hermes_cli.tools_list import handle_tools_rbac
        assert callable(handle_tools_rbac)
