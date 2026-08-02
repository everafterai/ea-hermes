# Webflow MCP for a Single Marketing User — Design

**Date:** 2026-08-02
**Status:** Approved design, pre-implementation
**Topic:** Wire Webflow's MCP server into the VM gateway, reachable by exactly
one user via a dedicated RBAC role, draft-first with an approval gate on publish
**Related:** [Slack Tool RBAC design](2026-05-31-slack-tool-rbac-design.md),
[Per-tool approval gate design](2026-07-01-per-tool-approval-gate-design.md)

## Problem

The Base AI content channel (`C0BCX83K82V`) runs a marketing agent whose channel
prompt already promises *"Webflow is draft first"* — but the agent has no way to
reach Webflow at all. Blog posts drafted in Slack must be hand-carried into the
Webflow CMS.

The agent needs read access to sites and CMS collections, the ability to create
and update CMS items as drafts, and an explicit, human-approved publish step.

**Access must be confined to one named person.** The obvious move — granting
Webflow to the existing `marketing` role — is wrong: `marketing` is also
attached to channel `C0BCX83K82V` through `channel_roles`, which equips *every
poster in that channel*, so the grant would silently extend to any teammate who
drops a message there. `operator` (likewise reachable by any poster in the two
issue-tracking channels) and `readonly` must not get it either.

## Scope

**No code changes.** Every edit lands in the VM's `~/.hermes/config.yaml`. The
`marketing` role already exists as a custom role under `slack.roles`, granted to
user `U02S08M50S3` and to channel `C0BCX83K82V` via `channel_roles`; it stays
unchanged, and a new single-member role carries the Webflow grant (§4).

Out of scope for v1: Webflow Designer manipulation (elements, styles, variables,
components), CMS schema changes, static-page editing, and site custom code.

## Key constraint: the hosted MCP is OAuth-only

Webflow ships two MCP servers. The hosted remote one
(`https://mcp.webflow.com/mcp` and `/sse`) requires an OAuth handshake. Probed
directly:

```
POST https://mcp.webflow.com/mcp  → HTTP 401
www-authenticate: Bearer realm="OAuth",
  resource_metadata="https://mcp.webflow.com/.well-known/oauth-protected-resource/mcp",
  error="invalid_token"
```

This fork deliberately omits the `auth` field on MCP servers (GitHub, Stripe) so
no OAuth flow can block a headless gateway or cron run. The remote server is
therefore unusable here.

The **local stdio server** (`npx -y webflow-mcp-server`, npm `1.0.0`) reads a
static API token from the `WEBFLOW_TOKEN` environment variable. That matches the
`WEBFLOW_API_TOKEN` already present in the VM's `~/.hermes/.env`, so this is the
transport we use.

Cost of the choice: a Node ≥ 22.3 + `npx` runtime dependency on the VM, and one
subprocess per gateway start. `_resolve_stdio_command`
([tools/mcp_tool.py](../../../tools/mcp_tool.py) ~l.403) resolves bare `npx`
against `~/.hermes/node/bin`, `~/.local/bin`, and `/usr/local/bin`, so a
Hermes-managed Node install is found without an absolute path.

**Verified on the VM (2026-08-02):** `node v22.22.3`, `npx` at
`/home/shaidiamant/.local/bin/npx`. That path is one of the explicit fallbacks
above, so a bare `command: npx` resolves even under a minimal systemd `PATH` —
*provided the gateway service runs as the `shaidiamant` user*, since the
fallback is built from `os.path.expanduser("~")` in the gateway process. If the
service ever runs as a different user, switch to the absolute path.

## Design

### 1. MCP server block

```yaml
mcp_servers:
  webflow:
    command: npx
    args: ["-y", "webflow-mcp-server@1.0.0"]
    env:
      WEBFLOW_TOKEN: "${WEBFLOW_API_TOKEN}"
    timeout: 120
    connect_timeout: 60
    enabled: true
    tools:
      include:
        - sites_list
        - sites_get
        - collections_list
        - collections_get
        - collections_items_list_items
        - pages_list
        - pages_get_content
        - pages_get_metadata
        - components_list
        - asset_tool
        - collections_items_create_item
        - collections_items_update_items
        - collections_items_publish_items
        - sites_publish
        - collections_items_delete_item
```

Two load-bearing details:

**The `env:` remap is required, not cosmetic.** The package reads
`WEBFLOW_TOKEN`; the `.env` file holds `WEBFLOW_API_TOKEN`. `_build_safe_env`
([tools/mcp_tool.py](../../../tools/mcp_tool.py) l.296) passes only a safe
baseline (`PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `TERM`, `SHELL`, `TMPDIR`,
`XDG_*`) plus variables explicitly listed in the server's `env:` block. A bare
`WEBFLOW_API_TOKEN` in `.env` would never reach the subprocess. The `${VAR}`
interpolation resolves at config load, the same way it does inside `headers:`
for the GitHub server.

**Pin the version.** With `@latest`, any npm publish silently changes the tool
surface — which would silently invalidate the approval-gate globs in §3. A
publish tool renamed upstream would sail through an unmatched glob. Pinning
makes that a connection-time failure instead of a silent authorization hole.

### 2. Tool surface: 42 → 15

The package registers 42 tools. Trimming is both a safety and a prompt-cache
decision — every included schema rides in the cached system prompt of every
`marketing_publisher` session.

**Excluded — Designer-session tools** (`de_component_tool`, `de_page_tool`,
`de_learn_more_about_styles`, `element_tool`, `element_builder`, `style_tool`,
`variable_tool`, `get_designer_app_connection_info`, `get_image_preview`,
`components_get_content`, `components_get_properties`,
`components_update_content`, `components_update_properties`): these require a
live Designer session through the Webflow Bridge App. Non-functional from a
headless gateway; pure token weight.

**Excluded — site custom code** (`add_inline_site_script`,
`delete_all_site_scripts`): arbitrary JavaScript on the live marketing site.
Excluded outright rather than gated — there is no content workflow that needs
them, and an approval prompt is a weaker control than absence.

**Excluded — the write-to-live variants** (`collections_items_create_item_live`,
`collections_items_update_items_live`): removing these at the schema level is
stronger than gating them. It means no accidental publish path exists at all;
publishing becomes exactly one explicit, gated step
(`collections_items_publish_items`). Gating them instead would leave two
distinct routes to live content, doubling the surface the gate must cover
correctly.

**Excluded — CMS schema changes** (`collections_create`,
`collection_fields_create_static`, `collection_fields_create_option`,
`collection_fields_create_reference`, `collection_fields_update`): a content
agent authors items, it does not reshape collections.

**Excluded — static page editing** (`pages_update_static_content`,
`pages_update_page_settings`): these edit live pages with no draft concept, and
the upstream README notes `pages_update_static_content` only supports secondary
locales. Blog posts are CMS items; static pages stay human-edited.

**Excluded — Webflow's own helpers** (`ask_webflow_ai`, `webflow_guide_tool`):
documentation assistants, not needed for the workflow, and they cost context.

**Included:** nine read tools, `asset_tool` for blog imagery, two draft-write
tools, and three gated tools (§3).

### 3. Approval gate on publish and delete

```yaml
approvals:
  require_for_tools:
    - "mcp_webflow_sites_publish"
    - "mcp_webflow_collections_items_publish_items"
    - "mcp_webflow_collections_items_delete_item"
```

**MCP tool names are prefixed.** Tools register and dispatch as
`mcp_{server}_{tool}` ([tools/mcp_tool.py](../../../tools/mcp_tool.py) l.3133,
`prefixed_name`), so the dispatcher — and therefore `tool_requires_approval`
([tools/approval.py](../../../tools/approval.py) l.988) — sees
`mcp_webflow_sites_publish`, never a bare `sites_publish`. A bare name matches
nothing and the gate becomes **silently inert**: configured, visible in config,
and not gating. This is the single most likely way to get this wrong, and it
fails open. The exact dispatched names must be confirmed with
`hermes mcp test webflow` before the config is trusted.

The gate runs at the single dispatch chokepoint in
`model_tools.handle_function_call`, immediately after the RBAC backstop: RBAC
answers *may you*, the gate answers *confirm you*. On Slack it blocks in-thread
offering **Allow once / Allow session** — never "always", so a gated tool is
never written to the permanent allowlist.

`collections_items_delete_item` is gated rather than excluded because a content
agent legitimately needs to retract a mistaken draft; destroying a published
item should still take a human beat.

### 4. RBAC grant — a dedicated single-user role

Webflow must reach **one specific user**, not everyone who holds `marketing`.
This matters because `marketing` is granted two ways: to user `U02S08M50S3` via
`user_roles`, *and* to every poster in `C0BCX83K82V` via `channel_roles`.
Adding Webflow to the `marketing` role would hand it to any teammate who posts
in that channel.

The policy has no per-user toolset list — `user_roles` maps each user to exactly
one role name — so per-user scoping is expressed as a role with one member:

```yaml
slack:
  roles:
    marketing:            # unchanged — no Webflow
      toolsets: [ web, vision, session_search, memory, image_gen, file,
                  skills, browser, google_docs, google_sheets, google_drive,
                  notion, marketing, slack_post, slack_react ]
    marketing_publisher:  # marketing + Webflow; exactly one member
      toolsets: [ web, vision, session_search, memory, image_gen, file,
                  skills, browser, google_docs, google_sheets, google_drive,
                  notion, marketing, slack_post, slack_react,
                  webflow, mcp-webflow ]
  user_roles:
    U02S08M50S3: marketing_publisher   # was: marketing
  channel_roles:
    C0BCX83K82V: marketing             # unchanged — channel posters get NO Webflow
```

Resolution, via `_effective_grant`
([gateway/tool_access.py](../../../gateway/tool_access.py) l.208-234):

| Identity | Grant | Webflow? |
|---|---|---|
| `U02S08M50S3` in `C0BCX83K82V` | `marketing_publisher` ∪ `marketing` | yes |
| `U02S08M50S3` elsewhere | `marketing_publisher` | yes |
| Any other poster in `C0BCX83K82V` | `marketing` | no |
| `operator` / `readonly` | own role | no |
| `admin` | `*` | yes (wildcard, by design) |

**The grant follows the user, not the channel.** `U02S08M50S3` carries Webflow
into every channel and DM, because a user's own role applies everywhere and
`channel_roles` is union-only. "This user, but only while in `C0BCX83K82V`" is
not expressible in the current policy and would require a code change to
`_effective_grant`. Accepted for v1: the constraint that was asked for is
*which person*, and that is met exactly.

**Known maintenance cost:** config roles have no inheritance —
`_coerce_roles` reads only a flat `toolsets` list — so `marketing_publisher`
duplicates all 15 of `marketing`'s entries. Any future edit to `marketing` must
be mirrored, or the two silently drift. The alternative (grant on the shared
role) was rejected because it fails the requirement outright. A YAML anchor
(`&marketing_ts` / `*marketing_ts` plus the two extras) is not used here because
`hermes users` rewrites `config.yaml` comment-preservingly and anchor round-trip
behavior through that path is unverified.

Assign with `hermes users update U02S08M50S3 --role marketing_publisher`, which
keeps `allow_admin_from` consistent, rather than hand-editing `user_roles`.

**Both names are required**, for the reason already documented for Stripe at
[gateway/tool_access.py](../../../gateway/tool_access.py) l.48-61: the two RBAC
enforcement points resolve the toolset differently. The `enabled_toolsets`
filter (`filter_enabled_toolsets` → `allowed_toolsets`) sees the bare alias
`webflow`, while the execution backstop (`denial_for_current_tool` →
`_toolset_for_tool` → `registry.get_toolset_for_tool`) reads `ToolEntry.toolset`
directly, which is set to the canonical `mcp-webflow` at registration time and
does **not** resolve through the alias table. Granting one name only means
passing one enforcement point and failing the other.

**No `platform_toolsets` edit is needed.** `_get_platform_tools`
([hermes_cli/tools_config.py](../../../hermes_cli/tools_config.py) l.1508-1526)
treats MCP server names inside a platform's toolset list as an allowlist *only
if at least one is present*. `platform_toolsets.slack` currently lists no MCP
server names, so `explicit_mcp_servers` is empty and the `else` branch adds
every globally enabled MCP server to the base set under its bare name. Adding
`webflow` to `mcp_servers` is therefore sufficient to put it in the base set;
RBAC's intersection with the role grant is what confines it to a single user.

Roleless users remain denied entirely (deny-until-assigned), and `chat_only`
resolves to a defined role granting nothing — both distinct from the
`marketing_publisher` path above.

### 5. CLI exposure (accepted, with an optional mitigation)

The same auto-add mechanism means **local `hermes` CLI sessions on the VM also
receive the Webflow tools**, since RBAC is a messaging-platform control and does
not apply to the CLI. This is already true of the GitHub MCP server and is
accepted: a CLI session on the VM is an operator with shell access, for whom the
Webflow token in `.env` is readable anyway.

Optional mitigation if tighter separation is wanted later: add `github` to
`platform_toolsets.cli`, which converts that list into an MCP allowlist and
excludes `webflow` from CLI sessions. Not part of v1.

## Security posture

The **Webflow API token is the real blast-radius boundary**, exactly as the
Stripe restricted key is for the Stripe MCP. RBAC controls *who* can reach the
tools, `tools.include` controls *which* operations exist, and the approval gate
adds a human confirmation on publish — but none of those substitute for a token
scoped to the single marketing site rather than the whole workspace.

Confining the grant to `marketing_publisher` rather than `marketing` is what
keeps the `channel_roles` attachment on `C0BCX83K82V` from becoming a Webflow
grant to every poster in that channel. That channel should still stay internal —
its posters do get the `marketing` toolset — but a message there no longer
reaches the live website.

Neither the approval gate nor RBAC is a boundary against an admin with a shell:
`terminal` grants direct access to `.env`. This design closes the
toolset-reachable path and makes publishing an explicit, logged, human-confirmed
act.

## Failure modes

| Failure | Behavior | Mitigation |
|---|---|---|
| Node missing / < 22.3 on VM | Server fails to connect; MCP discovery failure is non-fatal, server is skipped | Verify before rollout; blocking prerequisite |
| npm registry unreachable at gateway start | `npx` cannot fetch the package; server skipped, `marketing_publisher` silently loses Webflow tools | Pinned version + npm cache; consider a global pre-install for determinism |
| `marketing` edited later without mirroring to `marketing_publisher` | The two roles drift; the publisher silently lacks a capability their teammates have | Adjacent placement in config + the comment noted in §4; no code enforces this |
| Upstream renames a publish tool | With a pinned version, cannot happen silently; on a deliberate version bump the tool disappears from `tools.include` and connection logs a warning | Re-run `hermes mcp test webflow` on every version bump |
| Approval glob typo | Gate silently inert — the worst case | Confirm exact dispatched names via `hermes mcp test webflow` before trusting |
| Token scoped too broadly | Agent can touch non-marketing sites | Scope the token at issue time |

## Verification

1. ~~**Prerequisite (blocking):** `node --version` ≥ 22.3 on the VM and `npx`
   resolvable from the gateway's `PATH`.~~ **Cleared 2026-08-02** — v22.22.3,
   `npx` at `/home/shaidiamant/.local/bin/npx`.
2. `hermes mcp list` shows `webflow` enabled; `hermes mcp test webflow` connects
   without an OAuth prompt and lists exactly the 15 included tools under their
   dispatched `mcp_webflow_*` names.
3. Cross-check every name in `approvals.require_for_tools` against that output.
4. `hermes users list` shows `U02S08M50S3` as `marketing_publisher`.
5. Slack, as `U02S08M50S3` in `C0BCX83K82V`: listing sites and creating a draft
   CMS item succeed; `sites_publish` prompts in-thread for approval.
6. **The single-user check — the point of this revision.** Slack, as a *different*
   teammate posting in `C0BCX83K82V` (who receives `marketing` via
   `channel_roles`): Webflow tools are absent from the model's tool list, and a
   forced invocation is blocked by the execution backstop. If this passes for
   `U02S08M50S3` but also passes for anyone else in that channel, the grant
   landed on the wrong role.
7. Slack, as the `readonly` user (`U01SN6Y7V8A`): denied on both paths.
8. `scripts/run_tests.sh tests/gateway/` — no code changed, so this is a
   regression check only.

## Open question (non-blocking)

Is `WEBFLOW_API_TOKEN` scoped to the single Base AI marketing site, or to the
whole Webflow workspace? Per the security posture above, single-site is
strongly preferred; a workspace-wide token widens the blast radius beyond what
RBAC and the approval gate can constrain.
