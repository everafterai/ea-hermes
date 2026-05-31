# Slack Tool RBAC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add role-based access control so each Slack user is assigned a named role that determines which tool categories the agent may use on their behalf, with users denied entirely until assigned.

**Architecture:** A pure, unit-testable `ToolAccessPolicy` module (`gateway/tool_access.py`) mirroring the existing `gateway/slash_access.py`, wired into three enforcement points: the auth gate (`_is_user_authorized`), the per-run toolset assembly (`enabled_toolsets`), and the tool-dispatch backstop (`handle_function_call`). Config lives in YAML under the Slack platform's `extra:` block and becomes the single source of truth for authorization when present, retiring `SLACK_ALLOWED_USERS`.

**Tech Stack:** Python 3, dataclasses, `fnmatch` for glob matching, `pytest`. No new dependencies.

**Design spec:** `docs/superpowers/specs/2026-05-31-slack-tool-rbac-design.md`

---

## File Structure

| File | Responsibility |
| --- | --- |
| `gateway/tool_access.py` (new) | Pure policy: parse roles/user_roles from `extra`, resolve per source, answer `is_authorized` / `allowed_toolsets` / `can_use_tool`. Plus a contextvar-based denial helper for the dispatch backstop. |
| `tests/gateway/test_tool_access.py` (new) | Unit tests for the pure policy. |
| `tests/gateway/test_tool_access_enforcement.py` (new) | Integration tests for the three enforcement points. |
| `gateway/config.py` (modify ~849-862) | Bridge `roles` / `user_roles` YAML keys into `platform.extra`. |
| `model_tools.py` (modify ~784) | Dispatch backstop: deny forbidden tools using the contextvar helper. |
| `gateway/run.py` (modify `_is_user_authorized` ~6391-6607; toolset assembly ~11722, ~15845) | Auth gate by role presence; filter `enabled_toolsets` by role; suppress pairing under active RBAC. |
| `gateway/platforms/api_server.py` (modify ~989) | Apply the same `enabled_toolsets` filter at this assembly site. |
| `website/docs/user-guide/messaging/slack.md` (modify) | Document the RBAC config. |

Toolset names referenced (real registry values): `terminal`, `file`, `web`, `browser`, `browser-cdp`, `vision`, `memory`, `delegation`, `code_execution`, `image_gen`, `session_search`, `skills`, `mcp-<server>`.

---

## Task 1: Pure policy module `gateway/tool_access.py`

**Files:**
- Create: `gateway/tool_access.py`
- Test: `tests/gateway/test_tool_access.py`

- [ ] **Step 1: Write failing tests for built-in defaults and parsing**

Create `tests/gateway/test_tool_access.py`:

```python
"""Unit tests for gateway.tool_access — per-user tool RBAC.

Tests the pure policy resolver (no gateway plumbing). Integration tests that
exercise the enforcement sites live in test_tool_access_enforcement.py.
"""
from __future__ import annotations

from gateway.tool_access import (
    BUILTIN_ROLES,
    ToolAccessPolicy,
    policy_from_extra,
    policy_for_source,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


ALL_TOOLSETS = frozenset(
    {"terminal", "file", "web", "browser", "vision", "memory",
     "delegation", "session_search", "mcp-github", "mcp-jira"}
)
TOOL_MAP = {
    "run_shell": "terminal",
    "write_file": "file",
    "web_search": "web",
    "describe_image": "vision",
    "gh_issue": "mcp-github",
}


class TestPolicyFromExtra:
    def test_empty_extra_is_disabled(self):
        p = policy_from_extra({})
        assert p.enabled is False

    def test_disabled_policy_authorizes_anyone(self):
        # When RBAC is off, callers fall back to legacy auth; the policy
        # must not deny. is_authorized short-circuits True so the gate defers.
        p = policy_from_extra({})
        assert p.is_authorized("U_ANYONE") is True
        assert p.allowed_toolsets("U_ANYONE", ALL_TOOLSETS) == ALL_TOOLSETS

    def test_user_roles_activates_policy(self):
        p = policy_from_extra({"user_roles": {"U_A": "admin"}})
        assert p.enabled is True

    def test_builtin_roles_available_without_roles_block(self):
        p = policy_from_extra({"user_roles": {"U_A": "readonly"}})
        assert p.role_for("U_A") == "readonly"
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset(BUILTIN_ROLES["readonly"]) & ALL_TOOLSETS

    def test_id_and_role_coercion(self):
        # YAML may load int IDs and pad whitespace.
        p = policy_from_extra({"user_roles": {123: " admin ", "U_B ": "Operator"}})
        assert p.role_for("123") == "admin"
        assert p.role_for("U_B") == "operator"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/gateway/test_tool_access.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gateway.tool_access'`

- [ ] **Step 3: Implement the module**

Create `gateway/tool_access.py`:

```python
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
        items = toolsets if isinstance(toolsets, (list, tuple, set, frozenset)) else []
        resolved[role_name] = frozenset(
            _coerce_str(t).lower() for t in items if _coerce_str(t)
        )
    return resolved


def _granted(role_toolsets: FrozenSet[str], toolset: str) -> bool:
    """True if ``toolset`` is granted by ``role_toolsets`` (exact, ``*``, glob)."""
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

    def is_authorized(self, user_id: Optional[str]) -> bool:
        if not self.enabled:
            return True  # defer to legacy auth
        role = self.role_for(user_id)
        if role is None:
            return False
        if role not in self.roles:
            logger.error(
                "tool_access: user %s assigned undefined role '%s' — denying",
                user_id, role,
            )
            return False
        return True

    def allowed_toolsets(
        self, user_id: Optional[str], all_toolsets: FrozenSet[str]
    ) -> FrozenSet[str]:
        if not self.enabled:
            return frozenset(all_toolsets)
        role = self.role_for(user_id)
        if role is None or role not in self.roles:
            return frozenset()
        grant = self.roles[role]
        return frozenset(t for t in all_toolsets if _granted(grant, t))

    def can_use_tool(
        self, user_id: Optional[str], toolset: Optional[str]
    ) -> bool:
        if not self.enabled:
            return True
        role = self.role_for(user_id)
        if role is None or role not in self.roles:
            return False
        if not toolset:
            return False
        return _granted(self.roles[role], toolset)


def policy_from_extra(extra: Any) -> ToolAccessPolicy:
    """Build a policy from a platform's ``extra`` dict."""
    if not isinstance(extra, dict):
        extra = {}
    user_roles = _coerce_user_roles(extra.get("user_roles"))
    roles = _coerce_roles(extra.get("roles"))
    return ToolAccessPolicy(
        enabled=bool(user_roles),
        user_roles=user_roles,
        roles=roles,
    )


def _platform_extra(platform_config: Any) -> dict:
    if platform_config is None:
        return {}
    extra = getattr(platform_config, "extra", None)
    if isinstance(extra, dict):
        return extra
    if isinstance(platform_config, dict):
        return platform_config
    return {}


def policy_for_source(gateway_config: Any, source: Any) -> ToolAccessPolicy:
    """Resolve the policy for a SessionSource's platform."""
    if gateway_config is None or source is None:
        return ToolAccessPolicy(enabled=False, user_roles={}, roles=dict(BUILTIN_ROLES))
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/gateway/test_tool_access.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Add tests for toolset resolution, wildcard, glob, and fail-closed**

Append to `tests/gateway/test_tool_access.py`:

```python
class TestToolsetResolution:
    def test_admin_wildcard_grants_everything(self):
        p = policy_from_extra({"user_roles": {"U_A": "admin"}})
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == ALL_TOOLSETS
        assert p.can_use_tool("U_A", "terminal") is True

    def test_chat_only_grants_nothing(self):
        p = policy_from_extra({"user_roles": {"U_A": "chat_only"}})
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset()
        assert p.can_use_tool("U_A", "terminal") is False
        assert p.is_authorized("U_A") is True  # may still chat

    def test_explicit_toolset_list(self):
        p = policy_from_extra(
            {"roles": {"limited": {"toolsets": ["web", "vision"]}},
             "user_roles": {"U_A": "limited"}}
        )
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset({"web", "vision"})
        assert p.can_use_tool("U_A", "web") is True
        assert p.can_use_tool("U_A", "terminal") is False

    def test_mcp_glob(self):
        p = policy_from_extra(
            {"roles": {"mcpuser": {"toolsets": ["mcp-*"]}},
             "user_roles": {"U_A": "mcpuser"}}
        )
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset({"mcp-github", "mcp-jira"})
        assert p.can_use_tool("U_A", "mcp-github") is True
        assert p.can_use_tool("U_A", "terminal") is False

    def test_custom_role_overrides_builtin(self):
        p = policy_from_extra(
            {"roles": {"readonly": {"toolsets": ["web"]}},
             "user_roles": {"U_A": "readonly"}}
        )
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset({"web"})


class TestFailClosed:
    def test_unassigned_user_denied(self):
        p = policy_from_extra({"user_roles": {"U_A": "admin"}})
        assert p.is_authorized("U_STRANGER") is False
        assert p.allowed_toolsets("U_STRANGER", ALL_TOOLSETS) == frozenset()
        assert p.can_use_tool("U_STRANGER", "web") is False

    def test_undefined_role_denied(self):
        p = policy_from_extra({"user_roles": {"U_A": "ghost"}})
        assert p.is_authorized("U_A") is False
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset()


class TestPolicyForSource:
    def test_resolves_slack_extra(self):
        cfg = GatewayConfig()
        cfg.platforms[Platform.SLACK] = PlatformConfig(
            extra={"user_roles": {"U_A": "operator"}}
        )
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", user_id="U_A")
        p = policy_for_source(cfg, src)
        assert p.enabled is True
        assert p.role_for("U_A") == "operator"

    def test_missing_platform_is_disabled(self):
        cfg = GatewayConfig()
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", user_id="U_A")
        assert policy_for_source(cfg, src).enabled is False
```

> Note: confirm the `SessionSource(...)` keyword arguments against `gateway/session.py` before running; adjust if the constructor differs (e.g. requires `chat_type`).

- [ ] **Step 6: Run the full unit suite to verify it passes**

Run: `pytest tests/gateway/test_tool_access.py -v`
Expected: PASS (all tests)

- [ ] **Step 7: Commit**

```bash
git add gateway/tool_access.py tests/gateway/test_tool_access.py
git commit -m "feat(tool-access): pure RBAC policy module with built-in roles

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Bridge `roles` / `user_roles` YAML keys into `platform.extra`

The shared key-bridge in `gateway/config.py` copies platform-config keys into `extra` so `policy_for_source` can read them (same mechanism `allow_admin_from` uses). We add our two keys.

**Files:**
- Modify: `gateway/config.py` (~853-854)
- Test: `tests/gateway/test_tool_access.py` (add a config-loading check)

- [ ] **Step 1: Write the failing test**

Append to `tests/gateway/test_tool_access.py`:

```python
class TestConfigBridge:
    def test_roles_and_user_roles_bridged_to_extra(self):
        from gateway.config import GatewayConfig

        yaml_cfg = {
            "platforms": {
                "slack": {
                    "enabled": True,
                    "roles": {"limited": {"toolsets": ["web"]}},
                    "user_roles": {"U_A": "limited"},
                }
            }
        }
        # _bridge_shared_platform_keys is the helper that copies recognized
        # platform keys into platforms_data[...]["extra"].
        from gateway.config import _bridge_shared_platform_keys
        platforms_data = {}
        _bridge_shared_platform_keys(yaml_cfg, platforms_data)
        extra = platforms_data["slack"]["extra"]
        assert extra["roles"] == {"limited": {"toolsets": ["web"]}}
        assert extra["user_roles"] == {"U_A": "limited"}
```

> Note: the bridge logic at `gateway/config.py:~830-881` is currently inline inside `load_gateway_config`, not a named helper. If no `_bridge_shared_platform_keys` exists, instead assert end-to-end via `load_gateway_config` with a temp `config.yaml`, OR (preferred) extract the inline bridge loop into a `_bridge_shared_platform_keys(yaml_cfg, platforms_data)` function as part of this task and call it from `load_gateway_config`. Extraction keeps the test small and the function focused.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/gateway/test_tool_access.py::TestConfigBridge -v`
Expected: FAIL (keys absent from `extra`, or helper missing)

- [ ] **Step 3: Add the two keys to the bridge**

In `gateway/config.py`, in the shared-key bridge block (immediately after the `user_allowed_commands` copy at ~853-854), add:

```python
                if "roles" in platform_cfg:
                    bridged["roles"] = platform_cfg["roles"]
                if "user_roles" in platform_cfg:
                    bridged["user_roles"] = platform_cfg["user_roles"]
```

(If you extracted `_bridge_shared_platform_keys` per the Step 1 note, make the same addition inside that function.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/gateway/test_tool_access.py::TestConfigBridge -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/config.py tests/gateway/test_tool_access.py
git commit -m "feat(tool-access): bridge roles/user_roles YAML keys into platform extra

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Dispatch backstop in `handle_function_call`

Add a contextvar-driven denial helper to `tool_access`, then call it in `model_tools.handle_function_call` right after the `pre_tool_call` block check. This is the security backstop that fires regardless of `skip_pre_tool_call_hook`, covering delegated sub-tasks and the sandbox.

**Files:**
- Modify: `gateway/tool_access.py` (add helper + cached config loader)
- Modify: `model_tools.py` (~800, after the pre_tool_call block)
- Test: `tests/gateway/test_tool_access_enforcement.py` (new)

- [ ] **Step 1: Write the failing test for the contextvar helper**

Create `tests/gateway/test_tool_access_enforcement.py`:

```python
"""Integration tests for tool RBAC enforcement points."""
from __future__ import annotations

import pytest

from gateway.tool_access import denial_for_current_tool


class _FakePolicy:
    enabled = True

    def can_use_tool(self, user_id, toolset):
        return toolset == "web"


@pytest.fixture
def patched(monkeypatch):
    """Patch identity + policy + tool→toolset lookup used by the helper."""
    monkeypatch.setattr(
        "gateway.tool_access._current_identity",
        lambda: ("U_A", "slack"),
    )
    monkeypatch.setattr(
        "gateway.tool_access._policy_for_current_platform",
        lambda platform: _FakePolicy(),
    )
    monkeypatch.setattr(
        "gateway.tool_access._toolset_for_tool",
        lambda name: {"web_search": "web", "run_shell": "terminal"}.get(name),
    )


def test_allows_permitted_tool(patched):
    assert denial_for_current_tool("web_search") is None


def test_denies_forbidden_tool(patched):
    msg = denial_for_current_tool("run_shell")
    assert msg is not None
    assert "run_shell" in msg


def test_no_identity_allows(monkeypatch):
    # CLI / system context: no user → no gating.
    monkeypatch.setattr(
        "gateway.tool_access._current_identity", lambda: (None, None)
    )
    assert denial_for_current_tool("run_shell") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/gateway/test_tool_access_enforcement.py -v`
Expected: FAIL with `ImportError: cannot import name 'denial_for_current_tool'`

- [ ] **Step 3: Implement the helper in `gateway/tool_access.py`**

Append to `gateway/tool_access.py` (and add `import os`, `import time` at the top):

```python
# ---------------------------------------------------------------------------
# Dispatch-backstop helper. Reads identity from session contextvars, resolves
# the policy for that platform (cached on config mtime), maps the tool to its
# toolset via the registry, and returns a denial message or None.
# ---------------------------------------------------------------------------

_PLATFORM_BY_NAME: Dict[str, Any] = {}
_config_cache: Dict[str, Any] = {"fp": None, "config": None}


def _current_identity():
    """Return (user_id, platform_name) from session contextvars, or (None, None)."""
    try:
        from gateway.session_context import get_session_env
        uid = get_session_env("HERMES_SESSION_USER_ID", "") or None
        plat = get_session_env("HERMES_SESSION_PLATFORM", "") or None
        return uid, plat
    except Exception:
        return None, None


def _toolset_for_tool(tool_name: str) -> Optional[str]:
    try:
        from tools.registry import registry
        return registry.get_toolset_for_tool(tool_name)
    except Exception:
        return None


def _load_config_cached():
    """Load gateway config, memoized on config.yaml mtime."""
    try:
        from gateway.config import load_gateway_config, get_hermes_home
        cfg_file = get_hermes_home() / "config.yaml"
        try:
            st = cfg_file.stat()
            fp = (st.st_mtime_ns, st.st_size)
        except OSError:
            fp = None
        if fp != _config_cache["fp"] or _config_cache["config"] is None:
            _config_cache["config"] = load_gateway_config()
            _config_cache["fp"] = fp
        return _config_cache["config"]
    except Exception:
        return None


def _policy_for_current_platform(platform_name: str) -> Optional[ToolAccessPolicy]:
    config = _load_config_cached()
    if config is None:
        return None
    try:
        from gateway.config import Platform
        platform = Platform(platform_name)
    except Exception:
        return None
    platforms = getattr(config, "platforms", {}) or {}
    return policy_from_extra(_platform_extra(platforms.get(platform)))


def denial_for_current_tool(tool_name: str) -> Optional[str]:
    """Return a denial message if the current user may not use ``tool_name``,
    else None. Fail-open on any internal error (RBAC is a backstop; the
    toolset filter is the primary control)."""
    try:
        user_id, platform_name = _current_identity()
        if not user_id or not platform_name:
            return None  # CLI / system / cron context — no gating
        policy = _policy_for_current_platform(platform_name)
        if policy is None or not policy.enabled:
            return None
        toolset = _toolset_for_tool(tool_name)
        if policy.can_use_tool(user_id, toolset):
            return None
        logger.info(
            "tool_access: denied tool '%s' (toolset '%s') for %s on %s",
            tool_name, toolset, user_id, platform_name,
        )
        return (
            f"⛔ You are not permitted to use '{tool_name}' here. "
            "Ask an admin to adjust your role if you need this capability."
        )
    except Exception as err:  # pragma: no cover - defensive
        logger.debug("tool_access backstop error: %s", err)
        return None
```

Add `denial_for_current_tool` to `__all__`.

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `pytest tests/gateway/test_tool_access_enforcement.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Wire the backstop into `model_tools.handle_function_call`**

In `model_tools.py`, immediately after the `pre_tool_call` block check returns (after line ~799, before the ACP edit-approval block at ~801), insert:

```python
        # RBAC backstop: enforce per-user tool access even on paths that skip
        # the pre_tool_call hook (delegated sub-tasks, sandbox). Fail-open.
        try:
            from gateway.tool_access import denial_for_current_tool
            rbac_denial = denial_for_current_tool(function_name)
            if rbac_denial is not None:
                return json.dumps({"error": rbac_denial}, ensure_ascii=False)
        except Exception as _rbac_err:
            logger.debug("tool_access backstop import error: %s", _rbac_err)
```

- [ ] **Step 6: Add an end-to-end backstop test through `handle_function_call`**

Append to `tests/gateway/test_tool_access_enforcement.py`:

```python
def test_handle_function_call_blocks_forbidden_tool(monkeypatch):
    import model_tools

    monkeypatch.setattr(
        "gateway.tool_access._current_identity", lambda: ("U_A", "slack")
    )
    monkeypatch.setattr(
        "gateway.tool_access._policy_for_current_platform",
        lambda platform: _FakePolicy(),
    )
    monkeypatch.setattr(
        "gateway.tool_access._toolset_for_tool",
        lambda name: {"run_shell": "terminal"}.get(name),
    )
    # Skip the plugin hook so only the RBAC backstop is exercised.
    out = model_tools.handle_function_call(
        "run_shell", {"command": "ls"}, skip_pre_tool_call_hook=True
    )
    assert "not permitted" in out
```

- [ ] **Step 7: Run the enforcement suite to verify it passes**

Run: `pytest tests/gateway/test_tool_access_enforcement.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
git add gateway/tool_access.py model_tools.py tests/gateway/test_tool_access_enforcement.py
git commit -m "feat(tool-access): dispatch backstop blocks forbidden tools by role

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Toolset filter at per-run assembly

Hide forbidden toolsets from the model. Add a gateway helper that intersects the configured `enabled_toolsets` with the user's role grant, and apply it at each assembly site.

**Files:**
- Modify: `gateway/run.py` (add `_apply_rbac_toolset_filter` method; call at ~11722 and ~15845)
- Modify: `gateway/platforms/api_server.py` (~989)
- Test: `tests/gateway/test_tool_access_enforcement.py`

- [ ] **Step 1: Write the failing test for the filter helper**

Append to `tests/gateway/test_tool_access_enforcement.py`:

```python
from gateway.tool_access import filter_enabled_toolsets


class _RolePolicy:
    enabled = True

    def allowed_toolsets(self, user_id, all_toolsets):
        return frozenset({"web", "vision"}) & frozenset(all_toolsets)


def test_filter_intersects_with_role(monkeypatch):
    monkeypatch.setattr(
        "gateway.tool_access.policy_for_source",
        lambda cfg, src: _RolePolicy(),
    )
    result = filter_enabled_toolsets(
        gateway_config=object(),
        source=object(),
        enabled_toolsets=["web", "vision", "terminal", "file"],
    )
    assert sorted(result) == ["vision", "web"]


def test_filter_noop_when_disabled(monkeypatch):
    class _Disabled:
        enabled = False
    monkeypatch.setattr(
        "gateway.tool_access.policy_for_source",
        lambda cfg, src: _Disabled(),
    )
    result = filter_enabled_toolsets(
        gateway_config=object(),
        source=object(),
        enabled_toolsets=["web", "terminal"],
    )
    assert sorted(result) == ["terminal", "web"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/gateway/test_tool_access_enforcement.py::test_filter_intersects_with_role -v`
Expected: FAIL with `ImportError: cannot import name 'filter_enabled_toolsets'`

- [ ] **Step 3: Implement `filter_enabled_toolsets` in `gateway/tool_access.py`**

Append to `gateway/tool_access.py`:

```python
def filter_enabled_toolsets(gateway_config, source, enabled_toolsets):
    """Intersect ``enabled_toolsets`` with the source user's role grant.

    Returns the input unchanged when RBAC is disabled for the platform.
    The result is a sorted list (stable for the get_tool_definitions cache key).
    """
    base = list(enabled_toolsets or [])
    try:
        policy = policy_for_source(gateway_config, source)
        if not policy.enabled:
            return base
        user_id = getattr(source, "user_id", None)
        allowed = policy.allowed_toolsets(user_id, frozenset(base))
        return sorted(allowed)
    except Exception as err:  # pragma: no cover - defensive
        logger.debug("tool_access filter error: %s", err)
        return base
```

Add `filter_enabled_toolsets` to `__all__`.

- [ ] **Step 4: Run the filter tests to verify they pass**

Run: `pytest tests/gateway/test_tool_access_enforcement.py -k filter -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Apply the filter at the gateway assembly sites**

In `gateway/run.py`, at the background-task assembly (~11722) immediately after:

```python
            enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
```

insert:

```python
            from gateway.tool_access import filter_enabled_toolsets
            enabled_toolsets = filter_enabled_toolsets(
                gateway_config=_to_gateway_config(user_config),
                source=source,
                enabled_toolsets=enabled_toolsets,
            )
```

Apply the identical insertion at the second assembly site (~15845, same `enabled_toolsets = sorted(_get_platform_tools(...))` line).

> Note on `_to_gateway_config`: `_get_platform_tools` takes the dict-shaped `user_config`, but `policy_for_source` expects a `GatewayConfig` with `.platforms`. Use the gateway's existing typed config accessor — search `gateway/run.py` for where `load_gateway_config()` (the typed loader, distinct from the dict `_load_gateway_config()`) is already available on `self`, and pass that instead. If the typed config isn't readily on hand at the site, call `gateway.tool_access._load_config_cached()` to obtain it. Resolve this concretely during implementation; do not leave a placeholder.

- [ ] **Step 6: Apply the filter at the api_server site**

In `gateway/platforms/api_server.py` (~989), after:

```python
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))
```

insert the same `filter_enabled_toolsets(...)` call, passing the typed config and the request's `source`.

- [ ] **Step 7: Run the full enforcement + unit suites**

Run: `pytest tests/gateway/test_tool_access.py tests/gateway/test_tool_access_enforcement.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add gateway/tool_access.py gateway/run.py gateway/platforms/api_server.py tests/gateway/test_tool_access_enforcement.py
git commit -m "feat(tool-access): filter per-run toolset by user role

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Auth gate by role presence (retire `SLACK_ALLOWED_USERS`)

Make role presence the authorization decision when RBAC is active, ahead of the env-allowlist logic, with allow-all ignored and a one-time info log.

**Files:**
- Modify: `gateway/run.py` (`_is_user_authorized`, before the `platform_allow_all` check at ~6505)
- Test: `tests/gateway/test_tool_access_enforcement.py`

- [ ] **Step 1: Write the failing tests for the gate**

Append to `tests/gateway/test_tool_access_enforcement.py`:

```python
class TestAuthGate:
    def _gateway(self):
        # Build a minimal gateway instance exposing _is_user_authorized.
        # Reuse the project's existing gateway test fixture/factory if present
        # (search tests/gateway/ for how other tests construct the gateway);
        # otherwise instantiate the class under test directly with stubs.
        from gateway.run import HermesGateway  # confirm class name
        return HermesGateway.__new__(HermesGateway)

    def test_assigned_user_authorized(self, monkeypatch):
        from gateway.config import Platform, PlatformConfig, GatewayConfig
        from gateway.session import SessionSource

        gw = self._gateway()
        cfg = GatewayConfig()
        cfg.platforms[Platform.SLACK] = PlatformConfig(
            extra={"user_roles": {"U_A": "operator"}}
        )
        monkeypatch.setattr(gw, "_load_typed_config", lambda: cfg, raising=False)
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", user_id="U_A")
        assert gw._is_user_authorized(src) is True

    def test_roleless_user_denied(self, monkeypatch):
        from gateway.config import Platform, PlatformConfig, GatewayConfig
        from gateway.session import SessionSource

        gw = self._gateway()
        cfg = GatewayConfig()
        cfg.platforms[Platform.SLACK] = PlatformConfig(
            extra={"user_roles": {"U_A": "operator"}}
        )
        monkeypatch.setattr(gw, "_load_typed_config", lambda: cfg, raising=False)
        # Even if the legacy env allowlist would admit them:
        monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_STRANGER")
        monkeypatch.setenv("SLACK_ALLOW_ALL_USERS", "true")
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", user_id="U_STRANGER")
        assert gw._is_user_authorized(src) is False
```

> Note: align `HermesGateway`, `_load_typed_config`, and `SessionSource(...)` kwargs with the real code before running. The point of the test is the behavior: RBAC-active → role presence decides, overriding env allowlist and allow-all.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/gateway/test_tool_access_enforcement.py::TestAuthGate -v`
Expected: FAIL (roleless user still admitted by env/allow-all)

- [ ] **Step 3: Insert the RBAC branch into `_is_user_authorized`**

In `gateway/run.py:_is_user_authorized`, after `user_id` is known and before the per-platform allow-all check (~6505, after the bot/pairing handling but ahead of the env-allowlist logic), insert:

```python
        # RBAC: when tool-access roles are configured for this platform, role
        # assignment is the sole authorization source. A user with a role may
        # interact; a user with no role is denied — overriding the env
        # allowlist and any allow-all flag.
        try:
            from gateway.tool_access import policy_for_source
            _typed_cfg = self._load_typed_config()  # typed GatewayConfig
            _rbac = policy_for_source(_typed_cfg, source)
            if _rbac.enabled:
                if os.getenv(platform_env_map.get(source.platform, ""), "").strip():
                    if not getattr(self, "_warned_rbac_overrides_env", False):
                        logger.info(
                            "tool_access: RBAC roles are active for %s; "
                            "%s is ignored.",
                            source.platform.value if source.platform else "?",
                            platform_env_map.get(source.platform, ""),
                        )
                        self._warned_rbac_overrides_env = True
                return _rbac.is_authorized(user_id)
        except Exception as _rbac_err:
            logger.debug("tool_access auth-gate error: %s", _rbac_err)
```

> Note: `self._load_typed_config()` is shorthand for however the gateway obtains a typed `GatewayConfig` at this point. Search `gateway/run.py` for an existing typed-config accessor on the gateway (the typed `load_gateway_config()` import is already used elsewhere in this file). Use the real accessor; if none exists on `self`, call `gateway.tool_access._load_config_cached()`. Keep the branch above the allow-all check at ~6505 so RBAC wins.

- [ ] **Step 4: Run the gate tests to verify they pass**

Run: `pytest tests/gateway/test_tool_access_enforcement.py::TestAuthGate -v`
Expected: PASS

- [ ] **Step 5: Suppress the DM pairing offer under active RBAC**

Find the unauthorized-DM handling that offers a pairing code (search `gateway/run.py` for the pairing-offer call near `_get_unauthorized_dm_behavior` at ~6609). Guard it so that when RBAC is active for the platform, it sends the plain "ask an admin to assign you a role" message instead of a pairing code. Add a focused test asserting that with RBAC active, an unauthorized DM does not produce a pairing offer (assert the pairing-store `create`/`offer` method is not called, e.g. via `monkeypatch`/mock).

```python
    def test_pairing_suppressed_under_rbac(self, monkeypatch):
        # With RBAC active, an unauthorized DM gets the plain denial message,
        # not a pairing code. Mock the pairing-offer call and assert it is
        # never invoked; assert the returned/sent text mentions an admin.
        ...  # construct gateway + source as above; align with real APIs
```

- [ ] **Step 6: Run the full enforcement suite**

Run: `pytest tests/gateway/test_tool_access_enforcement.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gateway/run.py tests/gateway/test_tool_access_enforcement.py
git commit -m "feat(tool-access): role presence is the Slack auth gate; suppress pairing under RBAC

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Delegation propagation test, docs, and regression sweep

**Files:**
- Test: `tests/gateway/test_tool_access_enforcement.py`
- Modify: `website/docs/user-guide/messaging/slack.md`

- [ ] **Step 1: Write the delegation-propagation test**

Append to `tests/gateway/test_tool_access_enforcement.py` a test proving the backstop fires inside a delegated sub-task — i.e. that session contextvars (`HERMES_SESSION_USER_ID` / `HERMES_SESSION_PLATFORM`) are set when a sub-agent dispatches a tool. Use the real `set_session_vars` to set identity, configure a Slack policy via the cached-config path (monkeypatch `_load_config_cached` to return a `GatewayConfig` with a `chat_only` user), then call `model_tools.handle_function_call("run_shell", {...}, skip_pre_tool_call_hook=True)` and assert it is blocked.

```python
def test_backstop_under_session_contextvars(monkeypatch):
    import model_tools
    from gateway.session_context import set_session_vars
    from gateway.config import Platform, PlatformConfig, GatewayConfig

    cfg = GatewayConfig()
    cfg.platforms[Platform.SLACK] = PlatformConfig(
        extra={"user_roles": {"U_A": "chat_only"}}
    )
    monkeypatch.setattr("gateway.tool_access._load_config_cached", lambda: cfg)
    set_session_vars(
        platform=Platform.SLACK, chat_id="C1", user_id="U_A",
        user_name="A", session_key="C1",
    )
    out = model_tools.handle_function_call(
        "run_shell", {"command": "ls"}, skip_pre_tool_call_hook=True
    )
    assert "not permitted" in out
```

> Note: align `set_session_vars(...)` kwargs with `gateway/session_context.py`. If `run_shell` is not a real registered tool name, substitute a real terminal-toolset tool name (check `registry.get_tool_names_for_toolset("terminal")`).

- [ ] **Step 2: Run the test to verify it passes**

Run: `pytest tests/gateway/test_tool_access_enforcement.py::test_backstop_under_session_contextvars -v`
Expected: PASS (if it fails because contextvars don't propagate into the dispatch path, that is a real finding — investigate `set_session_vars` scope before forcing the test green)

- [ ] **Step 3: Document the feature**

In `website/docs/user-guide/messaging/slack.md`, add a "Tool RBAC" section: the `roles` / `user_roles` config under `extra:`, the four built-in roles and their toolsets, the deny-until-assigned behavior, that it activates when `user_roles` is set, and that `SLACK_ALLOWED_USERS` is then ignored (and can be removed). Mirror the structure of the existing slash-command-tier docs.

```markdown
## Tool RBAC (per-user tool access)

Assign each Slack user a role that controls which tool categories the agent
may use on their behalf. Configure under the Slack platform's `extra:` block
in `~/.hermes/config.yaml`:

​```yaml
slack:
  enabled: true
  extra:
    user_roles:            # presence activates RBAC; this is the auth source
      U_ALICE: admin
      U_BOB:   operator
      U_CAROL: readonly
      U_DAVE:  chat_only
    roles:                 # optional — customize or add to the built-ins
      operator: { toolsets: [terminal, file, web, browser, vision, memory, delegation] }
​```

- Built-in roles: `admin` (all tools), `operator`, `readonly`, `chat_only` (chat, no tools).
- A user with **no** role is denied entirely, including chat ("deny until assigned").
- When `user_roles` is set, `SLACK_ALLOWED_USERS` is ignored — manage access via roles and remove the env var.
- Toolset names: `terminal`, `file`, `web`, `browser`, `vision`, `memory`, `delegation`, `code_execution`, `image_gen`, `session_search`, `mcp-*` (glob), or `"*"` for all.
```

- [ ] **Step 4: Backward-compat regression sweep**

Run the existing auth and slash-access suites to confirm no behavior change when RBAC is unconfigured:

Run: `pytest tests/gateway/test_slash_access.py tests/gateway/ -k "auth or authorized or slash" -v`
Expected: PASS (no regressions). Also run the full new suite:
Run: `pytest tests/gateway/test_tool_access.py tests/gateway/test_tool_access_enforcement.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/gateway/test_tool_access_enforcement.py website/docs/user-guide/messaging/slack.md
git commit -m "test(tool-access): delegation propagation + docs + regression sweep

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes (for the implementer)

- **Spec coverage:** Role model + built-ins (Task 1); YAML config (Tasks 1-2); deny-until-assigned auth gate (Task 5); toolset filter / "hide from model" (Task 4); execution backstop / "hard block" (Task 3); env-var retirement + allow-all precedence + pairing suppression (Task 5); MCP glob (Task 1); delegation propagation + backward compat (Task 6). Audit logging is intentionally out of scope.
- **Unresolved-by-design lookups** (resolve concretely, do not leave as placeholders): the gateway's typed-config accessor used in Tasks 4-5; the `HermesGateway` class name and pairing-offer call site; exact `SessionSource` / `set_session_vars` signatures; a real terminal-toolset tool name for the dispatch tests.
- **Type consistency:** `ToolAccessPolicy` methods (`is_authorized`, `role_for`, `allowed_toolsets`, `can_use_tool`) and module functions (`policy_from_extra`, `policy_for_source`, `denial_for_current_tool`, `filter_enabled_toolsets`) are named identically across all tasks.
