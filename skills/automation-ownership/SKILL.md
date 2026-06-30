---
name: automation-ownership
description: Use when building, editing, claiming, or transferring ownership of skills, cron jobs, scripts, or automation bundles in a shared multi-user Hermes — explains the ownership registry, the cross-user edit gate, and the `hermes own` CLI.
metadata:
  hermes:
    tags: [ownership, automations, governance, multi-user]
---

# Automation Ownership

In this shared Hermes, every user-built automation (skill, cron job, script,
automation bundle) can have an **owner** plus **collaborators**, recorded in
`~/.hermes/ownership/registry.json`.

## Rules the tools enforce
- Creating a skill/cron/script/bundle records you as its owner.
- Owners and collaborators edit freely.
- Editing someone else's automation is **gated**: the tool refuses once with a
  warning. Confirm with the user, then re-invoke the same call with
  `confirm_cross_user_owner="<owner name>"`. The owner is DM'd on the confirmed edit.
- Editing an **unowned** legacy automation proceeds, but offer to claim it.
- An autonomous run (no human) may not edit an owned automation.

## `hermes own` CLI
- `hermes own list --user <id>` — what a user owns / collaborates on
- `hermes own claim <key>` — claim an unowned automation (`script:foo.sh`, `cron:<id>`, `skill:<name>`)
- `hermes own transfer <key> --to <id>` — owner or admin reassigns ownership
- `hermes own collab add|remove <key> --user <id>` — manage collaborators
- `hermes own init <name>` — scaffold an automation bundle under `automations/<name>/`

## Building a multi-part automation
Use `hermes own init <name>` to create `automations/<name>/` with `automation.yaml`
(owner/description/links), `workflow.md` (the runbook), `scripts/`, and `assets/`.
