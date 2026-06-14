---
name: issue-track
description: Use when a message in the EverAfter issue-tracking Slack channels (C03B4BC9D2P, C014RF3CQ9J everafter-first-tier-support, C01AYNDAX42 everafter-production_issues) reports, updates, escalates, or resolves a bug or issue, or when a user says "track this" or asks to record an issue in Notion.
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [notion, slack, issue-tracking, ntn, escalation, severity]
    related_skills: [productivity/notion, productivity/notion-issue-tracking]
---

# Issue Tracking in Notion (EverAfter)

## Overview

Track Slack-reported issues in the EverAfter Notion issue tracker using the
`ntn` CLI. Core invariants: **one Notion item per Slack root thread**,
**find before create**, **explore the schema before writing**,
**verify every write by reading it back**.

The issue tracker IDs are fixed — never search for them:

```
DB_ID=373e89d275ae8071a24ad776f46410af            # for creating pages (parent)
DS_ID=373e89d2-75ae-80da-8eb3-000b5371195c        # for querying + schema
```

(If `DS_ID` ever 404s, re-resolve it: `ntn api v1/databases/$DB_ID | jq '.data_sources'`.)

If `productivity/notion-issue-tracking` is available, load it for extra
context. The recipes below are self-sufficient when it isn't.

## Channels

| Channel | Meaning | On create |
|---|---|---|
| C03B4BC9D2P | Original tracking channel (POC) | Standard tracking |
| C014RF3CQ9J `#everafter-first-tier-support` | Lower-priority bugs, tracked and fixed over time — NOT production incidents | Record as first-tier / low priority |
| C01AYNDAX42 `#everafter-production_issues` | Live customer issues | Severity tier is REQUIRED: Moderate, Major, or Critical |

**Escalation:** a message in the production channel that forwards or links a
first-tier thread IS an escalation, even with no accompanying text. Find the
existing Notion item (search by the first-tier thread permalink) and UPDATE
it — raise priority, set the severity tier, add the production thread link.
Never create a duplicate. Only if no item exists, create one and note it was
escalated from first-tier support.

**Severity unstated** in the production channel → infer it from wording and
impact and note that it was inferred; ask in the thread only if genuinely
ambiguous.

## Setup (per shell)

`ntn` reads `NOTION_API_TOKEN` — fresh shells on the Hermes VM usually have
it. If not:

```bash
export NOTION_API_TOKEN=$NOTION_API_KEY
export NOTION_KEYRING=0
```

Never use the browser. If `ntn` is missing, use `curl` against the same
endpoints with `-H "Authorization: Bearer $NOTION_API_KEY"
-H "Notion-Version: 2025-09-03"`.

**JSON parsing: `jq` ONLY — never python/node/perl.** Interpreter one-liners
(`python3 -c`), heredocs (`python3 <<`), and piping API output into an
interpreter all trip the security scanner and force a human approval,
stalling the turn. `jq` triggers none of that. If `jq` is missing
(`command -v jq`), install it once (`sudo apt-get install -y jq`) rather than
falling back to python. There is no JSON-reading task in this workflow that
`jq` cannot do:

```bash
ntn api v1/data_sources/$DS_ID | jq -r '.properties | to_entries[] | "\(.key)\t\(.value.type)"'
ntn api v1/data_sources/$DS_ID | jq '.properties[] | select(.type=="select") | .select.options[].name'
```

## Workflow

### 1. Explore the schema

`database_id` and `data_source_id` are DIFFERENT IDs: `database_id` is for
creating pages, `data_source_id` is for querying and schema. Both are pinned
above.

```bash
ntn api v1/data_sources/$DS_ID | jq '.properties'        # property names + exact select options
```

Use the EXACT property names and select-option strings the schema returns —
never invent or approximate an option (Notion silently creates new options,
corrupting the database).

### 2. Find the existing item — always, before creating

Key on the **root thread permalink** (replies share the root's `p<ts>`; one
item per root thread):

```bash
echo '{"filter": {"property": "<slack-link-property>", "url": {"contains": "p1780475773736919"}}}' \
  | ntn api v1/data_sources/$DS_ID/query -X POST --json -
```

(If the schema shows the link property as `rich_text` rather than `url`, use
`"rich_text"` as the filter condition key.)

### 3. Create (only when nothing was found)

For multiline or quote-heavy issue text, ALWAYS pipe a JSON payload — never
inline it in `ntn` args:

```bash
echo '{
  "parent": {"database_id": "'$DB_ID'"},
  "properties": {
    "<title-property>": {"title": [{"text": {"content": "Short issue summary"}}]},
    "<status-property>": {"select": {"name": "<exact option from schema>"}},
    "<slack-link-property>": {"url": "https://base-ai.slack.com/archives/C.../p..."}
  },
  "markdown": "## Details\n\nWhat was reported, by whom, impact, thread context."
}' | ntn api v1/pages --json -
```

Syntax notes for inline args: `key=value` strings, `key[nested]=value`
objects, `key:=value` typed (bool/number/null).

### 4. Update

```bash
ntn api v1/pages/{page_id} -X PATCH properties[<status-property>][select][name]="<exact option>"
ntn api v1/pages/{page_id}/markdown -X PATCH markdown="## Update

New findings or mitigation steps."
```

Status changes, severity raises/lowers, new findings → property patch and/or
markdown append on the SAME item.

**Marking done:** someone says it's done or reacts :white_check_mark: / :vi:
→ set the schema's completed/done status option.

### 5. Verify every write

Read the page back and confirm the SPECIFIC fields you wrote (status,
severity, thread link) — not just that the page exists:

```bash
ntn api v1/pages/{page_id} | jq '.properties'
```

Mismatch → fix and re-verify. Still failing → react :eyes: and ask in the
thread.

## How to act (output contract)

**SILENT BY DEFAULT — this overrides your instinct to be helpful and to
acknowledge people.** In this channel you are a background tracker, NOT a chat
participant. The only thing you may emit is an emoji reaction. Being @mentioned
and asked directly does NOT earn a text reply.

**Every turn ends with exactly this, in order:**

1. `slack_react` — `:acknowledged:` when done, `:eyes:` when blocked
2. `turn_end`

A turn that ends without `slack_react` is a FAILURE. A turn that posts ANY text
(outside the one narrow exception below) is a FAILURE.

**NEVER post text to report, confirm, summarize, narrate, or explain work you
did.** Every one of these is a FAILURE — do not send anything like them:

- "Tracked." / "Done." / "Updated." / "Marked resolved."
- "Updated the Notion item with X" / "...with the screenshot details."
- "I inferred Critical severity from the wording."
- the Notion link, API output, JSON, or page dumps.

If the work succeeded, you have **nothing to say** — react and stop.

**The only time text is allowed** is when you are genuinely BLOCKED and cannot
finish tracking without a human's answer — e.g. it is truly ambiguous which
existing issue this refers to, or required info is missing and cannot be
inferred. Then ask ONE short question, react `:eyes:`, and `turn_end`. Wanting
to confirm, report, or "let you know" is NOT being blocked — that is narration;
stay silent. Before sending any text, ask: "Am I actually stuck, or just
talking?" If not stuck → send nothing.

(Only other case: a human explicitly asks you to hand over the tracked item →
reply with the Notion page URL only, then react and `turn_end`.)

## Slack links — which format where

- Notion `url` property: the bare permalink.
- Notion page body (markdown): a named link — `[Slack thread](https://...)`.
- Slack text replies (rare): mrkdwn — `<https://...|thread>`.

## Common Pitfalls

1. **Searching for the database.** Use `$DB_ID` directly — search finds
   lookalike databases.
2. **Mixing up the two IDs.** Query/schema → `data_source_id`; page create
   parent → `database_id`.
3. **Inventing select options.** Read the schema first; exact strings only.
4. **Duplicating an escalated issue.** A forwarded first-tier thread means
   UPDATE the existing item, not create.
5. **Inlining multiline text in `ntn` args.** Quotes and newlines break —
   pipe JSON with `--json -`.
6. **Parsing JSON with python/node/perl.** `python3 -c`, heredocs, and
   `ntn | python3` all trigger security-scan approval prompts and stall the
   turn. Use `jq` — it is never flagged.
7. **Using the browser.** The flow is CLI-native, always.

## Verification Checklist

- [ ] `$DB_ID` used directly (no database search)
- [ ] All JSON parsed with `jq` (no python/node one-liners or heredocs)
- [ ] Schema explored this session; exact property/option names used
- [ ] Queried for an existing item by root-thread permalink before creating
- [ ] Escalations updated the existing item (no duplicate)
- [ ] Severity tier set (production channel)
- [ ] Write read back; written fields confirmed
- [ ] **Called `slack_react` (then `turn_end`) — MANDATORY every turn**; no
      routine text confirmation posted
