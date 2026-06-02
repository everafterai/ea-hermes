"""Pure mutation helpers for the ``hermes users`` CLI.

This module is the logic layer for managing Slack RBAC users that live under
``slack.extra`` in ``~/.hermes/config.yaml``:

  - ``user_roles``      — map ``{user_id: role}``; its presence ACTIVATES RBAC.
  - ``user_names``      — optional map ``{user_id: human_name}``.
  - ``allow_admin_from``— list of ``user_id`` granted slash-admin. Kept in sync
                          with the ``admin`` role: promoting a user to admin
                          ADDs them; any non-admin role (or delete) REMOVEs them.

Every function here is PURE in the sense that it mutates the in-memory
``extra`` dict passed to it and returns a :class:`MutationResult` describing
what happened. There is no file I/O, no argparse, and no printing — those
concerns belong to the I/O / wiring layers (Tasks B/C).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from gateway.tool_access import BUILTIN_ROLES


class UsersError(Exception):
    """Raised for user-facing errors (existing/missing user, invalid role, no-op)."""


@dataclass
class MutationResult:
    rbac_activated: bool = False    # this op created the first user_roles entry
    rbac_deactivated: bool = False  # this op removed the last user_roles entry
    admin_added: bool = False       # id added to allow_admin_from
    admin_removed: bool = False     # id removed from allow_admin_from


def valid_roles(extra: Dict[str, Any]) -> Set[str]:
    """Return the set of assignable role names: built-ins plus custom roles.

    Custom role names are read from ``extra["roles"]`` (a dict) and lowercased.
    A missing or non-dict ``roles`` block yields just the built-ins.
    """
    roles: Set[str] = set(BUILTIN_ROLES)
    custom = extra.get("roles")
    if isinstance(custom, dict):
        for name in custom:
            normalized = str(name).strip().lower()
            if normalized:
                roles.add(normalized)
    return roles


def _validate_role(extra: Dict[str, Any], role: str) -> None:
    allowed = valid_roles(extra)
    if role not in allowed:
        raise UsersError(
            f"unknown role {role!r}; valid roles: {', '.join(sorted(allowed))}"
        )


def _user_roles(extra: Dict[str, Any]) -> Dict[str, Any]:
    return extra.setdefault("user_roles", {})


def _user_names(extra: Dict[str, Any]) -> Dict[str, Any]:
    return extra.setdefault("user_names", {})


def _allow_admin_from(extra: Dict[str, Any]) -> List[Any]:
    return extra.setdefault("allow_admin_from", [])


def _sync_admin(extra: Dict[str, Any], user_id: str, role: str) -> Tuple[bool, bool]:
    """Make ``allow_admin_from`` membership match ``role == "admin"``.

    Returns ``(added, removed)``. The list is de-duplicated and kept as a list
    of str, preserving existing order.
    """
    allow = _allow_admin_from(extra)
    present = user_id in allow
    should = role == "admin"
    added = removed = False
    if should and not present:
        allow.append(user_id)
        added = True
    elif not should and present:
        extra["allow_admin_from"] = [u for u in allow if u != user_id]
        removed = True
    return added, removed


def apply_add(
    extra: Dict[str, Any],
    user_id: str,
    role: str,
    name: Optional[str],
) -> MutationResult:
    """Add a brand-new user with ``role`` (and optional ``name``)."""
    _validate_role(extra, role)
    user_roles = _user_roles(extra)
    if user_id in user_roles:
        raise UsersError(
            f"user {user_id!r} already exists; use `update` to change it"
        )
    rbac_activated = len(user_roles) == 0
    user_roles[user_id] = role
    if name is not None:
        _user_names(extra)[user_id] = name
    added, removed = _sync_admin(extra, user_id, role)
    return MutationResult(
        rbac_activated=rbac_activated,
        admin_added=added,
        admin_removed=removed,
    )


def apply_update(
    extra: Dict[str, Any],
    user_id: str,
    role: Optional[str],
    name: Optional[str],
) -> MutationResult:
    """Update an existing user's ``role`` and/or ``name``."""
    user_roles = _user_roles(extra)
    if user_id not in user_roles:
        raise UsersError(f"user {user_id!r} does not exist; use `add` to create it")
    if role is None and name is None:
        raise UsersError("nothing to update; pass a role and/or --name")
    result = MutationResult()
    if role is not None:
        _validate_role(extra, role)
        user_roles[user_id] = role
        added, removed = _sync_admin(extra, user_id, role)
        result.admin_added = added
        result.admin_removed = removed
    if name is not None:
        _user_names(extra)[user_id] = name
    return result


def apply_delete(extra: Dict[str, Any], user_id: str) -> MutationResult:
    """Remove a user from ``user_roles``, ``user_names`` and ``allow_admin_from``."""
    user_roles = _user_roles(extra)
    if user_id not in user_roles:
        raise UsersError(f"user {user_id!r} does not exist")
    del user_roles[user_id]
    result = MutationResult()
    names = extra.get("user_names")
    if isinstance(names, dict) and user_id in names:
        del names[user_id]
    allow = extra.get("allow_admin_from")
    if isinstance(allow, list) and user_id in allow:
        extra["allow_admin_from"] = [u for u in allow if u != user_id]
        result.admin_removed = True
    result.rbac_deactivated = len(user_roles) == 0
    return result
