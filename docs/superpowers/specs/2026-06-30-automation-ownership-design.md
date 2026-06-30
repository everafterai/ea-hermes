# Automation Ownership & Cross-User Edit Protection — Design

**Date:** 2026-06-30
**Status:** Approved design, pre-implementation
**Topic:** Record who owns each user-built automation (skill, cron, script, automation
bundle), and make the agent surface a code-enforced confirmation gate — plus an owner
notification — when one team member edits another's automation.

## Problem

The fork runs **one gateway for the whole team over Slack**, and any identified user
can create skills, cron jobs, and scripts. Today none of those artifacts records *which
human built it*:

- **Skills** live at `~/.hermes/skills/<category>/<name>/SKILL.md`. Frontmatter accepts
  arbitrary `metadata.hermes.*` and an `author` field, and a `.usage.json` sidecar
  tracks `created_by: "agent"` + `created_at` — but **never which platform user**
  (`tools/skill_usage.py`, `tools/skill_manager_tool.py`).
- **Cron jobs** live in one shared `~/.hermes/cron/jobs.json`. Each job carries an
  `origin: {platform, chat_id, chat_name, thread_id}` and `created_at`, but **no
  explicit user owner** (`cron/jobs.py:672`, `tools/cronjob_tools.py:269`
  `_origin_from_env`).
- **Scripts** live flat in `~/.hermes/scripts/`, referenced by crons via relative path.
  They have **no metadata container at all**.

So when Bob asks the agent to "tweak the weekly-report cron" that Alice built, the agent
has no way to know it is Alice's, no way to pause and confirm, and no way to let Alice
know it changed. Collisions are silent. The team wants **awareness and a deliberate
confirmation step**, governed by code (not by the agent's goodwill), without turning
ownership into a hard RBAC permission wall.

The fork already has every primitive needed to build this:

- **Identity** flows through session contextvars — `HERMES_SESSION_USER_ID`,
  `_USER_NAME`, `_PLATFORM`, `_CHAT_ID` (`gateway/session_context.py`) — and the RBAC
  backstop already resolves "who is acting" at tool-execution time
  (`gateway/tool_access.py`).
- **Audit** exists: `agent/data_access_audit.record_access()` appends JSONL to
  `${HERMES_HOME}/audit/data-access.log`, best-effort, never raising.
- **Notification** exists: `send_message` (`tools/send_message_tool.py`) can open a Slack
  DM (`conversations.open`) or post to a channel.
- **System-prompt injection** has an established conditional-block pattern in
  `agent/system_prompt.py::build_system_prompt_parts()` (`MEMORY_GUIDANCE`,
  `SKILLS_GUIDANCE`, `KANBAN_GUIDANCE`, …), appended to the cached `stable` segment.
- **Self-documenting tool denials** are an established pattern: `file_safety.py`'s
  `_PROTECTED_DATA_MSG` is returned as a tool result and the model reads it verbatim as
  instruction.

## Goals

- **Record ownership** for every user-built automation: skills, cron jobs, scripts, and
  the new automation bundles. Owner is auto-assigned to the **creator** (from session
  identity) at create time. Each automation also carries an optional **collaborators**
  list.
- **Code-enforced soft gate on cross-user edits.** When an identified user who is not the
  owner or a collaborator tries to edit someone else's automation, the *first* attempt is
  refused by code with a self-documenting warning; it proceeds only on an explicit
  acknowledged re-invocation. This is real, code-governed friction at the tool chokepoint
  — not a prompt the agent can quietly skip. **Admins are not exempt** (they hit the same
  gate; the edit is still logged and the owner still notified).
- **Notify the owner** via Slack DM after a confirmed cross-user edit actually lands
  (best-effort; falls back to audit-log-only when no DM target).
- **Unowned legacy items: warn + offer to claim.** Editing an automation that has no
  recorded owner proceeds, but the result nudges the user to claim it (using existing
  provenance — cron `origin`, skill `author` — as a hint for who probably owns it).
- **Always-present agent guidance from code.** A short ownership instruction is injected
  into the **system prompt** (cached `stable` segment) so it is present on every turn,
  independent of whether any skill was loaded. **No `~/.hermes/SOUL.md` editing.**
- **Optional automation bundles** — a deliberate `~/.hermes/automations/<name>/` home for
  building a multi-part automation (manifest + workflow doc + scripts + assets) as one
  owned unit, without migrating any existing skill/script/cron.
- **Backward compatible / fail-open.** Absent config → feature disabled, exact upstream
  behavior. A malformed registry or missing identity degrades to "no ownership data"
  (everything reads as unowned, no gate) rather than blocking work.

## Non-goals (YAGNI / explicit decisions)

- **Not** an RBAC permission boundary. Ownership is an *awareness + collaboration* layer.
  The hard tool-access boundary stays `gateway/tool_access.py`. A user who can already
  invoke the skill/cron/file toolset can still edit cross-user automations — just not
  *silently* or *unacknowledged*.
- **Not** a guarantee that a *human* reviewed each cross-user edit. The gate guarantees a
  **deliberate two-step, code-enforced** acknowledgement and an owner notification + audit
  record. In interactive contexts (Slack/CLI) the warning surfaces to humans and the
  confirmation is human-driven; a fully autonomous agent acting *as an identified user*
  could supply the acknowledgement itself — that residual case is covered only by the
  visible warning + owner DM + audit trail, not a hard block. (Autonomous runs with **no**
  identity — e.g. cron — cannot acknowledge at all and are refused; see Design §3.)
- **Not** tamper-proof. The registry and audit log are local files writable by the same
  uid that runs the gateway. Designed to catch accidental/collegial collisions and make
  edits visible, not to survive an adversary who owns the box.
- **Not** forcing existing skills/scripts/crons into bundles. Bundles are opt-in for new
  work; the overlay covers everything that exists today in place.
- **Not** gating non-destructive operations: listing, viewing, or `trigger` (run-now) of a
  cron are ungated. Only create/edit/patch/delete and pause/disable go through the gate.

## Approach

A single **ownership module** owns the registry and all decision logic; thin hooks at the
existing edit chokepoints call it; the agent guidance and owner-notification reuse
existing primitives.

1. `agent/automation_ownership.py` — pure, unit-testable. Registry I/O + `check_edit`
   decision + claim/transfer/collaborator ops + the artifact-key/path classification.
2. Hooks at each edit chokepoint (skill tool, cron tool/CLI, raw file writes) call
   `check_edit` before mutating and register the creator on create.
3. `AUTOMATION_OWNERSHIP_GUIDANCE` appended to the cached `stable` system-prompt segment.
4. Owner DM via `send_message`; cross-user edits recorded via the existing
   `data_access_audit` log with a new action.
5. Optional `~/.hermes/automations/<name>/` bundles, indexed into the same registry.

Chosen over (a) a hard RBAC extension (the user explicitly wants a soft gate, not a wall)
and (b) a full bundle migration (upstream-merge risk; the overlay covers existing
artifacts in place). The registry is **canonical** for lookups so "what does Alice own?",
the claim flow, and the audit have one source of truth; artifacts with a natural metadata
slot also get a human-visible mirror written through the same module.

## Design

### 1. Ownership registry — `~/.hermes/ownership/registry.json`

One JSON file, written atomically (temp + `os.replace`), dir `0o700` / file `0o600`,
mirroring the cron storage hardening (`cron/jobs.py:159`). Path overridable via config;
resolved through `hermes_constants.get_hermes_home()` (profile-aware — each profile has its
own registry, like cron).

```jsonc
{
  "version": 1,
  "updated_at": "2026-06-30T…Z",
  "automations": {
    "skill:weekly-report": {
      "kind": "skill",
      "owner":         { "platform": "slack", "user_id": "U01ABC", "display_name": "Alice" },
      "collaborators": [ { "platform": "slack", "user_id": "U02DEF", "display_name": "Bob" } ],
      "created_at": "…", "updated_at": "…",
      "source": "creator",          // creator | claim | transfer
      "notify": true                 // owner DM on cross-user edit; per-item override
    },
    "cron:9f3a1c2b7e10":        { … },
    "script:reports/weekly.py": { … },
    "automation:weekly-report": { … }
  }
}
```

**Artifact key** = `<kind>:<stable-id>`:

| Kind         | Stable id                              | Source of the id |
|--------------|----------------------------------------|------------------|
| `skill`      | skill `name` (unique across dirs)      | frontmatter `name` |
| `cron`       | job id (existing 12-char uuid)         | `cron/jobs.py` |
| `script`     | path relative to `~/.hermes/scripts/`  | file path |
| `automation` | bundle dir name under `automations/`   | dir name |

Identity record uses `user_id` as the **stable** match key; `display_name` is cosmetic and
refreshed on every write. The registry is **canonical**; where a natural slot exists
(cron job field, skill frontmatter `metadata.hermes.owner`, bundle `automation.yaml`) the
module also stamps a human-visible mirror so ownership is legible in the artifact itself,
written through the same code path so it cannot drift silently (registry wins on conflict).

### 2. Ownership module — `agent/automation_ownership.py`

Pure and unit-testable, a sibling to `tool_access.py` / `file_safety.py`. It is the only
writer of the registry and the single home for the decision logic:

```python
@dataclass(frozen=True)
class Identity:
    platform: str
    user_id: str
    display_name: str

class EditDecision(Enum):
    OWNER          # acting identity is the owner            -> allow, silent
    COLLABORATOR   # acting identity is a collaborator       -> allow, silent
    UNOWNED        # no owner recorded                       -> allow, append claim nudge
    CROSS_USER     # owned by someone else                   -> refuse until acknowledged
    NO_IDENTITY    # no human identity + item is owned       -> refuse + log

def current_identity() -> Identity | None        # reads session contextvars; None if absent
def artifact_key(kind: str, stable_id: str) -> str
def path_to_artifact_key(path: str | Path) -> tuple[str, str] | None   # (key, kind) for files under scripts/ skills/ automations/
def get_record(key: str) -> dict | None
def check_edit(key: str, identity: Identity | None, *, acknowledged: bool) -> EditResult
def register_creator(key: str, kind: str, identity: Identity | None) -> None  # on create
def claim(key: str, kind: str, identity: Identity) -> None
def add_collaborator(key: str, ident: Identity) -> None
def transfer(key: str, new_owner: Identity, *, by: Identity) -> None
def record_and_notify(key: str, editor: Identity, record: dict) -> None       # audit + owner DM
```

`check_edit` returns an `EditResult { decision, message }` where `message` is the
self-documenting string surfaced to the model on a refusal (channel #1). Decision table:

| Acting identity vs record         | Item owned? | `acknowledged` | Result |
|-----------------------------------|-------------|----------------|--------|
| owner / collaborator              | yes         | —              | **allow** (silent) |
| other identified user             | yes         | false          | **refuse** — CROSS_USER warning |
| other identified user             | yes         | true           | **allow** → `record_and_notify` |
| any identified user               | no (unowned)| —              | **allow** + claim nudge in result |
| no identity (autonomous)          | yes         | —              | **refuse** — NO_IDENTITY (can't acknowledge) + log |
| no identity (autonomous)          | no (unowned)| —              | **allow** (silent) |

Note role is **absent** from the table — admins hit CROSS_USER like everyone else.

### 3. Enforcement surface — the hooks

Every path that mutates an automation calls `check_edit` before writing, and
`register_creator` after a successful create. The **chokepoints**:

| Edit path | Hook location | Notes |
|---|---|---|
| `skill_manage` create / edit / patch / delete / write_file / remove_file | `tools/skill_manager_tool.py` (~485–539; create already calls `mark_agent_created`, ~894) | create → `register_creator("skill:"+name)` |
| Cron edit / remove / pause(disable) | `tools/cronjob_tools.py` (~459–650) + `cron/jobs.py` (`update_job`/`remove_job`/`pause_job`, ~550–712) | create (`create_job`) → `register_creator("cron:"+id)`; `trigger`/`list` ungated |
| **Raw file** write / patch / edit under `scripts/`, `skills/<name>/`, `automations/<name>/` | `tools/file_tools.py` / `tools/file_operations.py` write+patch paths | classify via `path_to_artifact_key`; covers skill-file and script edits that bypass the dedicated tools |
| CLI | `hermes cron edit/remove`; new `hermes own` command | CLI prompts interactively for the confirmation |

**The soft-gate mechanism (code-enforced two-step).** On `CROSS_USER`, the hook returns
the warning string instead of mutating — e.g.:

> ⚠️ `cron weekly-report` is owned by **Alice** (slack). You are not an owner or
> collaborator. Confirm with the user, then re-invoke with
> `confirm_cross_user_owner="Alice"` to proceed. (To collaborate instead, ask Alice to add
> you; to claim an unowned automation use `hermes own claim`.)

Each mutating tool gains an optional `confirm_cross_user_owner` parameter (the file
write/patch tool included), accepting **either** the recorded owner's `display_name`
(what the warning shows) **or** their stable `user_id`. The hook calls
`check_edit(..., acknowledged=bool(confirm and confirm matches the recorded owner's
display_name or user_id))`. With a matching acknowledgement the edit proceeds
and `record_and_notify` fires. The two-step is enforced in code: the first attempt cannot
mutate. The agent's role is only to surface the warning and obtain the human's "yes" before
re-invoking (driven by the system-prompt guidance, §5); in CLI the prompt is genuinely
interactive.

**`UNOWNED`** edits proceed (low friction for legacy items) but the tool result appends a
one-line claim nudge; the guidance tells the agent to offer the claim, using `origin`/
`author` as a hint for the likely owner. **`NO_IDENTITY`** (autonomous run, no human, item
owned) is refused and logged — an unattended job cannot silently rewrite a human's owned
automation.

### 4. Optional automation bundles — `~/.hermes/automations/<name>/`

A deliberate home for building a multi-part automation as one owned unit. Nothing existing
is moved; this is purely additive.

```
~/.hermes/automations/weekly-report/
  automation.yaml      # owner, collaborators, description, links → skill / cron ids / scripts
  workflow.md          # human-readable: what it does, how the pieces fit, runbook
  scripts/             # bundle-local scripts
  assets/              # data, templates, fixtures
```

`automation.yaml` is the human-facing manifest; on create/edit the module mirrors its
`owner`/`collaborators` into the registry under `automation:<name>` (registry stays
canonical). Editing anything inside `automations/<name>/` resolves to that bundle's owner
via `path_to_artifact_key` on the same file-tool chokepoint. A new `hermes own init <name>`
scaffolds the skeleton and registers the creator. Crons and skills may reference a bundle
by name (documentation only in v1 — no execution change).

### 5. Always-present agent guidance — system prompt (no SOUL.md edit)

Add a module-level constant `AUTOMATION_OWNERSHIP_GUIDANCE` and append it to
`stable_parts` in `agent/system_prompt.py::build_system_prompt_parts()` (the same place and
manner as `SKILLS_GUIDANCE`/`MEMORY_GUIDANCE`, ~lines 111–201), gated on
`automation_ownership.enabled` **and** the presence of any automation-editing tool in
`agent.valid_tool_names` (skill/cron/file). Because it lands in the **`stable`** segment, it
is built once per session, persisted, and replayed byte-stable every turn — present on
every turn regardless of which skills load, and respecting the prompt-cache invariant
(`conversation_loop.py:1114`; only context compression invalidates). It does **not** read or
write `~/.hermes/SOUL.md` (`agent/prompt_builder.py:1401` `load_soul_md` is untouched).

The guidance is short and behavioral, e.g.: *"Skills, cron jobs, scripts, and automation
bundles may be owned by a specific teammate. When a tool reports an automation is owned by
someone else, relay the warning and get the user's explicit confirmation before re-invoking
with the confirmation flag — never confirm on their behalf. When a tool reports an
automation is unowned, offer to claim it. Owners and collaborators edit freely."*

A bundled `automation-ownership` skill (repo `skills/`) carries the deeper reference (how to
transfer ownership, add collaborators, build a bundle) — **optional**, not load-bearing.

### 6. Owner notification + audit

`record_and_notify(key, editor, record)` runs only when a cross-user edit is **confirmed and
lands**:

1. **Audit** — append a JSONL event to the existing `${HERMES_HOME}/audit/data-access.log`
   via `data_access_audit.record_access(tool=…, action="automation_edit", target=key)`,
   carrying the editor identity fields the module already logs.
2. **Owner DM** — if `record["notify"]` and the owner is on a DM-capable platform, send via
   `send_message` (Slack `conversations.open` → DM): *"⚠️ Bob edited your cron
   `weekly-report`. — via Hermes"*. Best-effort and **fail-open**: no reachable DM, a
   cross-platform owner, or a send error degrades to audit-log-only and never blocks or
   reverses the edit.

### 7. Configuration — top-level `automation_ownership:` block

Loaded by `gateway/config.py::load_gateway_config()`, same pattern as `data_access_audit:`:

```yaml
automation_ownership:
  enabled: true          # default true; soft-gate + registry + guidance active
  notify_owner: true     # DM owner on confirmed cross-user edit
  registry_path: "${HERMES_HOME}/ownership/registry.json"   # optional override
```

`enabled: false` → no gate, no guidance injection, exact pre-feature behavior. `notify_owner:
false` → gate + audit still apply, no DM. Fail-open: malformed block or unreadable registry
logs once and degrades to "no ownership data" (everything unowned, no gate).

### 8. Ownership management — `hermes own` CLI + tool

A small surface for the explicit (non-edit) ownership operations:

```
hermes own list [--user <id>]            # what a user owns / collaborates on
hermes own claim <key>                   # claim an unowned automation for yourself
hermes own transfer <key> <user-id>      # owner (or admin) reassigns ownership
hermes own collab add|remove <key> <id>  # manage collaborators
hermes own init <name>                   # scaffold an automation bundle, register creator
```

`transfer` is the one place a role matters: the current **owner** may transfer/release their
own automation, and an **admin** may reassign any (for offboarding) — this manages the
*registry*, distinct from editing automation *content* (which always hits the gate).

## Testing (via `scripts/run_tests.sh`)

- **Module** (`tests/.../test_automation_ownership.py`): `check_edit` truth table — owner /
  collaborator / unowned / cross-user(unacked) / cross-user(acked) / no-identity-owned /
  no-identity-unowned; `claim` / `transfer` / `add_collaborator`; `artifact_key` and
  `path_to_artifact_key` for each kind; registry atomic write + corrupt-file tolerance
  (degrades to unowned, no raise).
- **Hooks (integration):** a cross-user `skill_manage` edit and a cross-user `cron edit` are
  **refused** without `confirm_cross_user_owner` and **succeed** with a matching one; a raw
  file `patch` to another user's `scripts/foo.sh` is gated via `path_to_artifact_key`; a
  create registers the creator as owner; an unowned edit succeeds and returns the claim
  nudge; an autonomous (no-identity) edit of an owned item is refused; trigger/list are
  ungated.
- **Notification:** owner DM attempted on confirmed cross-user edit with a stubbed
  `send_message`; falls back to audit-log-only when no DM target; `notify_owner: false`
  sends nothing; one well-formed `action: "automation_edit"` JSONL line is written.
- **System prompt:** `AUTOMATION_OWNERSHIP_GUIDANCE` present in `stable` when enabled + an
  editing tool is available; absent when `enabled: false` or no editing tool; lands in
  `stable` (cache-safe), never `volatile`.
- Honors the autouse `HERMES_HOME` redirect — **no writes to the real `~/.hermes`**; no
  change-detector tests.

## Files touched

- `agent/automation_ownership.py` — **new**: registry I/O, `Identity`, `check_edit`,
  claim/transfer/collaborator ops, `path_to_artifact_key`, `register_creator`,
  `record_and_notify`.
- `agent/system_prompt.py` — add `AUTOMATION_OWNERSHIP_GUIDANCE`; conditional append to
  `stable_parts` in `build_system_prompt_parts()`.
- `tools/skill_manager_tool.py` — gate edit/patch/delete/write_file/remove_file; register
  creator on create; add `confirm_cross_user_owner` param.
- `tools/cronjob_tools.py`, `cron/jobs.py` — gate edit/remove/pause; register creator on
  `create_job`; add `confirm_cross_user_owner` param.
- `tools/file_tools.py`, `tools/file_operations.py` — gate write/patch/edit on paths under
  `scripts/` / `skills/<name>/` / `automations/<name>/`; add `confirm_cross_user_owner` param.
- `tools/send_message_tool.py` — reused (no change) for the owner DM, called from
  `automation_ownership.record_and_notify`.
- `agent/data_access_audit.py` — reused; new `action: "automation_edit"`.
- `gateway/config.py` — surface the `automation_ownership` block.
- `hermes_cli/` — new `hermes own` command (list/claim/transfer/collab/init).
- `skills/automation-ownership/SKILL.md` — **new** optional reference skill.
- `tests/` — module, hook, notification, and system-prompt tests per above.
