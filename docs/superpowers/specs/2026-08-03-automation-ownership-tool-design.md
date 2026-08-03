# Automation ownership without a shell — the `ownership` tool

**Date:** 2026-08-03
**Status:** implemented
**Related:** [2026-06-30-automation-ownership-design.md](2026-06-30-automation-ownership-design.md),
[2026-05-31-slack-tool-rbac-design.md](2026-05-31-slack-tool-rbac-design.md),
[2026-06-30-cron-rbac-toolset-ceiling-design.md](2026-06-30-cron-rbac-toolset-ceiling-design.md)

## Problem

Automation ownership (`agent/automation_ownership.py`) is administered exclusively through
`hermes own` — a CLI. Reaching a CLI from a messaging platform requires the `terminal`
toolset, which by design only `admin` holds (`operator` deliberately excludes it so an
operator cannot read host secrets). So every ownership operation is admin-only in practice,
and three things break:

1. **The claim nudge dead-ends.** `_claim_nudge` tells the agent to offer
   `hermes own claim <key>` when an unowned automation is edited. A non-admin user cannot
   run it, so the offer is noise — the exact case the ownership feature was built for
   (legacy items with no owner) is the case a normal user cannot resolve.
2. **An owner cannot hand off their own work.** Offboarding, or rotating who maintains a
   cron, requires an admin with shell access.
3. **A user cannot see what they own,** or find out who owns something before editing it —
   so the cross-user confirmation gate fires without the user knowing who to ask.

A fourth problem is latent in the current code rather than caused by the CLI-only surface:
`add_collaborator` / `remove_collaborator` perform **no permission check at all**. Anyone
who reaches them can add themselves as a collaborator on any automation and thereafter edit
it with no cross-user gate and no notification to the owner. Today that requires a shell, so
the blast radius is admins only — but any non-CLI surface must not inherit it.

## Non-goals

* Making ownership a security boundary. It is not one and does not become one here. RBAC
  (`gateway/tool_access.py`) remains the real tool-access boundary; ownership stays an
  awareness + collaboration layer, soft-gated and fail-open.
* Replacing or deprecating `hermes own`. The CLI remains the admin override path and the
  only way to scaffold a bundle (`hermes own init`).
* A `/own` slash command. Viable later as a thin wrapper, but it is gated by
  `slash_access` (`user_allowed_commands`) — a *different* config axis from RBAC roles —
  so shipping it now would mean maintaining two lists to express one permission, and it
  cannot participate in the mid-edit nudge.

## Approach

Add a single agent tool, `ownership`, in its own `ownership` toolset, and make that toolset
a **floor** toolset. The agent can then act on the nudge in the same turn it fires, which is
where the need actually arises.

Rejected alternatives:

* **Grant `ownership` to `operator` only.** Tighter, but a custom role that grants `cronjob`
  or `skills` and forgets `ownership` reproduces the dead-end bug this fixes.
* **Auto-claim on first edit.** Cheapest, but silently makes whoever touches a legacy
  automation its owner, including someone editing a teammate's unclaimed work. Ownership
  should stay an explicit act.

## Tool surface

`tools/ownership_tool.py` registers one tool, `ownership`, toolset `ownership`. It is
cross-platform: identity comes from session contextvars, which every platform populates.

| action | args | permitted to |
|---|---|---|
| `list` | `user` (optional) | self; passing `user` is admin-only |
| `show` | `key` | any valid-role user |
| `claim` | `key` | anyone, **only when the automation is unowned** |
| `transfer` | `key`, `to_user` | current owner, or an admin |
| `collab_add` | `key`, `user` | current owner, or an admin |
| `collab_remove` | `key`, `user` | current owner, or an admin |

`key` is the `kind:id` string the nudge and gate messages already print — `cron:9f3a1c2b`,
`skill:weekly-report`, `script:reports/weekly.py`, `automation:weekly-report`. It is
validated against those four kinds so a malformed key fails loudly instead of writing a
junk record.

The tool is registered with `check_fn=is_enabled`, so when `automation_ownership.enabled`
is false it does not appear to the model at all.

### Target-user resolution

`to_user` / `user` accept a platform user id **or** a human name, resolved against the
platform's `user_names` and `user_roles` maps (already bridged into the platform `extra` by
`gateway/config.py`). Slack mention syntax (`<@U123>`) and a leading `@` are stripped first.

* Exact user-id match wins.
* Otherwise a case-insensitive display-name match. Two or more matches → error naming the
  candidates, no write.
* No match → error. Ownership records pointing at a typo'd id are invisible garbage: the
  owner is never notified, and the cron toolset ceiling silently stops resolving a role
  for that job.
* If the platform has **no** `user_names` and **no** `user_roles` (RBAC never configured),
  there is no directory to validate against, so a raw id is accepted verbatim. Consistent
  with the rest of the fork: inert when unconfigured.

## Permission model

The acting identity is read **only** from `current_identity()` — never from tool arguments.
This is the material difference from the CLI, whose `--user` / `--by` flags let a caller act
as anyone; the tool exposes no such affordance.

* **No identity** — cron, delegation, autonomous runs — refuses every action, mutating or
  not, mirroring `check_edit`'s `NO_IDENTITY` stance. Adding `ownership` to
  `FLOOR_TOOLSETS` also admits it into the cron ceiling universe
  (`cron/rbac_ceiling.apply_cron_toolset_ceiling`); it is inert there by construction, which
  is the intended outcome and is covered by a test.
* **Admin** is `"*" in policy.grant_for(user_id, chat_id)` via
  `tool_access.policy_for_platform`, the same resolution `cron/rbac_ceiling.cron_owner_grant`
  uses, with `chat_id` from `HERMES_SESSION_CHAT_ID` so a `channel_roles` grant counts.
  **RBAC disabled → nobody is an admin**, so only owners can transfer and the CLI stays the
  override. Fail-closed: any error resolving the policy yields "not admin".
* **Enforcement lives in `agent/automation_ownership.py`, not the tool.**
  `add_collaborator` / `remove_collaborator` gain `by` and `by_is_admin` parameters and
  raise `PermissionError` for a non-owner, exactly as `transfer` already does. That closes
  the unchecked-mutation hole in one place for every caller. `hermes own collab` passes
  `by_is_admin=True` — a shell caller is an admin by definition — so the CLI behaves
  exactly as it does today.

### Accepted risk: claiming an unowned automation

Because `ownership` is a floor toolset, any valid-role user can `claim` an unowned
automation that is not theirs. This is accepted:

* Unowned automations are already editable by everyone with no gate at all, so a claim adds
  a gate rather than removing one.
* Every claim writes an audit line, so a land-grab is visible.
* An admin can undo it with `transfer`.

Roleless / undefined-role users get nothing, as with every floor toolset.

## Messages, notification, audit

* `_claim_nudge` stops citing the CLI and points at the tool — this is the user-visible bug.
* `claim()`'s already-owned `PermissionError` stops telling the user to run
  `hermes own transfer`; it names the owner and says to ask them or an admin.
* `_cross_user_message` gains one clause: ask the owner to add you as a collaborator.

Mutations reuse the existing `_send_dm` + `agent.data_access_audit.record_access` plumbing,
best-effort and never blocking the write (same contract as `record_and_notify`):

| action | audit | DM |
|---|---|---|
| `claim` | `automation_claim` | — (nobody to notify) |
| `transfer` | `automation_transfer` | previous owner and new owner |
| `collab_add` | `automation_collab_add` | the added collaborator |
| `collab_remove` | `automation_collab_remove` | the removed collaborator |

## Wiring

* `toolsets.py` — new `ownership` toolset entry beside `notion` / `jira` / `slack_post`.
  Tool modules self-register via `registry.discover_builtin_tools`, so no import list to
  update.
* `gateway/tool_access.py` — `FLOOR_TOOLSETS` gains `ownership`, with a comment giving the
  rationale (awareness/UX, not privilege; the tool self-enforces owner-only mutations).
  `hermes tools rbac` picks this up automatically via `hermes_cli/tools_list.py`.

## Deployment

RBAC's floor grant only ever *intersects* with the toolsets a platform actually offers, so
on an install that pins an explicit `platform_toolsets.<platform>` list — as the VM does —
`ownership` must be added to that list or the tool is silently absent from the model's tool
set no matter what the role allows. Added to `platform_toolsets.slack` and
`platform_toolsets.cli` in the checked-out `config.yaml`; **the VM's live
`~/.hermes/config.yaml` needs the same two lines.**

`user_names` must be populated for name-based targeting ("transfer this to Bob") to work;
without it the tool still accepts raw user ids. `hermes users add <id> <role> --name` writes
both maps.

## Testing

* `tests/tools/test_ownership_tool.py` — the permission matrix (owner × admin × other ×
  no-identity, per action), target resolution including unknown and ambiguous names, key
  validation, claim-on-already-owned, and the tool's disabled short-circuit. Most of these
  stub `_user_directory`, so one test deliberately un-stubs it and drives the real lookup
  off a config file on disk — that gap is what hid the `user_names` bridging bug.
* `tests/agent/test_automation_ownership_decision.py` — regression: non-owner
  `add_collaborator` / `remove_collaborator` now raise `PermissionError`; owner and admin
  still succeed.
* `tests/gateway/test_config.py` — `user_names` reaches `PlatformConfig.extra`, with
  numeric ids stringified.
* `tests/gateway/test_tool_access.py` — `ownership` is in `FLOOR_TOOLSETS` and reaches a
  `chat_only` user but not a roleless one.
* `tests/cron/test_rbac_ceiling.py` — the ceiling admits `ownership` as a floor toolset,
  and the tool refuses to mutate without an identity.

Beyond the unit tests, the flow was exercised end to end against a throwaway `HERMES_HOME`
with a real RBAC config: an `operator` (no `terminal`) claiming, being refused a
cross-user collaborator add, adding a teammate by name, transferring via `<@U…>` mention
syntax, an admin overriding, and a typo'd target being rejected — with the resulting
registry and audit trail inspected on disk. That run is what surfaced the `user_names` bug.
