"""Pure mutation helpers for the ``hermes users`` CLI.

This module is the logic layer for managing Slack RBAC users that live under
the top-level ``slack:`` platform block in ``~/.hermes/config.yaml`` (the
gateway config loader bridges these keys into the platform's runtime ``extra``
— see ``gateway/config.py`` — so the canonical hand-edit location is directly
under ``slack:``, NOT under ``slack.extra``):

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

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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


def _canonical_role(extra: Dict[str, Any], role: str) -> str:
    """Validate ``role`` and return its canonical (lowercased) form.

    The gateway lowercases role names when resolving grants
    (``gateway.tool_access._coerce_roles``), so the value stored in
    ``user_roles`` MUST be lowercased or the role→toolset lookup misses and the
    user is denied. We therefore canonicalize on the way in. Matching is
    case-insensitive, so ``ADMIN`` and a config-defined ``Auditor`` both work.
    """
    canon = str(role).strip().lower()
    allowed = valid_roles(extra)
    if canon not in allowed:
        raise UsersError(
            f"unknown role {role!r}; valid roles: {', '.join(sorted(allowed))}"
        )
    return canon


def _as_map(val: Any, key: str) -> Optional[Dict[str, Any]]:
    """Validate that a config value is a mapping (or absent).

    Returns ``None`` when absent, the dict when it's a mapping, and raises
    :class:`UsersError` when it's some other type — so a hand-corrupted
    ``slack.<key>`` surfaces as a clean error instead of a traceback.
    """
    if val is None:
        return None
    if not isinstance(val, dict):
        raise UsersError(
            f"`slack.{key}` in config.yaml is not a mapping "
            f"({type(val).__name__}); fix it by hand before using `hermes users`."
        )
    return val


def _user_roles(extra: Dict[str, Any]) -> Dict[str, Any]:
    existing = _as_map(extra.get("user_roles"), "user_roles")
    if existing is None:
        existing = {}
        extra["user_roles"] = existing
    return existing


def _user_names(extra: Dict[str, Any]) -> Dict[str, Any]:
    existing = _as_map(extra.get("user_names"), "user_names")
    if existing is None:
        existing = {}
        extra["user_names"] = existing
    return existing


def _coerce_admin_list(raw: Any) -> List[str]:
    """Normalize an ``allow_admin_from`` value into an ordered, de-duped list.

    Hand-edited configs may store it as a list, a comma-separated string, or a
    bare scalar (mirroring ``gateway.slash_access._coerce_id_list``). Coercing
    to a list here keeps the mutation helpers from corrupting a CSV string
    (e.g. iterating its characters on removal).
    """
    if raw is None:
        items: List[Any] = []
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    elif isinstance(raw, str):
        items = [s for s in raw.split(",")]
    else:
        items = [raw]
    out: List[str] = []
    seen: Set[str] = set()
    for it in items:
        s = str(it).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _allow_admin_from(extra: Dict[str, Any]) -> List[str]:
    """Return ``allow_admin_from`` as a normalized list, writing it back in place.

    Always materializes a list so callers (admin-sync, delete) can mutate it
    safely regardless of how the value was stored in the YAML file.
    """
    coerced = _coerce_admin_list(extra.get("allow_admin_from"))
    extra["allow_admin_from"] = coerced
    return coerced


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
    role = _canonical_role(extra, role)
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
        role = _canonical_role(extra, role)
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
    if "allow_admin_from" in extra:
        allow = _allow_admin_from(extra)  # coerces CSV/scalar -> list in place
        if user_id in allow:
            extra["allow_admin_from"] = [u for u in allow if u != user_id]
            result.admin_removed = True
    result.rbac_deactivated = len(user_roles) == 0
    return result


# =============================================================================
# I/O layer — load config.yaml, mutate the top-level slack block, write back.
# =============================================================================
#
# Everything above is pure (mutates an in-memory dict). The helpers below add
# the file I/O and the argparse handlers. The pure helpers stay untouched: the
# handlers partially-apply them and hand them to ``_mutate_slack`` as a
# mutator callback. The mutated mapping is the ``slack:`` block itself, because
# the gateway reads user_roles/allow_admin_from/roles from there (top-level),
# bridging them into the runtime ``extra`` at load time.


_NO_SLACK_MSG = (
    "No `slack:` platform configured in {path}; "
    "add Slack config before managing users."
)


def _mutate_slack(mutator: Callable[[Dict[str, Any]], MutationResult]) -> MutationResult:
    """Round-trip ``config.yaml`` through ``mutator`` while preserving comments.

    Loads ``config.yaml`` with ruamel's round-trip loader, hands the top-level
    ``slack:`` block to ``mutator`` (one of the ``apply_*`` helpers, partially
    applied), then writes the whole document back atomically using the same
    temp-file + fsync + atomic-replace pattern as
    ``utils.atomic_roundtrip_yaml_update``.

    The mutated mapping is the ``slack:`` block itself (NOT ``slack.extra``):
    the gateway config loader bridges top-level ``slack.user_roles`` /
    ``allow_admin_from`` / ``roles`` into the platform's runtime ``extra``, so
    that is the canonical hand-edit location.

    Raises :class:`UsersError` if there is no ``slack:`` platform configured —
    we never fabricate a Slack block, because writing ``user_roles`` under a
    non-existent platform would silently do nothing useful (and could confuse
    the operator into thinking RBAC is active when no Slack platform reads it).
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

    from hermes_cli.config import get_config_path
    from utils import (
        atomic_replace,
        _preserve_file_mode,
        _restore_file_mode,
    )

    path = get_config_path()
    if not path.exists():
        raise UsersError(_NO_SLACK_MSG.format(path=path))

    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    yaml_rt.allow_unicode = True
    yaml_rt.default_flow_style = False
    yaml_rt.indent(mapping=2, sequence=4, offset=2)

    with path.open("r", encoding="utf-8") as f:
        config = yaml_rt.load(f) or CommentedMap()

    if (
        not isinstance(config, dict)
        or "slack" not in config
        or config.get("slack") is None
    ):
        raise UsersError(_NO_SLACK_MSG.format(path=path))

    slack = config["slack"]

    result = mutator(slack)

    original_mode = _preserve_file_mode(path)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml_rt.dump(config, f)
            f.flush()
            os.fsync(f.fileno())
        real_path = atomic_replace(tmp_path, path)
        _restore_file_mode(real_path, original_mode)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return result


def _read_slack() -> Dict[str, Any]:
    """Return the top-level ``slack:`` block from the on-disk config (or ``{}``)."""
    from hermes_cli.config import read_raw_config

    cfg = read_raw_config()
    return cfg.get("slack") or {}


# =============================================================================
# argparse handlers — take an args namespace, return an int exit code.
# Printing lives ONLY here; the pure helpers never print.
# =============================================================================


def handle_users_list(args) -> int:
    """List Slack RBAC users (table by default, JSON with ``--json``)."""
    extra = _read_slack()
    try:
        user_roles = _as_map(extra.get("user_roles"), "user_roles") or {}
        user_names = _as_map(extra.get("user_names"), "user_names") or {}
    except UsersError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    allow = _coerce_admin_list(extra.get("allow_admin_from"))
    allow_set = set(allow)
    rbac_active = bool(user_roles)

    rows = []
    for user_id in sorted(user_roles):
        rows.append(
            {
                "user_id": user_id,
                "name": user_names.get(user_id) or "",
                "role": user_roles.get(user_id) or "",
                "admin": user_id in allow_set,
            }
        )

    if getattr(args, "json", False):
        payload = {
            "rbac_active": rbac_active,
            "users": rows,
            "allow_admin_from": list(allow),
        }
        print(json.dumps(payload, indent=2))
        return 0

    if rbac_active:
        print(
            "Slack RBAC is ACTIVE — only listed users may use Slack."
        )
    else:
        print(
            "Slack RBAC is INACTIVE — no user_roles set; Slack access is open."
        )

    if not rows:
        print("No Slack users configured.")
        return 0

    id_w = max(len("USER_ID"), *(len(r["user_id"]) for r in rows))
    name_w = max(len("NAME"), *(len(r["name"]) for r in rows))
    role_w = max(len("ROLE"), *(len(r["role"]) for r in rows))
    header = (
        f"{'USER_ID':<{id_w}}  {'NAME':<{name_w}}  "
        f"{'ROLE':<{role_w}}  ADMIN"
    )
    print(header)
    for r in rows:
        admin_mark = "✓" if r["admin"] else ""
        print(
            f"{r['user_id']:<{id_w}}  {r['name']:<{name_w}}  "
            f"{r['role']:<{role_w}}  {admin_mark}"
        )
    return 0


def handle_users_add(args) -> int:
    """Add a new Slack RBAC user."""
    name = getattr(args, "name", None)
    try:
        result = _mutate_slack(
            lambda extra: apply_add(extra, args.user_id, args.role, name)
        )
    except UsersError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    detail = f"Added Slack user {args.user_id} with role {args.role.lower()}"
    if name:
        detail += f" (name: {name})"
    print(detail + ".")

    if result.rbac_activated:
        print(
            "\n⚠️  WARNING: Slack RBAC is now ACTIVE. Any Slack user NOT in "
            "user_roles will be DENIED access.\n"
            "    Add more users with: hermes users add <user_id> <role> [--name NAME]"
        )
    if result.admin_added:
        print(
            f"Granted slash-admin to {args.user_id} (added to allow_admin_from)."
        )
    return 0


def handle_users_update(args) -> int:
    """Update an existing Slack RBAC user's role and/or name."""
    role = getattr(args, "role", None)
    name = getattr(args, "name", None)
    try:
        result = _mutate_slack(
            lambda extra: apply_update(extra, args.user_id, role, name)
        )
    except UsersError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    parts = []
    if role is not None:
        parts.append(f"role={role.lower()}")
    if name is not None:
        parts.append(f"name={name}")
    print(f"Updated Slack user {args.user_id} ({', '.join(parts)}).")

    if result.admin_added:
        print(
            f"Granted slash-admin to {args.user_id} (added to allow_admin_from)."
        )
    if result.admin_removed:
        print(
            f"Revoked slash-admin from {args.user_id} "
            "(removed from allow_admin_from)."
        )
    return 0


def handle_users_delete(args) -> int:
    """Remove a Slack RBAC user from roles, names, and allow_admin_from."""
    try:
        result = _mutate_slack(
            lambda extra: apply_delete(extra, args.user_id)
        )
    except UsersError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Removed Slack user {args.user_id}.")

    if result.admin_removed:
        print(
            f"Revoked slash-admin from {args.user_id} "
            "(removed from allow_admin_from)."
        )
    if result.rbac_deactivated:
        print(
            "\nNotice: Slack RBAC is now INACTIVE — no users remain in "
            "user_roles, so Slack access is open again."
        )
    return 0


# =============================================================================
# argparse wiring — register the `users` command group on the top-level CLI.
# =============================================================================


def _exit_on_failure(handler: Callable[[Any], int]) -> Callable[[Any], Optional[int]]:
    """Wrap a ``handle_users_*`` handler so a non-zero rc becomes the exit code.

    ``hermes_cli.main.main`` dispatches via ``args.func(args)`` and IGNORES the
    return value (it does not ``sys.exit`` on it). Our handlers return an int
    exit code, so without this adapter ``hermes users add <dupe>`` would print
    its error but still exit 0. The adapter raises ``SystemExit(rc)`` when the
    handler returns a truthy (non-zero) rc, and returns the rc otherwise. This
    is scoped to the ``users`` group only — global dispatch behavior for every
    other command is unchanged.

    The wrapped handler is exposed via ``__wrapped_handler__`` so tests can
    assert each subparser routes to the genuine Task-B handler.
    """

    def _run(args) -> Optional[int]:
        rc = handler(args)
        if rc:
            raise SystemExit(rc)
        return rc

    _run.__wrapped_handler__ = handler  # type: ignore[attr-defined]
    return _run


def register_users_subcommands(subparsers) -> None:
    """Register the `users` command group on the top-level CLI subparsers."""
    users_parser = subparsers.add_parser(
        "users",
        help="Manage Slack RBAC users (roles, optional names, slash-admin sync)",
        description=(
            "Add, update, list, or delete Slack users in ~/.hermes/config.yaml "
            "(slack.extra.user_roles). Promoting a user to the 'admin' role also "
            "adds them to slack.extra.allow_admin_from; changing them to a "
            "non-admin role or deleting them removes that grant.\n\n"
            "Roles: admin, operator, readonly, chat_only (plus any custom roles "
            "defined under slack.extra.roles).\n\n"
            "NOTE: setting the first user activates Slack RBAC — after that, any "
            "Slack user NOT listed is denied."
        ),
    )
    users_sub = users_parser.add_subparsers(dest="users_action")

    p_list = users_sub.add_parser("list", help="List Slack users and their roles")
    p_list.add_argument("--json", action="store_true", default=False,
                        help="Print machine-readable JSON instead of a table")
    p_list.set_defaults(func=_exit_on_failure(handle_users_list))

    p_add = users_sub.add_parser("add", help="Add a Slack user with a role")
    p_add.add_argument("user_id", help="Slack user id, e.g. U0123ABCD")
    p_add.add_argument("role", help="admin | operator | readonly | chat_only | <custom>")
    p_add.add_argument("--name", default=None, help="Optional human name (for auditing)")
    p_add.set_defaults(func=_exit_on_failure(handle_users_add))

    p_update = users_sub.add_parser("update", help="Change a Slack user's role and/or name")
    p_update.add_argument("user_id")
    p_update.add_argument("role", nargs="?", default=None,
                          help="New role (omit to change only --name)")
    p_update.add_argument("--name", default=None, help="New human name")
    p_update.set_defaults(func=_exit_on_failure(handle_users_update))

    p_delete = users_sub.add_parser("delete", help="Remove a Slack user")
    p_delete.add_argument("user_id")
    p_delete.set_defaults(func=_exit_on_failure(handle_users_delete))

    # `hermes users` with no action -> print help, non-zero exit.
    def _users_help(_args) -> int:
        users_parser.print_help()
        return 1
    users_parser.set_defaults(func=_exit_on_failure(_users_help))
