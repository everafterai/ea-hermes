"""``hermes tools rbac`` — list registered tools by toolset with built-in role coverage.

Helps operators verify that every toolset in the registry is intentionally
covered by their RBAC role assignments. For each registered toolset the
command shows:

- The tool names it contains.
- Which built-in roles grant it (admin, operator, readonly, chat_only), either
  because the role explicitly grants the toolset or because it is in
  FLOOR_TOOLSETS (granted to every valid-role user).

Usage::

    hermes tools rbac              # human-readable table
    hermes tools rbac --json       # machine-readable JSON

This command is read-only: it never mutates configuration or registry state.
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List


def _compute_role_grants() -> Dict[str, List[str]]:
    """Return ``{toolset: [role, ...]}`` for every registered toolset.

    Roles are checked in definition order; ``admin`` will always appear
    because it holds ``"*"`` which matches everything.
    Floor toolsets (``clarify``, ``todo``) are granted to every built-in
    role regardless of the explicit toolset list.
    """
    from gateway.tool_access import BUILTIN_ROLES, FLOOR_TOOLSETS, _granted
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()

    toolset_names = registry.get_registered_toolset_names()
    result: Dict[str, List[str]] = {}
    for toolset in toolset_names:
        granting: List[str] = []
        for role_name, role_toolsets in BUILTIN_ROLES.items():
            if _granted(role_toolsets, toolset) or toolset in FLOOR_TOOLSETS:
                granting.append(role_name)
        result[toolset] = granting
    return result


def handle_tools_rbac(args) -> int:
    """Handler for ``hermes tools rbac [--json]``."""
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()

    toolsets = registry.get_registered_toolset_names()
    role_grants = _compute_role_grants()

    # Build structured data: {toolset: {tools: [...], builtin_roles: [...]}}
    data: Dict[str, dict] = {}
    for toolset in sorted(toolsets):
        tools = registry.get_tool_names_for_toolset(toolset)
        data[toolset] = {
            "tools": sorted(tools),
            "builtin_roles": role_grants.get(toolset, []),
        }

    use_json = getattr(args, "json", False)

    if use_json:
        print(json.dumps(data, indent=2))
        return 0

    # Human-readable table output
    total_tools = sum(len(v["tools"]) for v in data.values())
    print(f"\nRegistered toolsets: {len(data)}  |  Total tools: {total_tools}\n")

    col_ts = max((len(ts) for ts in data), default=10)
    col_roles = 40
    header = (
        f"{'Toolset':<{col_ts}}  "
        f"{'Tools':>5}  "
        f"{'Built-in roles granting'}"
    )
    print(header)
    print("-" * len(header))

    for toolset, info in data.items():
        tool_count = len(info["tools"])
        roles_str = ", ".join(info["builtin_roles"]) if info["builtin_roles"] else "(none)"
        print(f"{toolset:<{col_ts}}  {tool_count:>5}  {roles_str}")
        for tool_name in info["tools"]:
            print(f"  {'':>{col_ts - 2}}{tool_name}")

    print()
    print(
        "Note: 'admin' (role toolsets={\"*\"}) and the 'mcp-*' glob also cover\n"
        "dynamically-registered MCP tools that are not listed here at startup."
    )
    return 0


def register_tools_rbac_subcommand(tools_sub) -> None:
    """Add the ``rbac`` subparser to an existing ``tools`` subparsers object.

    Called from ``hermes_cli/main.py`` inside the ``tools`` command block,
    passing the already-created ``tools_sub`` subparsers action.
    """
    rbac_parser = tools_sub.add_parser(
        "rbac",
        help=(
            "List every registered tool grouped by toolset and annotate which "
            "built-in roles (admin, operator, readonly, chat_only) grant each toolset"
        ),
        description=(
            "Read-only diagnostic: discovers all built-in tools via the tool registry, "
            "groups them by toolset, and for each toolset shows which built-in RBAC roles "
            "grant it. Useful for operators verifying per-user role coverage.\n\n"
            "admin always appears (holds '*'). FLOOR_TOOLSETS (clarify, todo) are granted "
            "to every valid-role user regardless of their explicit role assignment.\n\n"
            "MCP tools registered at runtime (mcp-* toolsets) are not shown here; "
            "admin's '*' and the mcp-* glob in operator/readonly cover them."
        ),
    )
    rbac_parser.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=False,
        help="Print machine-readable JSON instead of the human-readable table",
    )
    rbac_parser.set_defaults(func=handle_tools_rbac)
