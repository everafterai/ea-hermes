# Cron RBAC Toolset Ceiling — Design

**Date:** 2026-06-30
**Status:** Approved design, pre-implementation
**Topic:** Cap a cron job's agent to its creator's RBAC role, and gate script jobs
**Related:** [Slack Tool RBAC design](2026-05-31-slack-tool-rbac-design.md),
[Automation ownership design](2026-06-30-automation-ownership-design.md)

## Problem

The Slack tool RBAC system ([gateway/tool_access.py](../../../gateway/tool_access.py))
restricts which **toolsets** an identified messaging-platform user may invoke, keyed
to a named role. It enforces at three points: the message gate, the toolset filter
that feeds `enabled_toolsets`, and the `pre_tool_call` execution backstop.

**Cron execution bypasses all three.** A cron job's agent is built with whatever
`enabled_toolsets` the job carries:

```python
# cron/scheduler.py:1843
agent = AIAgent(
    ...
    enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg),
    disabled_toolsets=_resolve_cron_disabled_toolsets(_cfg),
)
```

`_resolve_cron_enabled_toolsets` ([cron/scheduler.py:85](../../../cron/scheduler.py))
honors the per-job `enabled_toolsets` **verbatim**; the only denylist is
`{cronjob, messaging, clarify}` plus the global `agent.disabled_toolsets`
([cron/scheduler.py:62](../../../cron/scheduler.py)). `terminal` is not stripped.

At run time the scheduler **deliberately clears** the identity contextvars
(`platform=""`, `chat_id=""`, no `HERMES_SESSION_USER_ID`) at
[cron/scheduler.py:1598-1623](../../../cron/scheduler.py), because terminal
background-notification routing, TTS format selection, per-platform skill-disable
lists, and the `send_message` gate all branch on them. Consequently the RBAC
execution backstop short-circuits:

```python
# gateway/tool_access.py:389 (denial_for_current_tool)
if not user_id or not platform_name:
    return None  # CLI / system / cron context — no gating
```

So if a cron job carries `enabled_toolsets: ["terminal"]`, its agent gets a host
shell with **no RBAC in the loop**. The 2026-05-31 RBAC design explicitly assumed
cron was trusted ("when there is no user identity, RBAC does not apply"); that holds
only while job creation itself is admin-only.

### Why it is reachable

Among `BUILTIN_ROLES`, only `admin` (`*`) grants the `cronjob` toolset, so today a
`readonly`/`operator` user cannot create a cron job through the agent — the single
layer protecting the system. It breaks the moment a non-admin can influence a job's
`enabled_toolsets`:

1. **A custom role (or `channel_roles` grant) that includes `cronjob`.** A natural
   config — "let operators schedule reports" — lets that user set
   `enabled_toolsets: ["terminal"]` and escalate to a shell, despite their role
   forbidding `terminal`. The `cronjob` tool does no role validation of the
   requested toolsets ([tools/cronjob_tools.py:566](../../../tools/cronjob_tools.py)).
2. **The `file` toolset writing `jobs.json` directly.** `operator` holds `file`;
   `~/.hermes/cron/jobs.json` is **not** a protected data path, so an operator can
   inject a job with `terminal`.
3. **`no_agent`/`script` jobs.** A job with `no_agent=True` (or any `script` field)
   runs an arbitrary `~/.hermes/scripts/` script with **zero** tool-layer gating
   ([cron/scheduler.py:1429](../../../cron/scheduler.py)) — shell-equivalent.

Delegation is **not** a separate hole: `delegate_task` intersects a child's toolsets
with the parent's ([tools/delegate_tool.py:984](../../../tools/delegate_tool.py)), so
capping the cron agent's `enabled_toolsets` automatically caps every sub-agent.

## Goals

- A cron job's agent can never exceed its **creator's current RBAC role**, enforced
  at run time (the hard boundary), holding even against a hand-edited `jobs.json`
  **as long as the job has an owner record**.
- Creating a cron job whose `enabled_toolsets` exceed the creator's role is rejected
  at create time with a clear message (fail-fast UX + defense in depth).
- Creating a `no_agent`/`script` job requires a shell-capable role
  (`terminal` or `code_execution`), since running a script is shell-equivalent.
- Reuse the **automation ownership** registry as the creator-identity source — no new
  identity plumbing.
- Fully backward compatible: installs without RBAC, and existing ownerless jobs, are
  unaffected.

## Non-goals (YAGNI)

- Capping jobs with **no resolvable creator role** — no owner record, or an owner who
  is now roleless. These run unchanged (operator decision: existing legacy/ownerless
  jobs must keep working). The residual `jobs.json`-injection path is left open and
  made visible via an audit line; RBAC's `file`/`terminal` grants remain the real
  boundary for that path.
- A runtime RBAC check on `no_agent`/`script` execution (only **create-time** is
  gated; an injected `no_agent` job still requires `file`/shell access to place its
  script under `scripts/`).
- A `trust|restrict` config switch for ownerless jobs. RBAC activation
  (`user_roles` presence) and `automation_ownership.enabled` already serve as
  off-switches. Documented below as a future extension.
- Freezing a per-job ceiling at create time (would go stale on role changes).
- Re-seeding identity contextvars during cron runs (fights the scheduler's
  deliberate clearing and its dependent tool behaviors).
- Changes to the three existing RBAC enforcement points or to non-cron paths.

## Approach

Cap `enabled_toolsets` at the single scheduler chokepoint where toolsets are
decided, using the creator's **current** role as the ceiling. Resolve the creator
from the ownership registry (`cron:<job_id>` → `{platform, user_id}`), resolve their
role through `ToolAccessPolicy` for that platform, and intersect.

Rejected alternatives:

- **Freeze a `toolset_ceiling` on the job at create time.** Goes stale: demoting the
  creator wouldn't shrink an already-stored ceiling.
- **Re-seed `HERMES_SESSION_*` so the existing backstop fires.** The scheduler clears
  these on purpose; re-seeding resurrects terminal-routing/TTS/skill/send_message
  bugs, fails open on error, and doesn't hide tools from the model.

## Design

### 1. Ceiling resolver — `cron/rbac_ceiling.py` (new, pure, testable)

```python
def cron_owner_grant(job: dict, cfg) -> Optional[frozenset[str]]:
    """The creator's role toolset grant, or None when no ceiling applies."""
    # None (no cap) when ANY of:
    #   - automation_ownership disabled
    #   - no owner record for cron:<job_id>
    #   - RBAC inactive for the owner's platform
    #   - owner is roleless / undefined role  (operator decision: leave as-is)
    # Otherwise: the owner's role grant (admin '*' => effectively unrestricted).

def apply_cron_toolset_ceiling(resolved, job, cfg) -> Optional[list[str]]:
    """Intersect the resolved cron toolset list with the owner's grant.

    `resolved` is the output of _resolve_cron_enabled_toolsets (a list, or None
    meaning 'AIAgent loads the full default set'). Returns the capped list, or
    `resolved` unchanged when no ceiling applies.
    """
    grant = cron_owner_grant(job, cfg)
    if grant is None:
        return resolved                       # unchanged (may be None)
    universe = resolved if resolved is not None else _all_registered_toolsets()
    return sorted(t for t in universe
                  if _granted(grant, t) or t in FLOOR_TOOLSETS)
```

Key points:

- **Reuses existing primitives**: `automation_ownership.get_record` /
  `artifact_key` / `is_enabled`; `tool_access.policy_from_extra`,
  `_granted`, `FLOOR_TOOLSETS`, and the owner's role lookup. Channel grants are
  honored via the job's `origin.chat_id` (passed into the policy's
  `_effective_grant`) so the runtime ceiling matches what the creator could grant
  in-channel at create time.
- **Unset `enabled_toolsets` (`resolved is None`) + a cap applies** → expand to the
  full registered toolset universe before intersecting, so a non-admin owner who
  omits `enabled_toolsets` is capped instead of silently receiving the full default
  (which includes `terminal`).
- **Admin owner** → grant contains `*` → every toolset passes → no effective change.
- Fail-open on any internal error (log loudly), matching
  `tool_access.filter_enabled_toolsets` — the cap is the primary cron control, not a
  backstop, and a transient config error must not strip a legitimate job's tools.

### 2. Wire into the scheduler

Apply the ceiling to the resolved list before it reaches `AIAgent`, keeping the
existing denylist order `(enabled ∩ ceiling) − disabled`:

```python
# cron/scheduler.py:1843 area
_resolved = _resolve_cron_enabled_toolsets(job, _cfg)
_capped = apply_cron_toolset_ceiling(_resolved, job, _cfg)
agent = AIAgent(
    ...
    enabled_toolsets=_capped,
    disabled_toolsets=_resolve_cron_disabled_toolsets(_cfg),
)
```

This is the **runtime hard boundary**. Because delegation intersects child toolsets
with the parent, every sub-agent the cron agent spawns inherits the cap.

### 3. Create-time validation — `tools/cronjob_tools.py`

On `create` and `update`, when the caller has an identity
(`automation_ownership.current_identity()`) and RBAC is active for their platform:

- **Toolset ceiling:** reject any requested `enabled_toolsets` not granted by the
  caller's role; error names the offending toolset(s) and points to an admin. Skip
  entirely when the caller has no identity (CLI/local — trusted) or RBAC is inactive.
- **Script gate:** reject `no_agent=True` **or** a non-empty `script` unless the
  caller's role grants `terminal` or `code_execution`. (`update` is gated on the
  resulting job state — adding a script to an existing job is gated too.)

Admins (`*`) pass both checks. This is the same policy the runtime ceiling enforces,
surfaced early as a clear refusal rather than silent stripping, and is the **only**
enforcement for the script gate (a `no_agent` job has no agent toolset to cap).

### 4. Visibility — `data_access_audit`

When a job with **no resolvable owner role** runs with toolsets beyond
`FLOOR_TOOLSETS` (i.e. the residual ownerless/roleless path), append one line to the
existing `data_access_audit` trail (job id, resolved toolsets, "ownerless-elevated").
Reuses [agent/data_access_audit.py](../../../agent/data_access_audit.py); never
blocks. Gives operators a signal that an unattributed job is running elevated without
changing its behavior.

### Data flow

```
create/update (cronjob tool):
  current_identity() ──► [RBAC active for platform?]
        ├─ validate enabled_toolsets ⊆ role grant         (else reject)
        └─ [no_agent OR script?] require terminal|code_execution (else reject)
  ──► create_job(...) ; register_creator(cron:<id>, owner)

run (scheduler.run_job):
  resolved = _resolve_cron_enabled_toolsets(job, cfg)      # list | None
  grant    = cron_owner_grant(job, cfg)                    # owner→role→grant | None
  capped   = resolved if grant is None
             else (resolved or ALL_TOOLSETS) ∩ grant ∪ FLOOR
  AIAgent(enabled_toolsets = capped)                       # − disabled_toolsets
        └─ delegate_task children intersect with parent ⇒ inherit the cap
```

### Edge cases / error handling

| Case | Behavior |
| --- | --- |
| RBAC inactive (`user_roles` empty) | No cap, no create-time validation. Exactly today's behavior. |
| `automation_ownership` disabled | No owner records ⇒ no cap anywhere. |
| No owner record (legacy, CLI/local, injected) | **No cap** (operator decision). Audit line if elevated. |
| Owner now roleless / undefined role | **No cap** (operator decision: leave as-is). Audit line if elevated. |
| Owner holds a real lesser role | Capped to that role's grant — the hard boundary in action. |
| Admin owner (`*`) | Grant matches all ⇒ no change. |
| `enabled_toolsets` unset + cap applies | Expand to full toolset universe, then intersect (prevents silent full-default escalation). |
| Ceiling-resolution error | Fail-open + loud log (parity with `filter_enabled_toolsets`). |
| Delegated sub-agent | Inherits parent's capped toolsets via the existing intersection. |

### Future extension (documented, not built)

A `cron.rbac_ceiling.ownerless: trust | restrict` config key could later flip
ownerless/roleless jobs from fail-open to a floor/default ceiling, closing the
residual `jobs.json`-injection path. Out of scope now per YAGNI; the audit line
provides interim visibility.

## Testing strategy

TDD — failing tests first, especially for the pure resolver.

**Pure unit (`tests/cron/test_rbac_ceiling.py`):** `cron_owner_grant` /
`apply_cron_toolset_ceiling` across: admin owner (no change), operator owner
requesting `terminal` (stripped), readonly owner with unset `enabled_toolsets`
(expanded-then-capped, `terminal` absent), roleless owner (no cap), no owner record
(no cap), RBAC inactive (no cap), ownership disabled (no cap), channel-grant via
`origin.chat_id` (granted), internal error (fail-open).

**Create-time (`tests/.../test_cronjob_tools_rbac.py`):** a `cronjob`-granted but
non-shell role creating a job with `enabled_toolsets:["terminal"]` → rejected;
`no_agent=True` without `terminal`/`code_execution` → rejected; adding a `script` on
`update` → rejected; admin → allowed; CLI/no-identity → allowed; RBAC inactive →
allowed.

**Integration (`tests/cron/`):** `run_job` caps a job whose `enabled_toolsets`
exceed the owner's role (agent constructed without `terminal`); an ownerless job runs
unchanged; RBAC-inactive runs unchanged.

**Regression:** a sub-agent delegated from a capped cron agent cannot use `terminal`
(proves the cap propagates through delegation). With no RBAC config, cron toolset
resolution is byte-for-byte unchanged.

All via `scripts/run_tests.sh` (CI parity). Tests must not write to `~/.hermes/`.

## Key files

| Concern | File |
| --- | --- |
| New ceiling resolver | `cron/rbac_ceiling.py` (new) |
| Cap wired into scheduler | `cron/scheduler.py` (`_resolve_cron_enabled_toolsets` area, `run_job` ~1843) |
| Create-time validation + script gate | `tools/cronjob_tools.py` (`cronjob` create/update, ~459) |
| Creator identity source | `agent/automation_ownership.py` (`get_record`, `artifact_key`, `current_identity`, `is_enabled`) |
| RBAC policy primitives | `gateway/tool_access.py` (`policy_from_extra`, `_granted`, `FLOOR_TOOLSETS`, role lookup) |
| Toolset universe / mapping | `tools/registry.py`, `model_tools.py` (toolset → tools) |
| Audit visibility | `agent/data_access_audit.py` |
| Resolver unit tests | `tests/cron/test_rbac_ceiling.py` (new) |
| Create-time tests | `tests/.../test_cronjob_tools_rbac.py` (new) |
| Integration/regression | `tests/cron/` |
