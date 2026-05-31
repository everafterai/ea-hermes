# Slack Tool RBAC — Design

**Date:** 2026-05-31
**Status:** Approved design, pre-implementation
**Topic:** Role-based access control for which tools each Slack user may invoke

## Problem

Hermes' Slack integration today has a binary chat allowlist (`SLACK_ALLOWED_USERS`
in `~/.hermes/.env`, checked in `gateway/run.py:_is_user_authorized`). Every
allowed user gets full, identical access to every tool the agent can run —
shell/terminal, file read/write, web, browser, MCP, etc. There is a separate
two-tier slash-command split (`gateway/slash_access.py`, config keys
`allow_admin_from` / `user_allowed_commands`), but it gates **slash commands
only**, not tool execution in free-form chat.

We want to open the agent to many Slack users without giving all of them the
power to execute shell scripts, modify environments, or write files. We need
**role-based access control over tool execution**, keyed to the Slack user.

## Goals

- Named roles, each granting a set of tool **categories (toolsets)**.
- One role per user, applied everywhere (DM and channel alike).
- Forbidden tools are both **hidden from the model** and **hard-blocked at
  execution** (defense in depth).
- **Deny until assigned:** a user with no role cannot use the agent at all,
  not even chat.
- All configuration in YAML (`~/.hermes/config.yaml`); the `SLACK_ALLOWED_USERS`
  env var is retired in favor of role assignment as the single source of truth.
- Fully backward compatible: installs that don't configure RBAC are unaffected.

## Non-goals (YAGNI)

- Audit logging of tool-access decisions (may add later).
- Per-DM-vs-channel role differences (one role everywhere).
- Per-user overrides on top of roles (named roles only).
- Unifying RBAC with the dangerous-command approval system or the slash-command
  tiers — they remain orthogonal and continue to apply on top.
- Individual-tool granularity in roles — roles grant whole toolset categories
  (with a wildcard and an `mcp-*` glob).

## Approach

Mirror the proven `gateway/slash_access.py` pattern: a new pure, unit-testable
policy module wired into the existing enforcement chokepoints. Chosen over a
plugin-based implementation (a plugin cannot filter the toolset the model sees —
that assembly lives in core) and over extending the existing two-tier admin/user
split (only two tiers; cannot express multiple named roles).

## Design

### 1. Role model & configuration

New module **`gateway/tool_access.py`**, sibling to `slash_access.py`, exposing a
pure `ToolAccessPolicy`. All config lives in the Slack block of
`~/.hermes/config.yaml`, read via `platform_config.extra` (the same place
`allow_admin_from` lands after config bridging):

```yaml
slack:
  enabled: true
  # token etc. remain in .env as today
  extra:
    # --- RBAC (single source of truth when present) ---
    roles:                       # OPTIONAL — omit to use built-in defaults
      admin:     { toolsets: ["*"] }
      operator:  { toolsets: [terminal, file, web, browser, vision, memory, delegation] }
      readonly:  { toolsets: [web, vision, session_search, memory] }
      chat_only: { toolsets: [] }
    user_roles:                  # REQUIRED to activate RBAC
      U_ALICE: admin
      U_BOB:   operator
      U_CAROL: readonly
      U_DAVE:  chat_only
```

Semantics:

- **Built-in default roles** ship in code: `admin` (`["*"]`), `operator`,
  `readonly`, `chat_only` (`[]`). The common case is just the `user_roles` map;
  the `roles:` block is only needed to customize or add roles. Defining a role
  with a built-in name overrides the built-in.
- Toolset names are the real registry values: `terminal`, `file`, `web`,
  `browser`, `browser-cdp`, `vision`, `memory`, `delegation`, `code_execution`,
  `image_gen`, `session_search`, `skills`, `mcp-<server>`, etc.
- `"*"` grants all toolsets; `[]` grants none (chat but no tools).
- **`user_roles` is the authorization source when RBAC is active.** Assigned a
  role ⇒ may chat *and* receives that role's toolsets. Not in `user_roles` ⇒
  denied entirely.
- **Activation:** RBAC is active for Slack when `user_roles` is present and
  non-empty. While absent, behavior is exactly as today.
- **Fail-closed:** a `user_roles` entry referencing an undefined role denies that
  user and logs a config error.

`ToolAccessPolicy` exposes pure functions: `is_authorized(user_id)`,
`allowed_toolsets(user_id)`, `can_use_tool(user_id, tool_name)`. A
`policy_for_source(gateway_config, source)` resolver mirrors
`slash_access.policy_for_source`. Toolset matching supports exact names, the `"*"`
wildcard, and an `mcp-*` glob.

### 2. Enforcement (three points, one policy)

All three resolve the same `ToolAccessPolicy`; they differ in where identity comes
from and what they do on deny.

**A. Message gate — "can this user interact at all?"**
In `gateway/run.py:_is_user_authorized`, before the final deny: if RBAC is active
for the platform, decide authorization by role presence. Has a role → authorized;
no role → denied. This short-circuits the `SLACK_ALLOWED_USERS` check so role
assignment fully replaces the env allowlist. Identity comes from the
`SessionSource` already passed in. Reuses the existing unauthorized-response path.

**B. Toolset filter — "what does the model see?"**
At the point the agent's toolset is assembled for a run, compute the role's
allowed toolsets and pass them as `enabled_toolsets` into
`model_tools.get_tool_definitions` (intersected with any existing enabled config).
The model is only ever shown tools the role grants; `chat_only` sees none.
`enabled_toolsets` is already part of that function's memoization key, so per-user
variation is cache-safe. Identity is known at run setup.

**C. Execution backstop — "enforce even if reached another way."**
At the dispatch chokepoint (the `pre_tool_call` hook in
`model_tools.py`, ~line 784), resolve the policy from `HERMES_SESSION_USER_ID` +
`HERMES_SESSION_PLATFORM` contextvars (set by `set_session_vars`), map the
requested tool → its `toolset` via the registry, and deny if the role doesn't
grant it. Covers paths that bypass the filter — `delegation` sub-agents, the
`code_execution` sandbox, plugin-invoked calls — returning a clear denial message.

Supporting pieces:

- The policy is resolved from `platform_config.extra` (config bridged in,
  mirroring `allow_admin_from`) and cached so the per-call backstop does not
  reload YAML each call.
- Orthogonal systems unchanged: dangerous-command approval (`tools/approval.py`)
  and slash-command tiers (`gateway/slash_access.py`) continue to apply on top.

### 3. Backward compatibility & edge cases

- **Activation boundary (safety hinge):** RBAC active only when `user_roles` is
  non-empty. Inactive → today's behavior exactly; existing users keep full
  access. Reversible by removing `user_roles`.
- **Env-var retirement:** when RBAC is active, the Slack auth gate is decided by
  role presence and `SLACK_ALLOWED_USERS` is ignored. If both are set, RBAC wins
  and a one-time info line is logged noting the env var is ignored.
- **Allow-all precedence:** `SLACK_ALLOW_ALL_USERS=true` is ignored when RBAC is
  active (otherwise unassigned users could chat with no role).
- **DM pairing flow:** when RBAC is active, the pairing-code offer is disabled;
  an unauthorized DM user gets a plain "ask an admin to assign you a role"
  message. Pairing is unchanged when RBAC is inactive.
- **Channels:** tool gates (B, C) are always per-user, keyed to whoever triggered
  the run, independent of channel. In a channel with RBAC active the triggering
  user must have a role; existing channel allowlists still control which channels
  the bot listens in. A roleless user in an allowed channel is not acted on.
- **System / CLI / cron context:** when there is no user identity, RBAC does not
  apply (backstop allows), matching how the slash gate treats an empty user.
- **Unknown role / config errors:** fail closed — deny the user and log.
- **MCP & dynamic tools:** filter and backstop resolve a tool's toolset from the
  live registry at call time; the `mcp-*` glob covers servers added later. The
  registry generation counter already invalidates the tool-definitions cache.
- **Delegation/sandbox propagation:** the backstop relies on session contextvars
  being present in delegated-task and sandbox threads — verified in testing
  rather than assumed.

### 4. Testing strategy

Mirrors `tests/gateway/test_slash_access.py`: a thick layer of pure-policy unit
tests, then thinner enforcement-integration tests. TDD — failing tests first,
especially for the pure policy.

**Pure policy unit tests** (`tests/gateway/test_tool_access.py`):
- Parsing: `roles` + `user_roles` from `extra`; built-in defaults when `roles:`
  omitted; custom override of a built-in name; id coercion (int/str, whitespace).
- Activation: empty/absent `user_roles` → disabled policy.
- `is_authorized`: assigned → true; unassigned → false.
- `allowed_toolsets`: `"*"` → all; `[]` → none; explicit list; `mcp-*` glob.
- `can_use_tool`: tool→toolset mapping, wildcard, glob, denial.
- Fail-closed: undefined role name → unauthorized, no toolsets, error logged.

**Enforcement integration tests:**
- Gate A: RBAC active + roleless → denied; assigned → allowed; RBAC inactive →
  falls back to `SLACK_ALLOWED_USERS`; `SLACK_ALLOW_ALL_USERS` ignored when active.
- Filter B: assembled `enabled_toolsets` equals role-allowed ∩ configured;
  `chat_only` → empty; cache key varies by role (no cross-user leakage).
- Backstop C: `pre_tool_call` denies a forbidden tool, allows a permitted one,
  allows when no user identity is in contextvars.

**Edge-case / regression tests:**
- `SLACK_ALLOWED_USERS` ignored + one-time info log when RBAC active.
- Pairing offer suppressed under active RBAC.
- Delegation/sandbox: a forbidden tool invoked from a delegated sub-task is still
  blocked (proves contextvar propagation).
- MCP glob: a dynamically registered `mcp-<server>` tool is gated correctly.
- Backward-compat regression: with no RBAC config, message gate, toolset, and
  dispatch behave exactly as before.

## Key files

| Concern | File |
| --- | --- |
| New policy module | `gateway/tool_access.py` (new) |
| Reference pattern | `gateway/slash_access.py` |
| Message gate (A) | `gateway/run.py` (`_is_user_authorized`, ~6391) |
| Toolset filter (B) | `model_tools.py` (`get_tool_definitions`, ~264) + per-run assembly |
| Execution backstop (C) | `model_tools.py` (`pre_tool_call` hook, ~784) |
| Tool→toolset lookup | `tools/registry.py` (`ToolEntry.toolset`) |
| Config bridging | `gateway/config.py` (extra-key bridge, ~849) |
| Identity contextvars | `gateway/session_context.py` (`set_session_vars`, `get_session_env`) |
| Policy unit tests | `tests/gateway/test_tool_access.py` (new) |
