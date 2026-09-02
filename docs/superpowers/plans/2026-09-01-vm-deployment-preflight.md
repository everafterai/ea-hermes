# VM deployment preflight — upstream sync 0.16.0 → 0.20.6

What was verified before deploying the sync branch to the GCP VM, what was
**not**, and the procedure that makes the deploy reversible.

The test suite proves the code is internally consistent. It does **not** prove
the gateway boots on the VM, that the VM's config still loads, or that live data
migrates. This document covers that second question.

## Verified against real data (on a copy, never the original)

| Check | Result |
|---|---|
| Config migration ladder v24 → v39 (15 steps) | ✅ 5 changes, 0 warnings |
| Full fork config surface survives migration | ✅ 14/14 keys preserved |
| RBAC activates end-to-end from the migrated config | ✅ enabled, deny-until-assigned, operator≠terminal, `extends` composes |
| `slack.*` bridged into `PlatformConfig.extra` | ✅ user_roles, user_names, channel_roles, quiet_channels, channel_models |
| Slack registers as a bundled **plugin** platform | ✅ 22 platforms, adapter factory present |
| `platform_toolsets.slack` gating | ✅ ownership / slack / slack_post / webflow_assets all survive |
| `state.db` migration (417 sessions, 52 messages) | ✅ 35 → 56 columns, integrity ok, zero rows lost |
| Cron drift guard vs 66 real jobs | ✅ 0 at risk — guard is inert without a snapshot (verified empirically, not from the comment) |
| `hermes doctor` | ✅ runs; only pre-existing Bedrock IAM issue |
| Gateway construction (no network) | ✅ GatewayRunner builds, authz mixin + toolset resolver present |
| Fork feature inventory (71 call sites) | ✅ all wired |
| Fork regression gate | ✅ 57 files / 700 tests |

## NOT verified — ranked by risk

1. **MCP servers under SDK 2.0.0.** `mcp` went `1.26.0 → 2.0.0` (MCP revision
   2026-07-28, new HTTP stack via `httpx2`). The VM runs `webflow`,
   `webflow_everafter`, `stripe`, and `github` MCP servers; this box has none,
   so none of them were exercised. A major-version SDK bump is the most likely
   thing to break real functionality (Webflow publishing, Stripe).
   **Mitigation:** after deploy, run `hermes mcp` / exercise one tool per server
   before declaring success.

2. **Linux-only paths — 28 test files never ran here**, including
   `test_gateway_service.py`, `test_service_manager.py`,
   `test_gateway_platform_gating.py`, `test_relaunch.py`. These are precisely
   the systemd/service-management paths the VM uses.
   **Mitigation:** run the suite ON the VM before restarting the gateway.

3. **Nothing has actually served a message.** No Slack event has been processed
   end-to-end. Adapter behaviour is proven only against mocks.
   **Mitigation:** smoke a real DM + a quiet-channel message post-deploy.

4. **The VM's config is not this box's config.** This machine has **no** RBAC
   keys. The migration was validated against a synthetic v24 config carrying the
   full fork surface, which is representative but not the real file.
   **Mitigation:** copy the VM's `config.yaml` to a scratch `HERMES_HOME` and
   dry-run the migration there first (procedure below).

5. **`state.db` migration is one-way.** 35 → 56 columns; v0.16.0 code cannot
   read the migrated DB. **Rollback requires a restored backup, not a git
   revert.**

## Behaviour changes the migration makes (accept deliberately)

- `delegation.max_concurrent_children` **3 → 10**
- `delegation.max_iterations` **50 → 250**

Together that is a much larger unattended spend envelope on a shared gateway:
10 concurrent children × 250 tool calls each. Pin both back in `config.yaml`
before deploy if that is not wanted.

- `agent.verify_on_stop` → `false`
- `display.personality` → `none` (one-time reset)
- `model_catalog.ttl_hours` → `1`

## Deploy procedure

```bash
# 1. BACK UP FIRST — the DB migration is one-way.
ssh ea-hermes 'cd ~/.hermes && tar czf ~/hermes-backup-$(date +%F-%H%M).tgz \
    config.yaml .env state.db kanban.db cron/jobs.json ownership/ memories/ sessions/'

# 2. Dry-run the config migration on a COPY, before touching the live one.
ssh ea-hermes 'mkdir -p /tmp/pre && cp ~/.hermes/config.yaml /tmp/pre/ && \
    HERMES_HOME=/tmp/pre hermes config migrate'   # inspect the diff

# 3. Deploy code + dependencies. A git pull ALONE is not enough:
#    upstream added snowballstemmer and bumped mcp to 2.0.0.
ssh ea-hermes 'cd <repo> && git fetch && git checkout chore/sync-upstream-2026-08-24 \
    && uv pip install -e ".[all]"'

# 4. Run the suite ON the VM (Linux paths that never ran on macOS).
#    Package by package — never the bare wrapper. See CLAUDE.md.
ssh ea-hermes 'cd <repo> && scripts/run_tests.sh tests/hermes_cli/'

# 5. Diagnostics before restart.
ssh ea-hermes 'hermes doctor'

# 6. Restart, then smoke in this order:
#    a) hermes tools rbac        — fork toolsets present
#    b) DM from an RBAC-roled user
#    c) DM from a roleless user  — must be REFUSED
#    d) quiet-channel message    — emoji-only, no text
#    e) one tool per MCP server  — webflow, stripe, github
#    f) wait for one cron tick   — jobs still firing
```

**Rollback:** restore the tarball from step 1 *and* check out the previous
commit. Restoring code alone leaves a 56-column `state.db` the old code cannot
read.
