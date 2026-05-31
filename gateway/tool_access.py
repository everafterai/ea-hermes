"""Per-user tool RBAC for messaging platforms.

Sits beside the chat allowlist and the slash-command tiers (see
``gateway/slash_access.py``) and adds a third axis: which *tools* an
identified platform user may invoke, expressed as named roles that grant
toolset categories.

Two config keys in a platform's ``extra`` block:

  - ``roles``       — optional map ``{role_name: {toolsets: [...]}}``. Merges
                      over the built-in defaults; a custom role with a
                      built-in name overrides it. ``"*"`` grants all toolsets;
                      ``[]`` grants none. Patterns containing ``*`` (e.g.
                      ``mcp-*``) are glob-matched against concrete toolsets.
  - ``user_roles``  — map ``{user_id: role_name}``. Its presence ACTIVATES
                      RBAC for the platform. When active it is the sole
                      authorization source: a user with a role may chat and
                      gets that role's toolsets; a user with no role is denied
                      entirely.

Backward compatibility: when ``user_roles`` is absent/empty, the policy is
disabled and every method defers (``is_authorized`` → True, ``allowed_toolsets``
→ everything). Existing installs are unaffected until an operator opts in.

Fail-closed: a ``user_roles`` entry naming an undefined role denies that user
and logs a config error.
"""

from __future__ import annotations

import fnmatch
import logging
import types
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Optional

logger = logging.getLogger(__name__)

# Built-in roles. Operators get these without writing a ``roles:`` block.
BUILTIN_ROLES: Dict[str, FrozenSet[str]] = {
    "admin": frozenset({"*"}),
    "operator": frozenset(
        {"terminal", "file", "web", "browser", "vision", "memory", "delegation"}
    ),
    "readonly": frozenset({"web", "vision", "session_search", "memory"}),
    "chat_only": frozenset(),
}


def _coerce_str(value: Any) -> str:
    return str(value).strip()


def _coerce_user_roles(raw: Any) -> Dict[str, str]:
    """Normalize ``{user_id: role_name}`` from YAML (int ids, padding, case)."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        uid = _coerce_str(k)
        role = _coerce_str(v).lower()
        if uid and role:
            out[uid] = role
    return out


def _coerce_roles(raw: Any) -> Dict[str, FrozenSet[str]]:
    """Normalize a ``roles`` block, merged over the built-in defaults."""
    resolved: Dict[str, FrozenSet[str]] = dict(BUILTIN_ROLES)
    if not isinstance(raw, dict):
        return resolved
    for name, body in raw.items():
        role_name = _coerce_str(name).lower()
        if not role_name:
            continue
        toolsets: Any = None
        if isinstance(body, dict):
            toolsets = body.get("toolsets")
        elif isinstance(body, (list, tuple)):
            toolsets = body
        if isinstance(toolsets, str):
            items = [s for s in (s.strip() for s in toolsets.split(",")) if s]
        elif isinstance(toolsets, (list, tuple, set, frozenset)):
            items = toolsets
        elif toolsets is not None:
            logger.warning(
                "tool_access: unexpected toolsets type %s for role '%s' — ignoring",
                type(toolsets).__name__, role_name,
            )
            items = []
        else:
            items = []
        resolved[role_name] = frozenset(
            _coerce_str(t).lower() for t in items if _coerce_str(t)
        )
    return resolved


def _granted(role_toolsets: FrozenSet[str], toolset: str) -> bool:
    """True if ``toolset`` is granted by ``role_toolsets`` (exact, ``*``, glob)."""
    toolset = toolset.lower()
    if "*" in role_toolsets:
        return True
    if toolset in role_toolsets:
        return True
    for pattern in role_toolsets:
        if "*" in pattern and fnmatch.fnmatchcase(toolset, pattern):
            return True
    return False


@dataclass(frozen=True)
class ToolAccessPolicy:
    """Resolved RBAC policy for a single platform.

    When ``enabled`` is False the policy defers entirely so legacy auth and
    the unfiltered toolset apply unchanged.
    """

    enabled: bool
    user_roles: Mapping[str, str]
    roles: Mapping[str, FrozenSet[str]]

    def role_for(self, user_id: Optional[str]) -> Optional[str]:
        if not self.enabled or not user_id:
            return None
        return self.user_roles.get(str(user_id))

    def _resolved_grant(self, user_id: Optional[str]) -> Optional[FrozenSet[str]]:
        """Return the grant set for *user_id*, or None if access should be denied.

        Logs an ERROR once when the user's assigned role name is not defined in
        ``self.roles``, so every entry point gets consistent logging.
        """
        role = self.role_for(user_id)
        if role is None:
            return None
        if role not in self.roles:
            logger.error(
                "tool_access: user %s assigned undefined role '%s' — denying",
                user_id, role,
            )
            return None
        return self.roles[role]

    def is_authorized(self, user_id: Optional[str]) -> bool:
        if not self.enabled:
            return True  # defer to legacy auth
        return self._resolved_grant(user_id) is not None

    def allowed_toolsets(
        self, user_id: Optional[str], all_toolsets: FrozenSet[str]
    ) -> FrozenSet[str]:
        if not self.enabled:
            return frozenset(all_toolsets)
        grant = self._resolved_grant(user_id)
        if grant is None:
            return frozenset()
        return frozenset(t for t in all_toolsets if _granted(grant, t))

    def can_use_tool(
        self, user_id: Optional[str], toolset: Optional[str]
    ) -> bool:
        if not self.enabled:
            return True
        grant = self._resolved_grant(user_id)
        if grant is None or not toolset:
            return False
        return _granted(grant, toolset)


def policy_from_extra(extra: Any) -> ToolAccessPolicy:
    """Build a policy from a platform's ``extra`` dict."""
    if not isinstance(extra, dict):
        extra = {}
    user_roles = _coerce_user_roles(extra.get("user_roles"))
    roles = _coerce_roles(extra.get("roles"))
    return ToolAccessPolicy(
        enabled=bool(user_roles),
        user_roles=types.MappingProxyType(user_roles),
        roles=types.MappingProxyType(roles),
    )


def _platform_extra(platform_config: Any) -> dict:
    if platform_config is None:
        return {}
    extra = getattr(platform_config, "extra", None)
    if isinstance(extra, dict):
        return extra
    # Some test harnesses pass dicts directly.
    if isinstance(platform_config, dict):
        return platform_config
    return {}


def policy_for_source(gateway_config: Any, source: Any) -> ToolAccessPolicy:
    """Resolve the policy for a SessionSource's platform."""
    if gateway_config is None or source is None:
        return ToolAccessPolicy(
            enabled=False,
            user_roles=types.MappingProxyType({}),
            roles=types.MappingProxyType(dict(BUILTIN_ROLES)),
        )
    platforms = getattr(gateway_config, "platforms", None)
    platform_config = None
    if platforms is not None:
        try:
            platform_config = platforms.get(source.platform)
        except Exception:
            platform_config = None
    return policy_from_extra(_platform_extra(platform_config))


__all__ = [
    "BUILTIN_ROLES",
    "ToolAccessPolicy",
    "policy_from_extra",
    "policy_for_source",
]
