# VM deploy runbook — upstream sync 0.16.0 → 0.20.6

Companion to [the preflight](2026-09-01-vm-deployment-preflight.md) (what was and
wasn't verified). This is the procedure.

Placeholders: `<REPO>` = the checkout path on the VM, `<SHA>` = the pre-deploy
commit recorded in step 1.3. Host is assumed to be the `ea-hermes` ssh alias.

## Read first — three things that will bite

1. **Deploy `main`, not the branch.** `updates.parked_branch_strategy` defaults
   to `"switch"` with `auto_switch_parked_branch: true`, so the next
   `hermes update` yanks a deployed branch back to `main` and silently reverts
   you. (That is what moved the dev checkout off the branch on 2026-09-01.)
2. **Do not run `hermes update` at any point in this procedure.** It autostashes
   local changes, may switch branches, and pulls from origin mid-deploy.
3. **`state.db` migration is one-way** (35 → 56 columns). v0.16.0 cannot read the
   migrated DB. It migrates on **first open** — the first `hermes` command that
   touches it, not just gateway start. So the backup in step 1 must happen
   before anything else. `updates.pre_update_backup` is `false` on the dev box;
   if the VM matches, **nothing backs you up automatically.**

---

## 1. Prepare and back up

```bash
# 1.1 — locally: merge the validated branch into main and push.
git checkout main
git merge --no-ff chore/sync-upstream-2026-08-24
git push origin main

# 1.2 — VM: stop the gateway BEFORE anything touches the DB or the code.
ssh ea-hermes 'hermes gateway stop && hermes gateway status'

# 1.3 — VM: record the exact commit you are rolling back TO. Save this output.
ssh ea-hermes 'cd <REPO> && git rev-parse HEAD && git status --short'

# 1.4 — VM: back up. This is what makes rollback possible.
ssh ea-hermes 'cd ~/.hermes && tar czf ~/hermes-backup-$(date +%F-%H%M).tgz \
    config.yaml .env state.db kanban.db cron/jobs.json \
    ownership/ memories/ sessions/ audit/ 2>/dev/null; \
    ls -lh ~/hermes-backup-*.tgz | tail -3'
```

Do not continue until you have the tarball and the SHA.

## 2. Dry-run the config migration on a copy

The dev box has no RBAC keys, so migration was validated against a synthetic
config. This step validates it against the **real** one. Nothing here touches
the live config.

```bash
ssh ea-hermes 'rm -rf /tmp/premig && mkdir -p /tmp/premig && \
    cp ~/.hermes/config.yaml /tmp/premig/ && \
    cd <REPO> && HERMES_HOME=/tmp/premig .venv/bin/python -c "
from hermes_cli.config import check_config_version, migrate_config
print(\"version:\", check_config_version())
print(migrate_config())
"'

# Diff what the migration would change, and confirm the RBAC keys survive.
ssh ea-hermes 'diff <(python3 -c "import yaml,sys;print(yaml.safe_dump(yaml.safe_load(open(\"/root/.hermes/config.yaml\")),sort_keys=True))") \
                    <(python3 -c "import yaml,sys;print(yaml.safe_dump(yaml.safe_load(open(\"/tmp/premig/config.yaml\")),sort_keys=True))") | head -60'
```

**Expected:** version `24 → 39` (or similar), `0 warnings`, and the diff shows
only the five behaviour changes in step 6 — with `slack.user_roles`,
`slack.channel_roles`, `slack.roles`, `quiet_channels`, `channel_models`,
`approvals.require_for_tools`, and `platform_toolsets.slack` all still present.

**If any RBAC key is missing from the migrated copy: STOP.** Do not deploy.

## 3. Deploy code and dependencies

```bash
ssh ea-hermes 'cd <REPO> && git fetch origin && git checkout main && git pull --ff-only origin main'

# A git pull ALONE leaves it broken: upstream added snowballstemmer and bumped
# mcp 1.26.0 -> 2.0.0 (new HTTP stack via httpx2).
ssh ea-hermes 'cd <REPO> && uv pip install -e ".[all]"'
```

## 4. Verify before starting the gateway

```bash
# 4.1 — Linux-only paths that never ran on macOS (28 files, incl. the systemd
#       service manager). Package by package — NEVER the bare wrapper.
ssh ea-hermes 'cd <REPO> && scripts/run_tests.sh tests/hermes_cli/'
ssh ea-hermes 'cd <REPO> && scripts/run_tests.sh tests/gateway/'

# 4.2 — Fork regression gate (fast, 57 files).
ssh ea-hermes 'cd <REPO> && scripts/run_tests.sh $(cat docs/superpowers/plans/2026-08-24-upstream-sync-tests.txt | tr "\n" " ")'

# 4.3 — Diagnostics. This also performs the one-way state.db migration.
ssh ea-hermes 'cd <REPO> && hermes doctor'
```

Expected: the fork gate is **700 passed, 0 failed**. `hermes_cli`/`gateway` will
show the known upstream/host failures documented in the preflight — the
`hermes update` family and macOS/Linux-specific ones. **A failure in the fork
gate is a stop.**

## 5. Start and smoke, in this order

```bash
ssh ea-hermes 'hermes gateway start && sleep 5 && hermes gateway status'
ssh ea-hermes 'hermes tools rbac | head -20'     # fork toolsets present
ssh ea-hermes 'hermes users list'                # roles intact, RBAC ACTIVE
```

Then, in Slack:

| # | Test | Expected |
|---|---|---|
| 1 | DM from an RBAC-roled user | normal reply |
| 2 | DM from a **roleless** user | **refused** — "ask an admin to assign you a role" |
| 3 | Message in a quiet channel | emoji reaction, no text |
| 4 | `@mention` in a normal channel | normal reply |
| 5 | Ask an `operator` to run a shell command | **refused** (terminal not granted) |
| 6 | One tool per MCP server: webflow, stripe, github | each returns real data |
| 7 | Wait for one cron tick | job fires; check `~/.hermes/cron/output/` |

Test 2 and test 5 are the RBAC boundary — if either passes when it should
refuse, roll back. Test 6 is the highest-risk unknown (MCP SDK 1.26 → 2.0).

## 6. Decide on the migration's behaviour changes

The migration silently changes five settings. Two matter:

```yaml
# ~/.hermes/config.yaml — pin these back if you don't want the wider envelope.
delegation:
  max_concurrent_children: 3    # migration raises to 10
  max_iterations: 50            # migration raises to 250
```

10 concurrent subagents × 250 tool calls each, each consuming tokens
independently, is a materially larger unattended spend envelope on a shared
gateway. The other three (`agent.verify_on_stop: false`,
`display.personality: none`, `model_catalog.ttl_hours: 1`) are benign.

Also consider moving `slack.user_roles` into a managed scope
(`/etc/hermes/config.yaml`, root-owned) so RBAC is uniform and non-root users
cannot edit it — see the profile-routing discussion in the sync plan.

---

# Rollback

Pick the level by **how far you got**.

## Level 1 — code only (you have NOT run step 4.3 or started the gateway)

`state.db` is untouched, so this is a clean revert.

```bash
ssh ea-hermes 'hermes gateway stop'
ssh ea-hermes 'cd <REPO> && git checkout <SHA>'
ssh ea-hermes 'cd <REPO> && uv pip install -e ".[all]"'   # restore old deps
ssh ea-hermes 'hermes gateway start && hermes gateway status'
```

## Level 2 — full (the DB migrated, i.e. you ran `hermes doctor` or started the gateway)

**A git revert alone will NOT work** — old code cannot read a 56-column
`state.db`. You must restore the data too.

```bash
ssh ea-hermes 'hermes gateway stop'

# Restore data from the step-1.4 tarball.
ssh ea-hermes 'cd ~/.hermes && tar xzf ~/hermes-backup-<TIMESTAMP>.tgz'

# Restore code + dependencies.
ssh ea-hermes 'cd <REPO> && git checkout <SHA> && uv pip install -e ".[all]"'

ssh ea-hermes 'hermes gateway start && hermes gateway status'
ssh ea-hermes 'hermes users list'    # confirm RBAC is back
```

Anything written between the backup and the rollback — new sessions, memories,
cron run history, ownership claims — is lost. That window is why step 1.4 comes
before everything else.

## If you only need to stop the bleeding

```bash
ssh ea-hermes 'hermes gateway stop'
```

The gateway stops serving Slack; nothing else is affected. Diagnose, then choose
a level above.
