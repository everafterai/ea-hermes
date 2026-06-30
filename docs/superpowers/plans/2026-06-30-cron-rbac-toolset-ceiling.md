# Cron RBAC Toolset Ceiling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cap a cron job's agent to its creator's current RBAC role at run time, and gate script/no_agent job creation behind a shell-capable role.

**Architecture:** A new pure module `cron/rbac_ceiling.py` resolves the job creator (from the automation-ownership registry) to their current RBAC toolset grant and intersects the job's resolved `enabled_toolsets` with it. The scheduler applies this at the single `AIAgent` construction point; `tools/cronjob_tools.py` validates the same policy at create/update time for fail-fast UX and to cover the script gate. Jobs with no resolvable creator role (ownerless/roleless) run uncapped (fail-open) and are surfaced via the data-access audit.

**Tech Stack:** Python 3, existing `gateway/tool_access.py` RBAC policy, `agent/automation_ownership.py` registry, `toolsets`/`tools/registry.py` toolset resolution, `agent/data_access_audit.py`.

**Design doc:** [docs/superpowers/specs/2026-06-30-cron-rbac-toolset-ceiling-design.md](../specs/2026-06-30-cron-rbac-toolset-ceiling-design.md)

## Global Constraints

- Run tests ONLY via `scripts/run_tests.sh` (CI parity: unset creds, `TZ=UTC`, xdist, subprocess isolation). Never bare `pytest`.
- Tests MUST NOT write to `~/.hermes/` (an autouse fixture redirects `HERMES_HOME`). Do not write change-detector tests.
- **Fail-open** on any ceiling-resolution error: return the uncapped resolution. The cap is the primary cron control, not a backstop — a transient error must not strip a legitimate job's tools. Mirrors `gateway/tool_access.filter_enabled_toolsets`.
- Preserve RBAC backward compatibility: when RBAC is inactive (`user_roles` empty) or `automation_ownership` is disabled, behavior is byte-for-byte unchanged.
- **Ownerless/roleless jobs are NOT capped** (operator decision) — only owners that resolve to a concrete, currently-defined role are capped.
- Lint: `ruff check .` (only `PLW1514` enforced) and `ty check` must pass.

---

### Task 1: Public RBAC helpers in `gateway/tool_access.py`

Add two public accessors the cron ceiling needs: a policy resolver by platform name, and a grant accessor that distinguishes "no role" (None) from "a role that grants nothing" (empty frozenset, e.g. `chat_only`).

**Files:**
- Modify: `gateway/tool_access.py` (add `ToolAccessPolicy.grant_for`; add module-level `policy_for_platform`; extend `__all__`)
- Test: `tests/gateway/test_tool_access.py`

**Interfaces:**
- Produces: `ToolAccessPolicy.grant_for(user_id: Optional[str], chat_id: Optional[str] = None) -> Optional[FrozenSet[str]]` — the effective grant, or `None` when the user resolves to no role (roleless/undefined). `chat_only` returns `frozenset()`, admin returns a set containing `"*"`.
- Produces: `policy_for_platform(platform_name: str) -> Optional[ToolAccessPolicy]` — policy from cached gateway config for a named platform, or `None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/gateway/test_tool_access.py` (import `policy_from_extra` is already used in that file):

```python
def test_grant_for_returns_none_for_roleless():
    policy = policy_from_extra({"user_roles": {"U1": "readonly"}})
    assert policy.grant_for("U_UNKNOWN") is None


def test_grant_for_returns_role_grant():
    policy = policy_from_extra({"user_roles": {"U1": "readonly"}})
    grant = policy.grant_for("U1")
    assert grant is not None
    assert "web" in grant and "terminal" not in grant


def test_grant_for_chat_only_is_empty_not_none():
    policy = policy_from_extra({"user_roles": {"U1": "chat_only"}})
    assert policy.grant_for("U1") == frozenset()


def test_policy_for_platform_delegates(monkeypatch):
    import gateway.tool_access as ta
    sentinel = object()
    monkeypatch.setattr(ta, "_policy_for_current_platform", lambda name: sentinel)
    assert ta.policy_for_platform("slack") is sentinel
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py -k "grant_for or policy_for_platform"`
Expected: FAIL — `AttributeError: 'ToolAccessPolicy' object has no attribute 'grant_for'` and `module 'gateway.tool_access' has no attribute 'policy_for_platform'`.

- [ ] **Step 3: Add `grant_for` to `ToolAccessPolicy`**

In `gateway/tool_access.py`, inside the `ToolAccessPolicy` dataclass, directly after the `_effective_grant` method (ends ~line 220), add:

```python
    def grant_for(
        self, user_id: Optional[str], chat_id: Optional[str] = None
    ) -> Optional[FrozenSet[str]]:
        """Public accessor for the user's effective toolset grant.

        Returns None when the user resolves to no role (roleless / undefined
        role) — distinct from a defined role that grants nothing (e.g.
        ``chat_only`` returns an empty frozenset). Used by the cron toolset
        ceiling to decide whether a cap applies at all.
        """
        return self._effective_grant(user_id, chat_id)
```

- [ ] **Step 4: Add `policy_for_platform` module function**

In `gateway/tool_access.py`, directly after `_policy_for_current_platform` (ends ~line 381), add:

```python
def policy_for_platform(platform_name: str) -> Optional[ToolAccessPolicy]:
    """Resolve the RBAC policy for a named platform from the cached gateway
    config. Returns None when config is unavailable or the platform is invalid.

    Public wrapper over the dispatch backstop's resolver, for callers (e.g. the
    cron toolset ceiling) that resolve a policy outside a live inbound request.
    """
    return _policy_for_current_platform(platform_name)
```

Then add `"policy_for_platform"` to the `__all__` list at the bottom of the file.

- [ ] **Step 5: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py -k "grant_for or policy_for_platform"`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add gateway/tool_access.py tests/gateway/test_tool_access.py
git commit -m "feat(rbac): expose grant_for + policy_for_platform for cron ceiling"
```

---

### Task 2: Pure ceiling intersection — `apply_cron_toolset_ceiling`

The pure core: given a resolved toolset list (or `None` = full default) and a grant (or `None` = no cap), return the capped list.

**Files:**
- Create: `cron/rbac_ceiling.py`
- Test: `tests/cron/test_rbac_ceiling.py`

**Interfaces:**
- Consumes: `gateway.tool_access._granted`, `gateway.tool_access.FLOOR_TOOLSETS`, `toolsets.get_all_toolsets`.
- Produces: `apply_cron_toolset_ceiling(resolved: Optional[List[str]], grant: Optional[FrozenSet[str]]) -> Optional[List[str]]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/cron/test_rbac_ceiling.py`:

```python
from cron.rbac_ceiling import apply_cron_toolset_ceiling


def test_no_grant_returns_resolved_unchanged():
    assert apply_cron_toolset_ceiling(["terminal", "web"], None) == ["terminal", "web"]
    assert apply_cron_toolset_ceiling(None, None) is None


def test_wildcard_grant_returns_resolved_unchanged():
    assert apply_cron_toolset_ceiling(["terminal"], frozenset({"*"})) == ["terminal"]
    assert apply_cron_toolset_ceiling(None, frozenset({"*"})) is None


def test_caps_to_grant_and_keeps_floor():
    grant = frozenset({"web", "vision"})
    out = apply_cron_toolset_ceiling(["terminal", "web", "todo"], grant)
    assert "terminal" not in out
    assert "web" in out
    assert "todo" in out  # 'todo' is a FLOOR toolset, always kept


def test_chat_only_grant_caps_to_floor_only():
    out = apply_cron_toolset_ceiling(["terminal", "web", "todo"], frozenset())
    assert out == ["todo"]


def test_unset_resolved_expands_then_caps(monkeypatch):
    monkeypatch.setattr("toolsets.get_all_toolsets", lambda: ["terminal", "web", "file"])
    out = apply_cron_toolset_ceiling(None, frozenset({"web"}))
    assert out == ["web"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/cron/test_rbac_ceiling.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'cron.rbac_ceiling'`.

- [ ] **Step 3: Create the module with the pure function**

Create `cron/rbac_ceiling.py`:

```python
"""Per-creator RBAC toolset ceiling for cron jobs.

Cron execution runs with no platform identity in session contextvars, so the
RBAC execution backstop (gateway/tool_access.denial_for_current_tool) cannot
gate a cron-spawned agent. A job's ``enabled_toolsets`` would otherwise be the
sole determinant of the agent's capabilities with no check against the role of
the user who created it — letting a non-admin who can author a job escalate to
``terminal`` and a host shell.

This module caps a cron job's resolved ``enabled_toolsets`` to the toolset grant
of its creator's CURRENT RBAC role. The creator is read from the automation
ownership registry (``cron:<job_id>``); the role is resolved fresh each run, so
a later demotion shrinks the ceiling.

Operator decisions (see
docs/superpowers/specs/2026-06-30-cron-rbac-toolset-ceiling-design.md):
  * Jobs with NO resolvable creator role — no owner record, or an owner who is
    now roleless / has an undefined role — run UNCAPPED (legacy/ownerless jobs
    must keep working). Surfaced via the data-access audit when elevated.
  * The cap is the primary cron control, not a backstop: it fails OPEN on any
    internal error, mirroring gateway/tool_access.filter_enabled_toolsets.
"""

from __future__ import annotations

import logging
from typing import FrozenSet, List, Optional

logger = logging.getLogger(__name__)


def apply_cron_toolset_ceiling(
    resolved: Optional[List[str]], grant: Optional[FrozenSet[str]]
) -> Optional[List[str]]:
    """Intersect a cron job's resolved toolset list with the creator's grant.

    ``resolved`` is the output of
    cron.scheduler._resolve_cron_enabled_toolsets: a list of toolset names, or
    None meaning "AIAgent loads the full default set". ``grant`` comes from
    :func:`cron_owner_grant`. Returns the capped, sorted list, or ``resolved``
    unchanged when no ceiling applies (grant is None, or the role grants
    everything via "*").
    """
    try:
        if grant is None or "*" in grant:
            return resolved
        from gateway.tool_access import FLOOR_TOOLSETS, _granted

        if resolved is not None:
            universe = frozenset(resolved)
        else:
            from toolsets import get_all_toolsets

            universe = frozenset(get_all_toolsets())
        return sorted(
            t for t in universe if _granted(grant, t) or t in FLOOR_TOOLSETS
        )
    except Exception as err:  # pragma: no cover - defensive, fail-open
        logger.debug("apply_cron_toolset_ceiling failed (fail-open): %s", err)
        return resolved
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/cron/test_rbac_ceiling.py`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add cron/rbac_ceiling.py tests/cron/test_rbac_ceiling.py
git commit -m "feat(cron): pure toolset-ceiling intersection"
```

---

### Task 3: Owner→grant resolution — `cron_owner_grant`

Resolve a job to its creator's current grant via the ownership registry + RBAC policy. All I/O lives here so Task 2 stays pure.

**Files:**
- Modify: `cron/rbac_ceiling.py` (add `cron_owner_grant`)
- Test: `tests/cron/test_rbac_ceiling.py`

**Interfaces:**
- Consumes: `agent.automation_ownership.{is_enabled, get_record, artifact_key}`, `gateway.tool_access.policy_for_platform` (Task 1), `ToolAccessPolicy.grant_for` (Task 1).
- Produces: `cron_owner_grant(job: dict) -> Optional[FrozenSet[str]]` — the creator's grant, or `None` when no ceiling applies.

- [ ] **Step 1: Write the failing tests**

Append to `tests/cron/test_rbac_ceiling.py`:

```python
import cron.rbac_ceiling as ceiling


class _FakePolicy:
    enabled = True

    def __init__(self, grant):
        self._grant = grant

    def grant_for(self, user_id, chat_id=None):
        return self._grant


def _record(user_id="U1", platform="slack"):
    return {"owner": {"user_id": user_id, "platform": platform, "display_name": "Bob"}}


def test_owner_grant_none_when_ownership_disabled(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.is_enabled", lambda: False)
    assert ceiling.cron_owner_grant({"id": "abc"}) is None


def test_owner_grant_none_when_no_record(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.is_enabled", lambda: True)
    monkeypatch.setattr("agent.automation_ownership.get_record", lambda key: None)
    assert ceiling.cron_owner_grant({"id": "abc"}) is None


def test_owner_grant_none_when_rbac_inactive(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.is_enabled", lambda: True)
    monkeypatch.setattr("agent.automation_ownership.get_record", lambda key: _record())

    class _Disabled:
        enabled = False

    monkeypatch.setattr("gateway.tool_access.policy_for_platform", lambda name: _Disabled())
    assert ceiling.cron_owner_grant({"id": "abc"}) is None


def test_owner_grant_resolves_role(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.is_enabled", lambda: True)
    monkeypatch.setattr("agent.automation_ownership.get_record", lambda key: _record())
    monkeypatch.setattr(
        "gateway.tool_access.policy_for_platform",
        lambda name: _FakePolicy(frozenset({"web"})),
    )
    assert ceiling.cron_owner_grant({"id": "abc"}) == frozenset({"web"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/cron/test_rbac_ceiling.py -k owner_grant`
Expected: FAIL — `AttributeError: module 'cron.rbac_ceiling' has no attribute 'cron_owner_grant'`.

- [ ] **Step 3: Implement `cron_owner_grant`**

In `cron/rbac_ceiling.py`, add above `apply_cron_toolset_ceiling`:

```python
def cron_owner_grant(job: dict) -> Optional[FrozenSet[str]]:
    """Resolve the toolset grant of the job creator's current RBAC role.

    Returns None ("no ceiling applies") when ownership is disabled, the job has
    no owner record, RBAC is inactive for the owner's platform, or the owner is
    roleless / has an undefined role. Returns the role's grant frozenset
    otherwise (admin's grant contains "*"). Fail-open on any internal error.
    """
    try:
        from agent import automation_ownership as ao

        if not ao.is_enabled():
            return None
        job_id = job.get("id")
        if not job_id:
            return None
        record = ao.get_record(ao.artifact_key("cron", str(job_id)))
        owner = (record or {}).get("owner") or {}
        user_id = owner.get("user_id")
        platform = owner.get("platform")
        if not user_id or not platform:
            return None
        from gateway.tool_access import policy_for_platform

        policy = policy_for_platform(str(platform))
        if policy is None or not policy.enabled:
            return None
        chat_id = (job.get("origin") or {}).get("chat_id")
        return policy.grant_for(str(user_id), str(chat_id) if chat_id else None)
    except Exception as err:  # pragma: no cover - defensive, fail-open
        logger.debug("cron_owner_grant failed (fail-open): %s", err)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/cron/test_rbac_ceiling.py`
Expected: PASS (9 passed total).

- [ ] **Step 5: Commit**

```bash
git add cron/rbac_ceiling.py tests/cron/test_rbac_ceiling.py
git commit -m "feat(cron): resolve cron job creator to current RBAC grant"
```

---

### Task 4: Audit helper + scheduler wiring

Add the ownerless-elevated audit line, then apply the ceiling at the scheduler's single `AIAgent` construction point via a testable seam.

**Files:**
- Modify: `cron/rbac_ceiling.py` (add `audit_ownerless_elevated`)
- Modify: `cron/scheduler.py` (add `_cron_enabled_toolsets_with_ceiling`; change the `AIAgent(... enabled_toolsets=...)` call at ~line 1843)
- Test: `tests/cron/test_rbac_ceiling.py`, `tests/cron/test_scheduler_ceiling.py`

**Interfaces:**
- Consumes: `agent.data_access_audit.record_access(*, tool, action, target)`, `gateway.tool_access.FLOOR_TOOLSETS`, Task 2 + Task 3 functions.
- Produces: `audit_ownerless_elevated(job: dict, resolved: Optional[List[str]]) -> None`; `cron.scheduler._cron_enabled_toolsets_with_ceiling(job: dict, cfg: dict) -> Optional[List[str]]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/cron/test_rbac_ceiling.py`:

```python
def test_audit_ownerless_elevated_logs_when_elevated(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "agent.data_access_audit.record_access",
        lambda **kw: calls.append(kw),
    )
    ceiling.audit_ownerless_elevated({"id": "abc"}, ["terminal", "todo"])
    assert len(calls) == 1
    assert calls[0]["tool"] == "cron"
    assert "terminal" in calls[0]["target"]


def test_audit_ownerless_elevated_silent_when_floor_only(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "agent.data_access_audit.record_access",
        lambda **kw: calls.append(kw),
    )
    ceiling.audit_ownerless_elevated({"id": "abc"}, ["todo"])
    assert calls == []
```

Create `tests/cron/test_scheduler_ceiling.py`:

```python
import cron.scheduler as sched


def test_ceiling_caps_resolved(monkeypatch):
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: ["terminal", "web"])
    monkeypatch.setattr("cron.rbac_ceiling.cron_owner_grant", lambda job: frozenset({"web"}))
    out = sched._cron_enabled_toolsets_with_ceiling({"id": "abc"}, {})
    assert "terminal" not in out
    assert "web" in out


def test_ceiling_noop_when_ownerless(monkeypatch):
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: ["terminal"])
    monkeypatch.setattr("cron.rbac_ceiling.cron_owner_grant", lambda job: None)
    out = sched._cron_enabled_toolsets_with_ceiling({"id": "abc"}, {})
    assert out == ["terminal"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/cron/test_rbac_ceiling.py -k audit tests/cron/test_scheduler_ceiling.py`
Expected: FAIL — `audit_ownerless_elevated` missing, and `_cron_enabled_toolsets_with_ceiling` missing.

- [ ] **Step 3: Add `audit_ownerless_elevated` to `cron/rbac_ceiling.py`**

```python
def audit_ownerless_elevated(job: dict, resolved: Optional[List[str]]) -> None:
    """Append one data-access audit line when a job with no resolvable creator
    role runs with toolsets beyond the floor. Visibility only; never raises,
    never blocks."""
    try:
        from gateway.tool_access import FLOOR_TOOLSETS

        if resolved is None:
            elevated, shown = True, "ALL_DEFAULT"
        else:
            elevated = bool(set(resolved) - set(FLOOR_TOOLSETS))
            shown = ",".join(resolved)
        if not elevated:
            return
        from agent.data_access_audit import record_access

        record_access(
            tool="cron",
            action="ownerless-elevated",
            target=f"cron:{job.get('id', '?')} toolsets={shown}",
        )
    except Exception:  # pragma: no cover - defensive
        pass
```

- [ ] **Step 4: Add the scheduler seam**

In `cron/scheduler.py`, add this function immediately after `_resolve_cron_enabled_toolsets` (ends ~line 113):

```python
def _cron_enabled_toolsets_with_ceiling(job: dict, cfg: dict) -> list[str] | None:
    """Resolve the cron job's enabled toolsets, then cap them to the creator's
    current RBAC role (see cron/rbac_ceiling). Fail-open: any error returns the
    uncapped resolution so a transient failure can't strip a legitimate job's
    tools. Audits ownerless/roleless jobs that run elevated."""
    resolved = _resolve_cron_enabled_toolsets(job, cfg)
    try:
        from cron.rbac_ceiling import (
            apply_cron_toolset_ceiling,
            audit_ownerless_elevated,
            cron_owner_grant,
        )

        grant = cron_owner_grant(job)
        if grant is None:
            audit_ownerless_elevated(job, resolved)
        return apply_cron_toolset_ceiling(resolved, grant)
    except Exception as exc:  # pragma: no cover - defensive, fail-open
        logger.debug("Job '%s': toolset ceiling skipped (%s)", job.get("id"), exc)
        return resolved
```

- [ ] **Step 5: Wire it into the `AIAgent` construction**

In `cron/scheduler.py`, change the line at ~1843 inside `run_job`:

```python
            enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg),
```

to:

```python
            enabled_toolsets=_cron_enabled_toolsets_with_ceiling(job, _cfg),
```

(Leave the adjacent `disabled_toolsets=_resolve_cron_disabled_toolsets(_cfg),` line unchanged — the existing denylist still layers on top.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/cron/test_rbac_ceiling.py tests/cron/test_scheduler_ceiling.py`
Expected: PASS (13 in test_rbac_ceiling.py + 2 in test_scheduler_ceiling.py).

- [ ] **Step 7: Commit**

```bash
git add cron/rbac_ceiling.py cron/scheduler.py tests/cron/test_rbac_ceiling.py tests/cron/test_scheduler_ceiling.py
git commit -m "feat(cron): cap cron agent toolsets to creator's RBAC role at run time"
```

---

### Task 5: Create-time validation + script gate in `tools/cronjob_tools.py`

Reject over-privileged `enabled_toolsets` and gate `no_agent`/`script` jobs behind a shell-capable role, on both `create` and `update`. This is fail-fast UX for the runtime ceiling and the ONLY enforcement for the script gate (no_agent jobs never build an agent).

**Files:**
- Modify: `tools/cronjob_tools.py` (add `_rbac_creation_error`; call it in `create` and `update`)
- Test: `tests/cron/test_cronjob_rbac.py`

**Interfaces:**
- Consumes: `agent.automation_ownership.current_identity`, `gateway.tool_access.policy_for_platform`, `ToolAccessPolicy.{allowed_toolsets, can_use_tool}`, module-level `get_session_env` (already imported in this file).
- Produces: `_rbac_creation_error(*, enabled_toolsets, has_script: bool, is_no_agent: bool) -> Optional[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/cron/test_cronjob_rbac.py`:

```python
import tools.cronjob_tools as cj
from agent.automation_ownership import Identity


class _Policy:
    enabled = True

    def __init__(self, grant):
        self._grant = grant

    def allowed_toolsets(self, user_id, requested, chat_id=None):
        return frozenset(t for t in requested if "*" in self._grant or t in self._grant)

    def can_use_tool(self, user_id, toolset, chat_id=None):
        return "*" in self._grant or toolset in self._grant


def _setup(monkeypatch, grant, identity=Identity("slack", "U1", "Bob")):
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda *a, **k: "")
    monkeypatch.setattr("agent.automation_ownership.current_identity", lambda: identity)
    monkeypatch.setattr("gateway.tool_access.policy_for_platform", lambda name: _Policy(grant))


def test_rejects_overprivileged_toolset(monkeypatch):
    _setup(monkeypatch, frozenset({"web"}))
    err = cj._rbac_creation_error(enabled_toolsets=["terminal"], has_script=False, is_no_agent=False)
    assert err and "terminal" in err


def test_allows_within_role(monkeypatch):
    _setup(monkeypatch, frozenset({"web"}))
    assert cj._rbac_creation_error(enabled_toolsets=["web"], has_script=False, is_no_agent=False) is None


def test_script_requires_shell_role(monkeypatch):
    _setup(monkeypatch, frozenset({"web"}))
    err = cj._rbac_creation_error(enabled_toolsets=None, has_script=True, is_no_agent=False)
    assert err and ("terminal" in err or "code_execution" in err)


def test_no_agent_requires_shell_role(monkeypatch):
    _setup(monkeypatch, frozenset({"web"}))
    err = cj._rbac_creation_error(enabled_toolsets=None, has_script=False, is_no_agent=True)
    assert err and ("terminal" in err or "code_execution" in err)


def test_admin_allowed(monkeypatch):
    _setup(monkeypatch, frozenset({"*"}))
    assert cj._rbac_creation_error(enabled_toolsets=["terminal"], has_script=True, is_no_agent=True) is None


def test_no_identity_is_trusted(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.current_identity", lambda: None)
    assert cj._rbac_creation_error(enabled_toolsets=["terminal"], has_script=True, is_no_agent=True) is None


def test_create_returns_rbac_error(monkeypatch):
    monkeypatch.setattr(cj, "_rbac_creation_error", lambda **kw: "DENIED-X")
    out = cj.cronjob(action="create", schedule="0 9 * * *", prompt="hi", enabled_toolsets=["terminal"])
    assert "DENIED-X" in out


def test_update_returns_rbac_error(monkeypatch):
    monkeypatch.setattr(
        cj, "resolve_job_ref",
        lambda ref: {"id": "abc", "name": "n", "enabled_toolsets": None},
    )
    monkeypatch.setattr(cj, "_rbac_creation_error", lambda **kw: "DENIED-Y")
    out = cj.cronjob(action="update", job_id="abc", enabled_toolsets=["terminal"])
    assert "DENIED-Y" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/cron/test_cronjob_rbac.py`
Expected: FAIL — `AttributeError: module 'tools.cronjob_tools' has no attribute '_rbac_creation_error'`.

- [ ] **Step 3: Add the `_rbac_creation_error` helper**

In `tools/cronjob_tools.py`, add this module-level function above the `cronjob(` function (~line 459):

```python
def _rbac_creation_error(
    *, enabled_toolsets, has_script: bool, is_no_agent: bool
) -> Optional[str]:
    """Return an error string if the acting user's RBAC role may not create a
    cron job with these capabilities, else None.

    Enforced only when a human identity is present (CLI / local / autonomous
    jobs are trusted) and RBAC is active for that platform. Mirrors the runtime
    toolset ceiling so an over-privileged request fails fast instead of being
    silently stripped, and is the ONLY enforcement for the script gate (a
    no_agent job never builds an agent to cap). Fail-open on any error — the
    runtime ceiling remains the hard control.
    """
    try:
        from agent.automation_ownership import current_identity

        identity = current_identity()
        if identity is None:
            return None  # CLI / local / autonomous — trusted
        from gateway import tool_access

        policy = tool_access.policy_for_platform(identity.platform)
        if policy is None or not policy.enabled:
            return None
        from gateway.session_context import get_session_env

        user_id = identity.user_id
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID") or None

        if enabled_toolsets:
            allowed = policy.allowed_toolsets(user_id, frozenset(enabled_toolsets), chat_id)
            denied = sorted(set(enabled_toolsets) - set(allowed))
            if denied:
                return (
                    "Your role does not permit granting these toolset(s) to a "
                    f"cron job: {', '.join(denied)}. Ask an admin to adjust your "
                    "role if you need them."
                )

        if is_no_agent or has_script:
            if not (
                policy.can_use_tool(user_id, "terminal", chat_id)
                or policy.can_use_tool(user_id, "code_execution", chat_id)
            ):
                return (
                    "Creating a script or no_agent cron job requires a role that "
                    "grants 'terminal' or 'code_execution' (running a script is "
                    "shell-equivalent). Ask an admin if you need this."
                )
        return None
    except Exception:
        return None  # fail-open — runtime ceiling remains the hard control
```

(`Optional` is imported at the top of `tools/cronjob_tools.py` via `from typing import Any, Dict, List, Optional, Union` at line 13. Note: `get_session_env` is imported only *inside* `_origin_from_env` (line 270), NOT at module level — so this helper imports it locally, as shown.)

- [ ] **Step 4: Call it from `create`**

In the `create` branch, immediately before `job = create_job(` (~line 553), insert:

```python
            _rbac_err = _rbac_creation_error(
                enabled_toolsets=enabled_toolsets,
                has_script=bool(script),
                is_no_agent=bool(no_agent),
            )
            if _rbac_err:
                return tool_error(_rbac_err, success=False)

```

- [ ] **Step 5: Call it from `update`**

In the `update` branch, after the `updates` dict is fully assembled (after the `no_agent` handling, ~line 739-742) and immediately before the `update_job(` call, insert:

```python
            _eff_toolsets = (
                updates["enabled_toolsets"]
                if "enabled_toolsets" in updates
                else job.get("enabled_toolsets")
            )
            _eff_script = updates["script"] if "script" in updates else job.get("script")
            _eff_no_agent = updates["no_agent"] if "no_agent" in updates else job.get("no_agent")
            _rbac_err = _rbac_creation_error(
                enabled_toolsets=_eff_toolsets,
                has_script=bool(_eff_script),
                is_no_agent=bool(_eff_no_agent),
            )
            if _rbac_err:
                return tool_error(_rbac_err, success=False)

```

(If the exact `update_job(` call line differs, the insertion point is: after every `updates[...] = ...` assignment in the `update` branch, before the job is persisted.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/cron/test_cronjob_rbac.py`
Expected: PASS (8 passed).

- [ ] **Step 7: Commit**

```bash
git add tools/cronjob_tools.py tests/cron/test_cronjob_rbac.py
git commit -m "feat(cron): validate creator role on cron create/update + script gate"
```

---

### Task 6: Full-suite verification, lint, and regression confirmation

Confirm nothing regressed (especially cron and RBAC suites), that delegation propagation holds, and that lint/typecheck pass.

**Files:**
- None (verification only)

- [ ] **Step 1: Run the cron + RBAC suites**

Run: `scripts/run_tests.sh tests/cron/ tests/gateway/test_tool_access.py`
Expected: PASS — all green, including pre-existing cron tests (no behavior change when RBAC inactive / ownerless).

- [ ] **Step 2: Confirm delegation propagation (reasoning + grep, no new test)**

The cron agent's capped `enabled_toolsets` propagates to delegated sub-agents because `delegate_task` intersects a child's toolsets with the parent's. Verify the intersection still exists:

Run: `rg -n "expanded_parent|child_toolsets = \[t for t in toolsets if t in" tools/delegate_tool.py`
Expected: shows the intersection at ~line 984 (`child_toolsets = [t for t in toolsets if t in expanded_parent]`). No code change needed — capping the parent caps the children.

- [ ] **Step 3: Run the full suite**

Run: `scripts/run_tests.sh`
Expected: PASS (no new failures attributable to this change).

- [ ] **Step 4: Lint and typecheck**

Run: `ruff check . && ty check`
Expected: clean (or only pre-existing, unrelated diagnostics).

- [ ] **Step 5: Final commit (if any verification fixes were needed)**

```bash
git add -A
git commit -m "test(cron): verify cron RBAC ceiling across full suite"
```

(Skip if Steps 1-4 required no changes.)

---

## Self-Review

**Spec coverage:**
- Runtime ceiling at the scheduler chokepoint → Task 4 (`_cron_enabled_toolsets_with_ceiling`, wired at ~1843).
- Creator identity from ownership registry → Task 3 (`cron_owner_grant` via `get_record`).
- Current-role (not frozen) resolution → Task 3 (resolves policy each call).
- Create-time validation → Task 5 (`_rbac_creation_error` on create + update).
- Script/no_agent gate behind `terminal`/`code_execution` → Task 5.
- Ownerless/roleless = no cap → Tasks 2/3 (`grant is None` → unchanged); covered by `test_no_grant_returns_resolved_unchanged`, `test_owner_grant_none_*`.
- Unset `enabled_toolsets` expand-then-cap → Task 2 (`test_unset_resolved_expands_then_caps`).
- Admin owner unaffected → Task 2 (`test_wildcard_grant_returns_resolved_unchanged`).
- Audit line for ownerless-elevated → Task 4 (`audit_ownerless_elevated`).
- Fail-open on error → Tasks 2/3/4/5 (try/except returning the uncapped/None value).
- Delegation inherits cap → Task 6 Step 2 (existing intersection).
- RBAC-inactive / ownership-disabled unchanged → Tasks 3 (`enabled`/`is_enabled` guards) + Task 6 full suite.

**Placeholder scan:** none — every step has complete code or an exact command.

**Type consistency:** `cron_owner_grant(job) -> Optional[FrozenSet[str]]` feeds `apply_cron_toolset_ceiling(resolved, grant)`; `grant_for(user_id, chat_id) -> Optional[FrozenSet[str]]` matches the `grant` type; `_rbac_creation_error(*, enabled_toolsets, has_script, is_no_agent) -> Optional[str]` is called identically in create and update. `policy_for_platform(name) -> Optional[ToolAccessPolicy]` consumed with `.enabled`/`.grant_for`/`.allowed_toolsets`/`.can_use_tool` — all existing or Task-1 methods.
