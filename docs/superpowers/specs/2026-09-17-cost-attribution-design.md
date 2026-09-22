# Cost attribution per channel and automation — `hermes costs`

**Date:** 2026-09-17
**Status:** approved, not yet implemented
**Related:** [2026-06-30-cron-rbac-toolset-ceiling-design.md](2026-06-30-cron-rbac-toolset-ceiling-design.md)
(cron session identity), [2026-06-30-automation-ownership-design.md](2026-06-30-automation-ownership-design.md)
(who owns a job)

## Problem

We run one gateway for a whole company: many Slack channels, many cron jobs,
delegated sub-agents. We want to know what Hermes costs over time and which
channel or automation is spending it. Upstream already records the raw
material — every session row in `state.db` carries token counts,
`estimated_cost_usd`, `actual_cost_usd`, `cost_status` and `cost_source`, and
`session_model_usage` holds a per-(session, model, provider, task) breakdown
written per API call (`SessionDB._record_model_usage`). `hermes insights` and
the dashboard analytics endpoint aggregate it by model, by platform (`source`)
and by day.

Two gaps stop that from answering the question:

1. **Nothing groups by channel or by cron job.** Insights stops at platform
   level. "Slack cost $X" is answerable; "#issues cost $X" and "the daily
   digest job cost $Y" are not. The keys exist on each session — `chat_id`,
   `chat_type`, `origin_json` (with `parent_chat_id`), and for cron the job id
   embedded in the session id `cron_<job_id>_<YYYYmmdd_HHMMSS>`
   (`cron/scheduler.py`) — but no query uses them.
2. **The VM's main model is unpriced.** The catalog in
   `agent/usage_pricing.py` knows `gpt-5.6-*`, `gpt-4o`, `gpt-4.1` and `o3`,
   not `gpt-5.4-mini` (the VM default). OpenAI's `/models` endpoint carries no
   prices, so `get_pricing_entry` returns `None` and every main-loop session
   is stored with `cost_status="unknown"` and no cost. Upstream reserved a
   `user_override` value in `CostSource` but never wired a config that
   produces it. A report over the VM's data today would show tokens and $0.

Sub-agent sessions (`platform="subagent"`, `parent_session_id` set in
`tools/delegate_tool.py`), compression children and branches also hold cost
of their own; unless they roll up to their root, a channel's cost is
undercounted by everything it delegated.

## Non-goals

- **Per-turn accuracy.** Cost is attributed to the day a session *started*
  (`started_at`): each contributing session is windowed and bucketed on its
  **own** `started_at`, and the root supplies only the keys. Cron runs are one
  session each so they bucket cleanly; a single long-lived Slack channel
  session still smears its own turns across its lifetime, though its
  sub-agent, compression and branch children each land on their own day.
  Accepted for v1 (decision 2026-09-17). A per-API-call ledger is the
  follow-up if trend curves for long-lived channels turn out to matter.
- **A dashboard page, a scheduled Slack post, a sheet export.** The CLI
  (with `--json`/`--csv`) is the query layer those would build on; the user
  will design the presentation surface separately with Hermes itself.
- **Schema changes.** Attribution is derived at query time from columns that
  already exist. No new columns, no backfill, no upstream write-path edits.
- **Live pricing fetches.** The override block is static rates the operator
  maintains.
- **Budgets / alerts / enforcement.** Reporting only.

## Approach

Derive attribution at query time from the session store, in a new fork-only
module, and expose it as a new `hermes costs` subcommand. Alternatives
considered: stamping `cron_job_id` / `channel_id` columns at write time
(cleaner SQL, but three upstream write paths to patch and carry through every
sync, and history stays unattributed until backfilled); a cached attribution
table (only worth it if the recursive walk is slow — the store is small).
Query-time derivation ships on all existing history the day it lands.

## Attribution model

Every session resolves to a **root** by walking `parent_session_id` to the top
(recursive CTE, capped at `_MAX_LINEAGE_DEPTH` = 1000 with a logged warning if
a lineage ever reaches it). Sub-agents, compression children and branch
sessions all attribute to their root. The query returns **one row per
contributing session**, windowed and bucketed on that session's own
`started_at`; the root supplies the keys. This matters because
`session_reset.mode` defaults to `none` and `publish_compression_child` keeps
`parent_session_id`, so a Slack channel's root is permanent and quickly months
old — windowing on the root would make this month's spend vanish from a
30-day report. The root supplies both keys:

- **Job** — the root's `id` matches `^cron_(?P<job>.+)_\d{8}_\d{6}$`.
  Job ids are `uuid4().hex[:12]` (`cron/jobs.py`), but the pattern anchors
  on the trailing timestamp and is otherwise permissive so a hand-written id
  containing underscores still parses. The job **name** is resolved from
  `cron.jobs.load_jobs()`; a deleted job shows as its id with
  `(deleted)`.
- **Channel** — from the root's `origin_json`: `parent_chat_id` when present
  (a thread), else `chat_id`; platform from `source`. Thread sessions
  therefore collapse into their channel. The display name is the session's
  `display_name` (the adapter's `chat_name`), falling back to the raw id.
  DMs attribute to the DM (`chat_type="dm"`), shown as `dm:<user_id>`.
- **Both** — a cron session whose job delivers into a Slack channel counts
  under the job *and* under that channel. The channel comes from the job's
  resolved delivery targets (`cron.scheduler._resolve_delivery_targets`,
  `[{platform, chat_id, thread_id}]`); `deliver=origin` resolves via the
  job's stored origin. In the `channel` view a job with several targets is
  attributed to each, **duplicating** its cost per channel — that view is
  "what does this channel consume", not a partition. The `both` view keys
  each root once, on `(job, channel)`, where a multi-target job's channel
  column lists all its targets joined by `+`, so `both` *is* a partition.
  Totals in the `job` and `both` views equal the sum of
  `COALESCE(actual_cost_usd, estimated_cost_usd)` over every session row
  (roots and descendants) that started in the window, plus the cost of
  their auxiliary usage rows — a superset of `SessionDB.usage_totals`, which
  ignores both child sessions and auxiliary usage rows. The `channel` view
  carries a footer noting how much was counted more than once, or nothing
  when it is zero.
- **Unattributed** — a root with neither key (CLI sessions, sub-agents whose
  root was deleted) appears as one `(unattributed)` row so totals reconcile.

Roots key, children don't: a child session is never keyed on its own, so a
sub-agent spawned from a channel session is that channel's cost — but it is
its own row, counted on the day it ran.

## Cost and status

Per contributing session: `cost = COALESCE(actual_cost_usd,
estimated_cost_usd)` plus that session's own auxiliary usage rows (a session's
usage rows attach to that session only, never to its root), the same
precedence `usage_totals` uses. `aggregate` then sums those rows per key and
period, so a root's lineage adds up exactly once.
Every report row also carries:

- `status` — `actual` when every contributing session has `cost_status =
  actual`; `estimated` when all are priced (actual or estimated); `partial`
  when some contributing session has `cost_status = unknown` (or a null
  cost); `unpriced` when none is priced. A single session's row is whole, so
  `partial` first appears at the aggregate.
- `unpriced_tokens` — input+output tokens of the unknown-status sessions, so
  "$0.00" is never silently a missing price.
- tokens (input, output, cache read, cache write), sessions, API calls.

Buckets: `--bucket day|week|month` on `started_at` in UTC. Week starts
Monday.

`--by model` and `--by user` are included because they are free once the
query layer exists: `model` reads `session_model_usage` grouped by
`(model, billing_provider)`, `user` groups roots by `user_id`. A session spans
several models, so the model view has no session count to give: it renders
`-` in the table, `null` in JSON and an empty CSV cell rather than a
misleading `0`. A legacy session with no usage rows therefore counts in the
session views but not in the model view.

## Pricing override

New top-level `pricing:` block in `config.yaml`:

```yaml
pricing:
  overrides:
    gpt-5.4-mini:
      input: 0.40          # USD per million tokens
      output: 1.60
      cache_read: 0.10     # optional; default 0
      cache_write: 0.0     # optional; default 0
    "openai/gpt-5.4":
      input: 2.50
      output: 10.00
```

`get_pricing_entry` (`agent/usage_pricing.py`) consults the overrides
**first**, before the subscription/OpenRouter/endpoint/catalog lookups, and
returns a `PricingEntry(source="user_override",
pricing_version="user-override")`. Match is on the model name after
`resolve_billing_route` normalisation, exact first, then the bare name with
any `vendor/` prefix stripped. Rates are parsed with `Decimal` from the YAML
value; a malformed entry logs once and is ignored (fail-open to the catalog,
never a crash in the request path). The block is read through the read-only
fast path `read_raw_config_readonly` (falling back to `read_raw_config` when
unavailable), and the parsed entries are memoized on the raw `overrides`
mapping, so an edited config.yaml is picked up automatically. Non-finite
rates (NaN/Infinity) are rejected like any other malformed entry.

New sessions are priced at write time as today. History is fixed by:

`hermes costs --reprice [--days N] [--dry-run]` — for every
`session_model_usage` row whose `cost_status` is `unknown` (or cost is null)
and whose model now resolves to a pricing entry (override or catalog),
recompute `estimated_cost_usd` via `estimate_usage_cost` from the stored
tokens, set `cost_status="estimated"` and `cost_source` to the entry's
source, then re-sum the parent `sessions` row from its usage rows. With no
`--days/--since/--until` it covers the **whole store** (a repair pass should
not silently stop at 30 days).

Rows already priced **from provider data or the catalog** are never touched —
a later rate change does not rewrite history; `--reprice --force` is
deliberately not offered in v1. Rows whose `cost_source` is `user_override`
**are** recomputed, at any status other than `actual`: their price is fully
determined by the override table, so recomputing from the stored tokens is
exact and idempotent. That is what repairs a *straddled* session — one alive
when `pricing.overrides` landed. `session_model_usage` rows are
UPSERT-accumulated per route with
`cost_status = COALESCE(excluded.cost_status, cost_status)`, so the first
priced call after the override flips the whole row to `estimated` while
carrying only that one call's cost, and the unpriced-row query would skip it
forever. `added_usd` counts only the delta (new − old), and a session whose
summary is already `cost_status = 'actual'` keeps that summary (its usage
rows are still priced). Runs in one transaction per session; prints how many
rows it priced, how many it recomputed, and the total it added. `--dry-run`
prints the same without writing.

## CLI

```
hermes costs [--days 30 | --since YYYY-MM-DD [--until YYYY-MM-DD]]
             [--by channel|job|both|model|user]   (default: both)
             [--bucket day|week|month|none]        (default: none)
             [--platform slack]                     (filter on source)
             [--top N]                              (default 50; 0 = all)
             [--json | --csv]
             [--profile NAME]
hermes costs --reprice [--days N] [--dry-run] [--profile NAME]
```

Terminal output is a right-aligned table: key columns, cost (via
`format_cost_label` so sub-cent rows show 4dp), status, sessions, tokens,
`unpriced_tokens` only when non-zero. Sorted by cost descending, with a
`TOTAL` line and the reconciliation footer described above. `--bucket` adds
a leading period column and sorts by period then cost. `--json` emits
`{"window": {...}, "by": ..., "bucket": ..., "rows": [...], "total": {...},
"double_counted_usd": ...}`; `--csv` emits the rows with a header.

Errors (missing DB, bad dates, a locked store) print one line and exit 1: a
missing store names its path and `--profile`, a locked one says the gateway is
writing and to retry. A store with no priced sessions still prints the table
(all `unpriced`) plus a hint to set `pricing.overrides` and run `--reprice`.
When `--top` cuts rows, the table says how many are hidden and JSON carries
`rows_omitted`; TOTAL always covers every row. The report opens the store
`SessionDB(read_only=True)` (avoids writer-lock contention with the live
gateway) and never migrates it; a store not write-opened since a past schema
migration prints a hint naming `hermes costs --reprice --dry-run` (a
write-open that migrates but changes no cost) and exits 1. Read connections
set `PRAGMA busy_timeout=5000` so a non-WAL store waits for the gateway's
writer instead of failing instantly.

## Files

- `agent/cost_attribution.py` — **new.** Pure query layer over a SessionDB
  connection: `attribute_sessions(conn, since, until, platform=None) ->
  list[AttributedSession]` (root id, job id, job name, channel platform/id/
  name, user, started_at, tokens, cost, status), `aggregate(rows, by, bucket)
  -> Report`, `reprice(db, since, dry_run) -> RepriceResult`. No CLI
  imports. Job names and delivery targets come through two small injectable
  resolvers so tests need no jobs file.
- `hermes_cli/subcommands/costs.py` — **new.** `build_costs_parser(subparsers,
  *, cmd_costs)`, mirroring `subcommands/insights.py`; the table/JSON/CSV
  formatters live here.
- `hermes_cli/main.py` — `cmd_costs`, the registration line beside
  `build_insights_parser`, and `"costs"` in `_BUILTIN_SUBCOMMANDS` and the
  ordered command list.
- `agent/usage_pricing.py` — override lookup at the top of
  `get_pricing_entry`, plus `_load_pricing_overrides()`.
- `CLAUDE.md` — a "Cost attribution" entry in the fork-specific section.
- `tests/test_fork_feature_inventory.py` — needles for the module, the
  parser registration and the override hook.

## Testing

Unit tests on a temp `SessionDB` (the autouse fixture already redirects
`HERMES_HOME`), under `tests/agent/test_cost_attribution.py`,
`tests/hermes_cli/test_costs_cli.py` and `tests/agent/test_usage_pricing_overrides.py`:

- cron id parse: 12-hex id, hand-written id with underscores, non-cron id,
  cron id whose job was deleted.
- thread session with `parent_chat_id` rolls up to the channel; DM keys as
  `dm:<user>`; a session with no origin is unattributed.
- sub-agent and compression-child chains (two levels deep) attribute to
  the root and add their cost to it; a child whose root is missing is
  unattributed.
- cron job delivering to a channel appears under `job`, `channel` and as
  one `(job, channel)` row under `both`; a job with two targets is
  double-counted in `channel` and the footer reports the amount; `job` and
  `both` totals equal the direct SUM over all rows in the window.
- status: all-actual, all-estimated, mixed → `partial` with the right
  `unpriced_tokens`, all-unknown → `unpriced`.
- buckets: day/week/month boundaries in UTC; `--since/--until` inclusive.
- override: exact match, vendor-prefixed match, malformed entry ignored,
  override wins over a catalog model.
- reprice: prices only unknown rows, re-sums the session row, leaves
  already-priced rows alone, `--dry-run` writes nothing.
- CLI: `--json` shape, `--csv` header, exit 1 on a bad date.

Run with `scripts/run_tests.sh <file>` per the repo rule; never the whole
suite.

## Deployment

1. Deploy the code (`main` → VM fast-forward → gateway restart, per the
   usual procedure).
2. Check the VM's SQLite is ≥ 3.51.3 / 3.50.7 / 3.44.6 (`hermes doctor`) so
   the store stays in WAL; otherwise reports contend with the gateway's
   writes.
3. **Stop the gateway**, then add `pricing.overrides` for `gpt-5.4-mini` (and
   any other model in use) to the VM's `config.yaml`; rates from OpenAI's
   pricing page at the time.
4. Run `hermes costs --reprice --dry-run` (no window: the whole store), check
   the count, then `hermes costs --reprice`.
5. **Start the gateway** again.

   Steps 3–5 are ordered on purpose. A session that is alive when the
   overrides land straddles the change: its usage row keeps the earlier
   unpriced tokens but flips to `estimated` carrying only the first priced
   call's cost, so it is under-priced until repaired. `--reprice` does repair
   it (it recomputes `user_override` rows), but it writes an absolute cost
   computed from a snapshot of the tokens — a call landing on the same row
   between that read and the write is overwritten. Stopping the gateway
   first removes both the straddle and the race.
6. `hermes costs --days 30 --by both` is the first real report.
