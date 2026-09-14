# Google Drive per-user access check

**Date:** 2026-09-14
**Status:** approved, not yet implemented
**Related:** [2026-05-31-slack-tool-rbac-design.md](2026-05-31-slack-tool-rbac-design.md),
[2026-06-29-cross-user-data-access-protection-design.md](2026-06-29-cross-user-data-access-protection-design.md),
[2026-06-30-cron-rbac-toolset-ceiling-design.md](2026-06-30-cron-rbac-toolset-ceiling-design.md),
[2026-06-30-automation-ownership-design.md](2026-06-30-automation-ownership-design.md)

## Problem

The bundled `plugins/google_drive_sa/` plugin acts as the **service account itself**:
every Drive/Sheets/Docs call runs on the SA's token, and access is granted by sharing
a file or folder with the SA's email. That makes the SA a *shared* identity — any user
whose RBAC role includes the `google_drive` / `google_sheets` / `google_docs` toolsets
can read anything anyone has ever shared with the SA.

Concretely: user A shares "Customer ARR 2026" with the SA to build a report. User B —
who cannot open that sheet in Drive — asks the bot "what is company Y's ARR?" and the
bot answers, because it never asks who is asking. The same hole exists for listings
(file names, owners, modified times of every shared file) and for writes (B can edit a
sheet B cannot see).

Asking the *model* to check first is not a boundary: it can be skipped, and delegated
sub-agents and cron runs would not inherit the instruction. Enforcement has to live in
the tool handlers and resolve the requesting user from session contextvars, exactly
like the RBAC execution backstop (`model_tools.pre_tool_call`).

## Decision summary

Domain-wide delegation (impersonating the requester so Google enforces its own ACLs) was
ruled out — it is not something EverAfter can grant. Per-user OAuth was ruled out as too
heavy. The chosen approach: **keep acting as the SA, but before every tool call read the
target file's ACL and check that the requesting user is on it.** Google Groups cannot be
expanded without DWD, so group grants fail closed unless the operator maps the group in
config.

## Non-goals

* Group-membership resolution via the Directory API (needs DWD).
* Per-user OAuth / per-user Drive tokens.
* Attributing writes to the requesting user (writes still come from the SA).
* A fully general "identity → email" service for all platforms. v1 resolves Slack
  automatically; other platforms are manual-map only.
* Making the listing pretend nothing was hidden by over-fetching to fill the page.
  Fewer results is acceptable; a "N hidden" count is not (it leaks existence).

## Design

### 1. Enforcement point — `plugins/google_drive_sa/access.py`

Two public pieces, both called from the existing handlers in `tools.py`,
`sheets_tools.py`, `docs_tools.py`.

**`resolve_requester() -> Requester | None`**, `Requester(platform, user_id, email)`.

Sources, in order:

1. Session contextvars — `HERMES_SESSION_PLATFORM` + `HERMES_SESSION_USER_ID`
   (`gateway/session_context.get_session_env`). Covers Slack turns and sub-agents
   delegated from them (contextvars inherit into child tasks).
2. Cron — the scheduler sets a new contextvar `HERMES_CRON_JOB_ID` around each job run
   (it deliberately clears the platform identity; this var is additive). The resolver
   maps it to the job's owner through the automation-ownership registry
   (`cron:<job_id>` → `owner.{platform,user_id}`, the same lookup
   `cron/rbac_ceiling.cron_owner_grant` performs). Ownerless job → `None`.
3. Email for `(platform, user_id)` per §3.

Outcome when there is no requester:

| situation | behaviour |
|---|---|
| gateway session, identity missing or email unresolvable | **deny** |
| cron job with no owner record | **deny** |
| local CLI — `session_context_engaged()` is false | **check skipped** (the fork's "a shell caller is an admin" convention; local dev keeps working) |

**`require_access(file_id, level) -> AccessGrant`**, `level ∈ {"reader", "writer"}`.
Fetches the ACL (§2, cached), evaluates it for the requester, raises `DriveAccessDenied`
otherwise. Every handler calls it **before** its Google API call:

| tool | check |
|---|---|
| `drive_read_file`, `sheets_get_values`, `docs_get` | `reader` on the file |
| `sheets_update_values`, `sheets_append_values`, `sheets_clear`, `docs_insert_text`, `docs_replace_text` | `writer` on the file |
| `drive_upload`, `drive_create_folder`, `sheets_create`, `docs_create` with a folder | `writer` on the parent folder |
| same four, no folder | no pre-check (lands in the SA root); post-create share, see §2 |
| `drive_list_files` | per-result filter, see §4 |

`DriveAccessDenied` is caught at the handler boundary and rendered as the tool's error
string.

**Two denial surfaces.** The tool result the model sees is generic and does not name
groups: *"alice@everafter.ai does not have access to this file. Ask the file's owner to
share it with you."* The audit trail (`agent/data_access_audit.record_access`, event
`drive_access_denied`) records requester, file id/name, required level, the role that
was found (if any), and the **unmapped groups** that would have granted access — the
signal an operator uses to decide what to add to `group_members`.

**Kill switch.** `google_drive.access_check: false` bypasses all of it. Default `true`.

### 2. The ACL decision

**Fetch.** `files.get(fileId, fields="id,name,driveId,permissions(type,emailAddress,domain,role)", supportsAllDrives=True)`.
For My Drive files that is the full ACL in one call (the Drive API populates
`permissions` when the requesting principal can share the file; the SA is on the ACL,
so it usually can). Whenever `permissions` is **absent** from the response — shared-drive
items (`driveId` set) always, and any file the SA holds only `reader` on without
share rights — a second call
`permissions.list(fileId, supportsAllDrives=True, fields="permissions(type,emailAddress,domain,role)")`
follows; for shared-drive items that response includes drive-level (inherited)
memberships. The decision keys on "did we get an ACL", never on `driveId` alone.

**Cache.** In-process `file_id → (acl, fetched_at)`, TTL
`google_drive.acl_cache_ttl_seconds` (default 300). Populated by both the per-file
fetch and by listings (§4). Any API error during fetch → **deny** + log (fail closed).

**Evaluate.** A pure function `evaluate(acl, requester_email, cfg) -> Decision(granted_role, unmapped_groups)`.
For requester `E` with domain `D = E.rsplit("@", 1)[1]`, a permission entry grants its
`role` when:

| entry `type` | grants when |
|---|---|
| `user` | `emailAddress` equals `E` (case-insensitive) |
| `domain` | `domain` equals `D` (case-insensitive) |
| `anyone` | always |
| `group` | `emailAddress ∈ everyone_groups`, **or** `E ∈ group_members[emailAddress]` (case-insensitive) |
| `group`, otherwise | ignored; the address is appended to `unmapped_groups` |

Role ladder: `owner`, `organizer`, `fileOrganizer`, `writer` satisfy `writer`; those
plus `commenter`, `reader` satisfy `reader`. The highest matching role is the
`granted_role`; `None` if nothing matched.

**Files the agent creates.** Without a folder, `sheets_create` / `docs_create` /
`drive_upload` / `drive_create_folder` land in the SA's own root with only the SA on
the ACL — under the new rule not even the requester could read them back. So on
**every** create without a folder the handler adds the requester as `writer` via
`permissions.create(fileId, body={type: user, role: writer, emailAddress: E}, sendNotificationEmail=False, supportsAllDrives=True)`.
With a folder, `writer` on the folder is required first and inheritance does the rest.
A share failure after a successful create is reported in the tool result (the file
exists; the requester may not be able to open it) but is not retried.

### 3. Identity → email, and the write-back

**Config map**, per platform, beside the RBAC keys:

```yaml
slack:
  user_roles:  {U0123: operator}
  user_names:  {U0123: Alice}
  user_emails: {U0123: alice@everafter.ai}   # new
```

* `gateway/config.py` bridges `user_emails` into the platform `extra` exactly as it does
  `user_names`.
* `hermes users add/update` gain `--email`; `hermes users list` shows it.

**Lookup order** inside `resolve_requester()`:

1. `<platform>.user_emails[user_id]` → done.
2. Slack only: `users.info(user=<id>)` → `profile.email`. Bot token via the existing
   `_resolve_slack_token()` (shared with `slack_react`). Requires the **`users:read.email`**
   bot scope on the Slack app (one-time reinstall). The result is cached in memory
   regardless of step 3's outcome, so a persistent write failure never causes repeated
   API hits.
3. Write-back: persist to `slack.user_emails` with the comment-preserving YAML writer
   `hermes_cli/users.py` already uses, and update the loaded in-memory gateway config so
   the running process sees it without a restart. Write failure → warning log, keep the
   in-memory value, continue.
4. Nothing resolvable (missing scope, deactivated/external user, non-Slack platform with
   no map entry) → `None` → deny.

Non-Slack platforms get the same `<platform>.user_emails` map with no API fallback in v1.

### 4. Listing

`drive_list_files` adds `driveId` and `permissions(type,emailAddress,domain,role)` to the
`fields` of its `files.list` call. Each result is evaluated inline when `permissions`
came back; items without one (shared-drive items, and any the SA cannot share) are fetched via `permissions.list` in a
bounded `ThreadPoolExecutor` (8 workers), and every ACL seen populates the cache. Items
the requester cannot `reader` are **silently dropped** — no count, no placeholder. The
tool description changes from "files the service account can see" to "files you have
access to".

Cost: My Drive listings add zero round trips. A cold listing of shared-drive items adds
one parallel batch of `permissions.list` calls (a few hundred ms), after which the cache
covers a following `read` of any listed file.

### 5. Config

New top-level block, read via the gateway config loader:

```yaml
google_drive:
  access_check: true            # kill switch
  acl_cache_ttl_seconds: 300
  everyone_groups:              # groups treated as "anyone in the company"
    - all@everafter.ai
  group_members:                # manually mapped groups (operator-maintained)
    sales@everafter.ai:
      - alice@everafter.ai
      - bob@everafter.ai
```

Values are normalised to lowercase on load. An empty/missing block means: check on,
300 s TTL, no groups mapped (every group grant ignored).

### 6. Error handling summary

| failure | result |
|---|---|
| ACL fetch API error | deny, audit log |
| requester email unresolvable in a gateway session | deny, audit log |
| Slack `users.info` error / missing scope | treated as unresolvable → deny; warning log names the scope |
| config write-back fails | warning; in-memory value used |
| post-create share fails | tool result reports the created file id and that sharing failed |
| `access_check: false` | all checks bypassed, no audit lines |

### 7. Testing

All under `tests/plugins/google_drive_sa/`, against a fake Drive service (no network),
with `HERMES_HOME` redirected by the autouse fixture.

* **`evaluate()`** — table-driven: user / domain / anyone / everyone-group /
  mapped-group / unmapped-group / mixed ACLs; role ladder (`commenter` satisfies
  `reader` not `writer`; `fileOrganizer` satisfies `writer`); case-insensitive emails and
  domains; `unmapped_groups` reported and deduplicated.
* **`resolve_requester()`** — config hit; Slack fallback + write-back (YAML comments
  preserved, in-memory config updated, second call hits neither Slack nor disk); write
  failure keeps the in-memory value; missing scope → `None`; no identity in an engaged
  gateway session → `None`; context not engaged (CLI) → check skipped; cron job id →
  owner → email; ownerless cron → `None`.
* **Handlers** — each read tool denies without `reader`; each write tool denies without
  `writer`; upload/create check the parent folder; create-without-folder calls
  `permissions.create` for the requester; listing drops denied items, uses inline
  permissions when present, fetches shared-drive ACLs, never emits a hidden count;
  ACL fetch error → deny; cache TTL respected (a second read inside the TTL makes no
  ACL call, one after it does).
* **`access_check: false`** → every handler bypasses the check.
* **Cron** — the scheduler sets `HERMES_CRON_JOB_ID` for the duration of the run and
  clears it after.
* Guarded in `tests/test_fork_feature_inventory.py`.

### 8. Deployment notes

* Add the `users:read.email` scope to the Slack app and reinstall it.
* Set `google_drive.everyone_groups` before enabling on the VM; otherwise every
  "shared with all@" file becomes invisible.
* Existing cron jobs that read Drive need an owner (`hermes own claim cron:<id>` or the
  `ownership` tool) or they start denying.
* Watch `audit/data-access.log` for `drive_access_denied` lines with `unmapped_groups`
  to decide which groups to add to `group_members`.
* CLAUDE.md fork section gets a "Drive per-user access check" entry pointing here.

## Not a security boundary against…

An admin with `terminal` can still read the SA key file and call the API directly —
the same residual the cross-user data-access design accepts. This closes the
**tool-mediated** path, which is the one every non-admin Slack user has.
