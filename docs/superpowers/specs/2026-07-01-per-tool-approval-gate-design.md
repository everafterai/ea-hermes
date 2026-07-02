# Config-Driven Per-Tool Approval Gate — Design

**Date:** 2026-07-01
**Status:** Approved design, pre-implementation
**Topic:** Let config name specific tools (native or MCP) that require human
approval before each invocation; roll it out to gate Stripe write tools over the
hosted Stripe MCP server.
**Related:** [Slack Tool RBAC design](2026-05-31-slack-tool-rbac-design.md),
[Cron RBAC Toolset Ceiling design](2026-06-30-cron-rbac-toolset-ceiling-design.md),
[Automation ownership design](2026-06-30-automation-ownership-design.md),
[Cross-user data-access protection design](2026-06-29-cross-user-data-access-protection-design.md)

## Problem

Approval today is **content-matched and surface-bound**. The guards in
[tools/approval.py](../../../tools/approval.py) — `check_dangerous_command`,
`check_all_command_guards`, `check_execute_code_guard` — match a **command/code
string** against `HARDLINE_PATTERNS` / `DANGEROUS_PATTERNS` regexes (or, in
`smart` mode, an LLM classifies the command). They are called only from
[tools/terminal_tool.py](../../../tools/terminal_tool.py),
[tools/code_execution_tool.py](../../../tools/code_execution_tool.py), and the
file-write paths.

There is **no way to say "this *tool* requires approval."** In particular, MCP
tool calls never pass through any approval flow — the dispatch path
([model_tools.py](../../../model_tools.py), [tools/registry.py](../../../tools/registry.py),
[tools/mcp_tool.py](../../../tools/mcp_tool.py)) has no `require_approval` hook for
arbitrary tools. The only per-tool control is RBAC
([gateway/tool_access.py](../../../gateway/tool_access.py)), which is binary
(may / may not invoke), not "may invoke, with a human confirming each time."

The motivating case: wiring Stripe's hosted MCP server (`https://mcp.stripe.com`)
gives the agent `stripe_api_write` and `create_refund` — **money movement** — with
its blast radius bounded only by the restricted API key. We want a human confirm on
those calls in interactive contexts, config-selected, without hard-coding anything
Stripe-specific into core. The mechanism should work for **any** tool (native or
MCP) so future sensitive tools reuse it.

## Goals

- Config lists tool names (globs) that require approval before each call.
- Works for **every** tool the dispatcher sees: native, MCP, delegation sub-agents,
  the code-execution sandbox — the same coverage as the RBAC execution backstop.
- Interactive surfaces (CLI, TUI, Slack/gateway) prompt with the existing
  approval UX (**Allow once / Allow session**); no new UI.
- Headless/cron does **not** break legitimate automations: a job authorized to run
  a gated tool runs it; an unauthorized/ownerless job does not silently do so.
- Backward compatible: absent config → byte-for-byte current behavior.

## Non-goals

- Not a replacement for RBAC or for a tightly-scoped credential. With Stripe write
  granted to `operator` on a **live** key, the `rk_live_` key's scope remains the
  real blast-radius boundary; this gate and RBAC are layers on top.
- No per-argument policy language (e.g. "approve refunds over $100"). The unit is
  the tool name. Argument *display* is redacted, not evaluated.
- No change to the existing command/code content guards; they keep running
  independently, governed by `approvals.mode`.

## Design

### 1. Config schema

Extend the existing `approvals:` block (read by `_get_approval_config` at
[tools/approval.py:905](../../../tools/approval.py)):

```yaml
approvals:
  mode: manual                 # existing — governs the command/code content guards
  timeout: 30                  # existing — reused for the tool-approval prompt
  cron_mode: deny              # existing — reused for the cron fallback (see §5)
  require_for_tools:           # NEW — tool-name globs that require approval
    - stripe_api_write
    - create_refund
    - "send_*"
```

`require_for_tools` is a list of `fnmatch` globs matched (case-insensitively)
against the resolved tool name. Absent or empty → the gate is **inert** (no code
path changes for existing installs). It is **orthogonal** to `approvals.mode`:
`mode` governs the command/code content guards; `require_for_tools` governs named
tools. Setting `mode: off` does **not** disable `require_for_tools` (documented
explicitly, since it is a plausible surprise).

### 2. Enforcement point

A new function in [tools/approval.py](../../../tools/approval.py):

```python
def check_tool_approval(tool_name: str, args: dict, session_key: str) -> dict:
    """Return one of:
      {"approved": True,  "message": None}                       # not gated / already approved / cron-skip
      {"approved": False, "status": "approval_required", ...}    # interactive: ask the user
      {"approved": False, "message": "BLOCKED: ..."}             # denied (user said no, or cron deny)
    """
```

It is called from the **`pre_tool_call` path** in
[model_tools.py](../../../model_tools.py) (~line 1012–1057), right beside the RBAC
backstop `denial_for_current_tool` (~line 1056). This point fires **exactly once
per tool execution** for every tool — native, MCP, delegation, sandbox — and is the
same place the RBAC backstop resolves identity, so the gate inherits identical
coverage.

**Ordering.** RBAC hard-deny runs **first**: if the role may not invoke the tool at
all, we deny without prompting. Only if RBAC allows does the approval gate run. This
keeps "may you" (RBAC) strictly ahead of "confirm you" (approval).

**Single-fire.** The gate participates in the existing single-fire `pre_tool_call`
contract; it must not double-prompt when the hook and the backstop both run.

The result-dict contract mirrors `check_dangerous_command`
([tools/approval.py:1055–1091](../../../tools/approval.py)) exactly, so:

- An `approval_required` return is surfaced **as the tool's result**, short-circuiting
  execution. The gateway/conversation loop already detects `status ==
  "approval_required"` and drives the Slack prompt; `resolve_gateway_approval`
  records the answer; on retry `is_approved(session_key, pattern_key)` short-circuits
  to allow. No new UI, no new loop handling.
- The approval is keyed by `pattern_key = f"tool:{tool_name}"` in the existing
  session/approval store (`submit_pending`, `approve_session`, `is_approved`).

**Load-bearing integration risk (de-risk first in the plan).** Terminal's
`approval_required` surfaces because terminal returns it as its own **tool result**,
and the conversation loop inspects *tool results* for `status == "approval_required"`.
The RBAC backstop at this same `pre_tool_call` point, by contrast, returns a plain
**block message** (a hard denial), not a status-bearing dict that the loop re-drives
into a prompt. So the first implementation step is to confirm (and, if needed, wire)
that an `approval_required` short-circuit returned from the `pre_tool_call` path is
surfaced to the gateway/conversation loop with its `status` intact — i.e. treated
like a terminal approval, not like an RBAC hard-block. If the `pre_tool_call` return
cannot carry `approval_required`, the fallback is to run `check_tool_approval` at the
point where the tool result is produced/returned (so the dict travels the exact
tool-result path terminal uses) rather than in the block-message hook. This choice
determines the insertion mechanics and must be settled before the rest is built.

### 3. Interactive flow (CLI / TUI / Slack)

Reuse the existing prompt, offering **Allow once** and **Allow session**
(`once` / `session`), with **`allow_permanent=False`** — the "always" / on-disk
permanent allowlist option is hidden, following the tirith precedent
([tools/approval.py:775–789](../../../tools/approval.py), `allow_permanent`
parameter). A gated (money-touching) tool must never be persisted to the permanent
allowlist across restarts.

- **Allow once** → `is_approved` is *not* recorded; the next call re-prompts.
- **Allow session** → `approve_session(session_key, "tool:<name>")`; further calls
  of that tool in the session pass silently.
- **Deny / timeout** → `{"approved": False, "message": "BLOCKED: ..."}`; the tool
  result tells the agent the user rejected it and not to retry.

CLI uses `prompt_dangerous_approval(..., allow_permanent=False)`; the gateway path
uses the `approval_required` submit/resolve flow with the once/session choices.

### 4. Prompt content & redaction

The prompt shows the tool name and a **redacted** one-line args summary. Redaction
drops the *value* of any arg key matching a sensitive-key denylist
(`*key*`, `*token*`, `*secret*`, `*password*`, `*authorization*`) and truncates long
values. Stripe write args (`method`, `path`, `body`) are safe to show — the API key
lives host-side in the MCP request headers, never in tool args. Redaction is a
defensive default for arbitrary future tools, not a Stripe requirement.

### 5. Headless / cron — skip when authorized, otherwise fall back

Cron runs with identity contextvars **deliberately cleared** (see
[cron/scheduler.py](../../../cron/scheduler.py) ~1598–1623), so there is no user to
prompt. The gate must not blanket-block (breaks legitimate automations) nor
blanket-allow (silent money movement). Behavior:

1. **Skip the gate (auto-allow)** iff **both** hold:
   - **(a) Standing permission** — the job owner's role grants the tool's toolset.
     Resolved via `cron_owner_grant(job)`
     ([cron/rbac_ceiling.py:32](../../../cron/rbac_ceiling.py)), the same hook the
     cron RBAC ceiling uses. Under the ceiling, a capped job's toolset is already
     intersected with the owner's grant, so a callable gated tool implies the owner
     had permission.
   - **(b) Explicit create-time acknowledgment** — the tool is listed in the job's
     `unattended_approved_tools` (see §6). Standing permission alone is not enough;
     the owner must have explicitly acknowledged unattended execution of this tool.
2. **Otherwise** (no owner, roleless owner, owner lacks the grant, or the tool was
   not acknowledged) → follow the existing `approvals.cron_mode`
   (`_get_cron_approval_mode`, [tools/approval.py:930](../../../tools/approval.py)):
   **deny by default**, `approve` as the operator blanket escape hatch.
3. **Audit either way** — every cron run of a gated tool (skipped or `cron_mode:
   approve`) appends an `approval-gated-unattended` line (owner, job id, tool) to the
   `data_access_audit` trail via
   [agent/data_access_audit.py](../../../agent/data_access_audit.py) `record_access`,
   so unattended sensitive calls stay visible even though they were not blocked.

Net: a cron job *meant* to call `stripe_api_write`, whose owner holds the `stripe`
grant **and** who acknowledged it at creation, just runs; anything else fails closed
by default.

### 6. Create-time acknowledgment

The `cronjob` tool ([tools/cronjob_tools.py](../../../tools/cronjob_tools.py)) gains
an `unattended_approved_tools: [str]` field on the job record. Its RBAC creation
validator (`_rbac_creation_error`) is extended so that, on **create and update**:

- If the job's effective toolsets make any `require_for_tools`-matched tool callable
  and that tool is **not** listed in `unattended_approved_tools`, the job is
  **rejected** before it is persisted or the owner notified — with a message telling
  the creator to acknowledge unattended execution by listing the tool.
- The creator's role must be able to grant the tool's toolset (they cannot
  acknowledge a tool they could not invoke themselves). This reuses the role→toolset
  resolution already in the creation validator.

Mapping a `require_for_tools` tool name to its toolset uses the registry (an MCP
tool `stripe_api_write` resolves to toolset `mcp-stripe`; see
[tools/mcp_tool.py:3365](../../../tools/mcp_tool.py) `toolset_name = f"mcp-{name}"`).
The acknowledgment is stored on the job and surfaced to the owner on the confirmed
edit (the automation-ownership `record_and_notify` path).

### 7. Runtime plumbing

At run start the scheduler already resolves the owner grant (for the ceiling) and
clears identity. It additionally exports, for the duration of the run:

- the resolved owner grant (or a boolean "owner-authorized for toolset X"), and
- the job's `unattended_approved_tools`,

into the run context (a contextvar/env pair, mirroring `HERMES_CRON_SESSION` and the
existing ceiling application) so `check_tool_approval` in `pre_tool_call` can read
them without threading the `job` dict through `model_tools`. In non-cron contexts
these are absent and the interactive path (§3) applies.

## Stripe rollout (first consumer)

Rides on the gate above; mostly ops/config plus one RBAC edit:

- **MCP wiring** — VM `~/.hermes/config.yaml`:
  ```yaml
  mcp_servers:
    stripe:
      url: "https://mcp.stripe.com"
      headers:
        Authorization: "Bearer ${STRIPE_API_KEY}"   # interpolated from ~/.hermes/.env
      timeout: 120
      connect_timeout: 60
      enabled: true
  ```
  Omitting `auth` → no OAuth handshake (headless/cron-safe); static Bearer on every
  request. Stripe's remote MCP has **no read-only header** — access is bounded by the
  key. `~/.hermes/.env`: `STRIPE_API_KEY=rk_live_...`, scoped **least-privilege**
  (write only on the resources actually needed; read/none elsewhere; no Connect /
  payout / key-management scopes). Registers as toolset `mcp-stripe` (alias `stripe`).
- **RBAC** — add `"stripe"` to the `operator` frozenset in `BUILTIN_ROLES`
  ([gateway/tool_access.py:48](../../../gateway/tool_access.py)); `admin` gets it via
  `*`. Chosen over a config `roles:` override because a config redefinition
  **replaces** a role's toolset set (`_coerce_roles`,
  [gateway/tool_access.py:134](../../../gateway/tool_access.py)) and would silently
  fork `operator`'s whole list.
- **Approval** — `approvals.require_for_tools: [stripe_api_write, create_refund]`.
- **Cron** — any job that should call those must list them in
  `unattended_approved_tools`, and its owner must hold the `stripe` grant.
- **Memory note** — document the recipe (mirrors the existing GitHub remote-MCP note).

## Security considerations

- **The restricted key is the real boundary.** operator + live + write is a broad
  posture (operator is reachable by `channel_roles` posters). RBAC controls *who*
  invokes the toolset; the approval gate adds *human confirm* in interactive
  contexts; but the `rk_live_` key's scope caps what is *possible* regardless of
  either. Scope it tightly.
- **Fail-closed in cron by default.** Unauthorized/ownerless/unacknowledged →
  `cron_mode` (deny). `approve` is an explicit operator opt-out, audited.
- **No permanent allowlisting** of gated tools (`allow_permanent=False`).
- **Orthogonal to `mode`.** `mode: off` does not disable `require_for_tools`.
- **Prompt-injection.** Attacker-influenceable text reaching the agent can *propose*
  a gated call, but interactive contexts require a human, and cron requires prior
  explicit acknowledgment by an authorized owner.

## Backward compatibility

- Absent `require_for_tools` → `check_tool_approval` returns `{"approved": True}`
  immediately; no new prompts, no audit writes, current behavior byte-for-byte.
- Absent `unattended_approved_tools` on existing cron jobs → only matters if a job's
  toolset makes a `require_for_tools` tool callable; installs without
  `require_for_tools` are unaffected.
- The command/code content guards and `approvals.mode` are untouched.

## Testing

Via `scripts/run_tests.sh` (CI-parity wrapper):

- **Unit** — glob matching (hit/miss/case), sensitive-key redaction + truncation,
  the three result-dict shapes, once vs session (`is_approved` recorded only for
  session), `allow_permanent=False` hides "always".
- **Cron** — skip when owner-grant + acknowledged; deny when acknowledged but owner
  lacks grant; deny when grant present but not acknowledged; `cron_mode: approve`
  fallback allows + audits; ownerless follows `cron_mode`.
- **Create-time** — `cronjob` create/update rejected when a `require_for_tools` tool
  is callable but unacknowledged; rejected when the creator's role can't grant the
  toolset; accepted when acknowledged + grantable.
- **Integration** — a `require_for_tools` match on an MCP tool returns
  `approval_required`; a resolved gateway approval lets the retry through; RBAC
  hard-deny takes precedence over the approval prompt.
- **Stripe RBAC** — `operator` and `admin` grant `stripe`; `readonly` / `chat_only`
  do not.

## Residual / out of scope

- Per-argument / threshold policies (approve only above an amount) — future work.
- A UI to manage `require_for_tools` — config-only for now.
- The gate is not a boundary against a determined admin with a shell (they can edit
  config); like the ownership and audit systems, it closes the tool-driven path and
  makes the residual visible.
