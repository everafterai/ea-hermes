---
name: automation-ownership
description: Use when building, editing, claiming, or transferring ownership of skills, cron jobs, scripts, or automation bundles in a shared multi-user Hermes — explains the ownership registry, the cross-user edit gate, and the `ownership` tool.
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

## The `ownership` tool

Use this for everything below. It works for any user; the `hermes own` CLI needs
a terminal, which only admins have. Keys are `<kind>:<id>` — `cron:<job_id>`,
`skill:<name>`, `script:<relpath>`, `automation:<bundle>`. The gate and claim
messages print the exact key, so pass it straight through.

| goal | call |
|---|---|
| what do I own? | `ownership(action="list")` |
| who owns this? | `ownership(action="show", key="cron:9f3a1c2b")` |
| claim an unowned one | `ownership(action="claim", key="skill:weekly-report")` |
| hand mine to a teammate | `ownership(action="transfer", key="…", to_user="Bob")` |
| let a teammate edit mine | `ownership(action="collab_add", key="…", user="Bob")` |
| revoke that | `ownership(action="collab_remove", key="…", user="Bob")` |

`to_user` / `user` take a platform user id or the name the person is registered
under. The tool always acts as **the user you are talking to** — there is no way
to act on someone else's behalf, so never claim or transfer without asking first.

Only the owner (or an admin) may transfer or change collaborators. `claim` works
only on an automation nobody owns yet; when someone already owns it the tool names
them — ask them to transfer it rather than retrying.

## `hermes own` CLI (admins, and bundle scaffolding)
- `hermes own init <name>` — scaffold an automation bundle (**only** available here,
  not in the tool)
- `hermes own list|claim|transfer|collab` — the same operations from a shell

## Building a multi-part automation
Use `hermes own init <name>` to create `automations/<name>/` with `automation.yaml`
(owner/description/links), `workflow.md` (the runbook), `scripts/`, and `assets/`.
