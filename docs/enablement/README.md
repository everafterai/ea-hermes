# Off Your Plate — Agent Building Adventure

Materials for the internal session that introduces the team (~20 people) to what
our self-hosted Hermes agent can do, and has them design, build and demo their
first automation in squads — all inside a single three-hour session.

**Program:** *Off Your Plate*. **Session:** *Agent Building Adventure*.

Two deliverables, built from the same design system:

| File | What it is |
| --- | --- |
| `dist/deck.html` | 33-slide presentation. The version shown in the room. |
| `dist/playbook.html` | Long-form facilitator playbook. The version used to prepare. |
| `dist/notes.html` | Speaker notes, one entry per slide — printable, phone-friendly. Stripped from the deck itself. |

## Editing

**Edit the `*.template.html` files, never `dist/`.** Then rebuild:

```bash
python docs/enablement/build.py
```

The build splices `assets/fonts.css` into the `/*FONTS*/` marker in each
template and writes a self-contained file to `dist/`. It refuses to write if
markup is unbalanced.

`assets/fonts.css` holds base64 `@font-face` rules for Poppins 500/600 and Lato
400/700 — the real base.ai pairing — so the output renders identically offline
and when published as an artifact, where external font CDNs are blocked. It's
kept out of the templates because the payload is ~59 KB and would otherwise
dominate every diff.

To regenerate the fonts (only needed if you change weights or faces), fetch the
Google Fonts `css2` stylesheet for each face, take the `@font-face` block whose
`unicode-range` covers `U+0000-00FF` (the latin subset), download that `.woff2`
and base64 it into a `src:url(data:font/woff2;base64,…)` rule.

## Branding

Pulled from the live base.ai stylesheet rather than guessed:

- **Poppins** 500/600 for headings, labels and UI; **Lato** 400/700 for body
- Purple `#8F4AFB` → pink `#F84E8E`, used as a gradient in exactly two places
  (the progress bar and the quote rules) so it stays a signature rather than a
  texture
- Pastel tints `#F3EEFA` / `#FCE4EC` / `#FFEEDF` on cards
- Neutrals biased slightly purple rather than flat grey
- Light and dark themes both designed; the viewer's toggle stamps
  `data-theme` on `:root` and overrides the `prefers-color-scheme` default

## Deck controls

| Key | Action |
| --- | --- |
| `→` `space` `PgDn` | Next slide |
| `←` `PgUp` | Previous slide |
| `O` | Jump menu (all slide titles) |
| `Home` / `End` | First / last slide |
| `Esc` | Close jump menu |

Touch swipe works. The URL carries `#12`, so reloading mid-session keeps your
place.

**Speaker notes are not in the projected deck.** They live in the template as
`data-notes` on each slide — that's where the facilitation lives — and the
build strips them from `dist/deck.html`, so nothing on the projector can
reveal them. `dist/notes.html` is the same notes as a clean, printable document,
one entry per slide, for paper or a phone.

The prep checklist and the failure-mode table are **not** in the deck — they
live in the playbook only, so nothing facilitator-facing can end up on the
projector.

## The concept model

One mental model, taught once and reused everywhere — in the demo, on the
worksheet, in the share-back:

**Trigger → Context → Decision → Action**

The third block has two settings, and which one you pick is what decides the
*kind* of automation you're building:

| | Script | Agent |
| --- | --- | --- |
| Decides by | Rules you can state | Judgment you can only describe |
| Same input twice | Identical answer, always | Usually the same. Not guaranteed |
| Costs per run | Effectively nothing | Real, if small |
| When it's wrong | It breaks loudly | It can be quietly plausible |

The test squads apply during the brainstorm: **can you write the rule down?**
Then it's a script. Can you only describe what good looks like? Then it needs
judgment.

Most real automations are **both** — a script gathers and filters
deterministically, an agent handles the one messy part, a script delivers. The
skill being taught is carving an idea into the part you can write down and the
small part you can't, then handing over only the second piece. It mirrors the
access rule deliberately: least information, least judgment.

Two consequences worth keeping if you rework the material:

**The script half is a credibility device, not a footnote.** Saying out loud
that much of what the team wants needs no model at all is what makes the
agentic claims believable — particularly to R&D. It also converts the
"not good at" list into a routing rule rather than a limitation: anything that
must be exact becomes a script.

**The pitch for scripts is that the script was never the hard part.** What
stopped anyone writing the Jira nudge was auth, hosting, scheduling, breakage
and Python. All five are gone. That is the empowerment message for the
non-engineers in the room.

**When one job is too big, it's a conveyor belt.** The senior lesson, taught on
Adi's blog: idea → draft → visuals/approval → release gate → published is three
scheduled automations, one on-demand step and two human approvals — and they
never call each other. Each watches one Notion board and touches only rows in
its own stage; humans move rows too. That gives each stage the smallest cage,
lets a person step in between any two, and contains failure. The routing rule
for squads: *if the one-liner won't fit in one line, it's several automations —
build the first stage today.* It's on the worksheet and in the sign-off note.

Every item in the seeded idea menus is tagged `script` / `agent` / `both`, so
the distinction gets reinforced twenty-four more times during the design phase.

## Squads

Cohesive around a workflow, not a department — which is why QA sits with the R&D
lead rather than with the developers, and why the CEO is inside a squad instead
of observing.

| Squad | Members | Shared cause | First port of call |
| --- | --- | --- | --- |
| Revenue | Adi · Ohad · Yonat · Rona · Uri | Demand → close → onboard → retain → expand | Shachar |
| Product | Itamar · Roni · Vivian | Discovery → spec → feedback | Shachar |
| Release Pipeline | Elad · Ben · Ori | Ticket → PR → QA → deploy | Gil |
| Dev Experience | Leetal · Yahav · Yiftach | The daily developer loop | Gal Briner |
| Internal Ops | Pazit · Ayelet · Ariel | Running the company itself | Tal |

Release Pipeline is the pipeline the Jira ↔ GitHub reconciler watches, so the
board doubles as that squad's brief. Internal Ops is furthest from an obvious
idea and most likely to stall — check on them first.

**Revenue is a merge.** Go-To-Market (Adi, Ohad, Gal Biran) and Customer were
folded together once Gal Biran dropped out — a two-person squad is too fragile,
and marketing + sales + CS at one table is the whole customer journey. Five is
the ceiling. Nir is on leave, so Dev Experience is three.

**Attrition rule for the day:** any squad that finds itself at two merges into
its partner — Revenue ↔ Product, Release Pipeline ↔ Dev Experience, Internal
Ops ↔ Revenue. Decide it at the squad brief, out loud.

**Show the trust segment to Ariel (CISO) before the day.** He is a participant,
which makes the 10:02 block a security briefing delivered with the security
officer in the room. Best case he co-signs it; worst case he corrects something
you would otherwise have said wrong in front of everyone.

## Session shape

**One session, three hours.** Five squads of 3–5, each built around one shared
workflow rather than one department: Revenue, Product, Release Pipeline, Dev
Experience, Internal Ops. Each squad names its own **driver** (the
person at the keyboard, who owns the build and needs the `config.yaml` role).
The four champions sit in **no** squad — they float, advise, unblock and **sign
off worksheets**, and are briefed never to take the keyboard. There is no
follow-up Demo Day — squads demo inside the session, and design and build are
one continuous 90-minute block with a single mobile checkpoint in the middle:
**the sign-off**.

| Time | Block |
| --- | --- |
| 09:30 | Open on where it started: the tracker's numbers, three months later, the board |
| 09:40 | What this is: the four blocks, scripts vs agents, the filter, good-at / not-good-at |
| 10:02 | What it can and cannot see — deliberately **before** the brainstorm |
| 10:12 | Demo: build The Radar live |
| 10:24 | Break |
| 10:34 | Squad brief |
| 10:38 | **Design it · get it signed off · build it — one 90-minute block** |
| 12:08 | Show us what you made — 3 min per squad, live (five squads) |
| 12:25 | What happens tomorrow, champion commitments |

Two load-bearing design decisions worth preserving if you rework this:

**The trust segment runs before the brainstorm.** Unresolved anxiety about what
the agent can see suppresses idea generation in both directions — some people
won't propose anything touching data they assume is off-limits, others get
quietly uneasy about being watched.

**The trust segment ends with people rules, not system rules** — "Rules of the
road": never paste a secret; share what the channel could see; don't feed it
what you don't trust (prompt injection, explained as "text written *to* the
agent"); nothing leaves the building unreviewed; everything you build has your
name on it; when unsure, ask. Each is backed by something the platform actually
enforces (session persistence, per-user Drive check, the cage, approval gates,
the ownership registry), but it is deliberately phrased as behaviour.

**The worksheet has a required "access needed" field.** It turns scope into
something squads design within and own, rather than something vetoed later. A
squad that has to write down the access its idea needs self-selects toward
narrow ideas.

**The sign-off gate.** Nobody touches a keyboard until a champion (or the
facilitator) has initialled the squad's worksheet with the *exact* tool list —
not "Notion" but which database, not "Slack" but which channel. It is mobile
(squads come to whoever is nearest), it is where over-scoped ideas get shrunk,
and it turns the tool list into the access request. Champions carry the
toolset-by-role sheet (`hermes tools rbac`) so they can check a list against
the driver's grant on the spot, and are briefed to shrink rather than promise
to fix permissions later.

**The demos are the accountability loop, and something has to carry it
afterwards.** A single session with no follow-up meeting decays inside a week.
The replacement already exists (the creation monitor and the weekly report)
but is presented as platform behaviour, not as anyone's trackers: "every new
automation announces itself in one channel", "once a week, one post on what
everything did." Point both at a channel the whole company can see and the
scoreboard becomes ambient rather than a meeting.

## The claw intro

Two slides after the opening numbers, before the four blocks: the origin story of the
category, told for a laugh, landing on the serious idea underneath. Three
minutes, no more. The arc — one developer's WhatsApp assistant goes viral in
January; renamed twice in a week after Anthropic objects to "Clawd"
(Clawdbot → Moltbot → OpenClaw); Moltbook, the agents-only social network,
where the bots found a religion and someone leaves the database open; then the
serious self-hostable frameworks that came out of that spring, of which Hermes
(Nous Research) is the one we run. **Verify the dates before presenting** —
someone in the room will have followed it closely.

## Images

The lobster from the origin story is the deck's recurring character: after
slide 6 it turns up every few slides doing what that slide describes. Nineteen
slots, all under `assets/img/`, all inlined by the build:

- `claw-1.png` … `claw-4.png` are **structural** — they render a labelled
  placeholder until filled.
- Everything else (`title`, `ba282`, `twokinds`, `scripts`, `strong`, `trust`,
  `leastinfo`, `radar`, `cage`, `squads`, `signoff`, `stop`, `showus`,
  `tomorrow`, `pipeline`, `snowball`, `armor`; `ba282` is retired) is **optional** — an absent file simply doesn't render, so the
  slide reads as designed. Generate the ones you like.

The full prompt table — one style line, one character description (small red
lobster, round glasses, grey hoodie) — is in the playbook under "The images".
Generate with Hermes's own `image_gen` if you can. Keep videos out of the deck
and play them from a second window; prompts for four short clips are in the
playbook too.

## The opening

Do **not** open with a capability tour or a live "ask it anything" demo. Open
on the origin story with its own numbers, then what it rolled into:

1. **Where it started — the tracker, 14 June.** Someone posts a bug in
   `#production_issues` or `#first-tier-support`; the tracker reads the thread,
   files it in Notion with title, summary, priority and customer, acknowledges
   the post, and keeps the ticket in step until it closes. Since 14 June:
   **80 reports, 83 tickets — every one filed by the tracker, none typed by a
   person**; 73 acknowledged in-thread; 78 status notes across 27 threads;
   30 tickets updated as threads moved; **46 loops closed, median 5.2 days**;
   **~10 hours** of secretary work since June (conservative: 5 min/ticket +
   2 min per update or reply). The claim to lean on is *eighty for eighty — not
   one went untracked*; say the ten honestly so the sixty on the next slide is
   trusted.
2. **Three months later** — 8 automations (seven scheduled + the tracker),
   4 builders, **~360 receipt-verified actions since August** (321 from the
   acceptance script, 21 topic radar, 11 reconciler, 5 blog drafting, 4 MRR),
   **~60 hours** of work nobody did since June (49 from six weekly reports +
   10 from the tracker), ~9 hours a week lately. Both slides are totals since
   June so the units match: ten became sixty. "Verified" is the weekly
   report's strict definition — a change another system can confirm — which
   is why the acceptance script dominates and the blog jobs score low despite
   doing plenty; say that if asked. Outside the report: 128 blog rows, all
   created by the pipeline, 3 published. Of the 187 scheduled runs that left a record, 20 said
   anything: the silence is deliberate.
3. **The board** — all eight with owners and engines; point at *owners* (four
   people, one of them the chief of staff) and *engine* (four hybrids, two
   pure scripts, the tracker agent-only because every message is different).

The tracker returns as the four-blocks worked example — trigger (cheap
classifier gate) · context (thread + Notion DB) · decision (judgment) · action
(deterministic tool calls) — so the story stays continuous.

**Where the tracker figures come from.** Notion issues data source
`373e89d2-75ae-80da-8eb3-000b5371195c` (all pages are created by the
integration; `Issue Type`, `Priority`, `Status`, `Time to Close (hours)`),
Slack `conversations.history`/`replies` for `C01AYNDAX42` and `C014RF3CQ9J`
since 14 Jun (bot user `U0B2YAZTE9G` reactions and replies), and
`~/.hermes/state.db` sessions for those two `chat_id`s (173 sessions, ~6,600
messages). Refresh before presenting. Last refreshed 15 Sep (evening):
unchanged except status replies 74 → 78; reconciler clean and silent since the
token fix.

**Keep the honesty beat.** Most live automations run flawlessly without
substantiating that they changed anything. Saying *"it ran is not the same as
it helped"* costs nothing and buys real credibility — but the bar for the
in-session demos is deliberately lower (*it runs*); "it helped" is what the
Sunday report tests.

**Where the figures come from.** The Sunday job (`62aa0df31eac`) writes
`~/.hermes/cron/output/62aa0df31eac/<date>.md` on the VM with a JSON block
under "Script Output" — `totals`, `run_categories`, `actions`, and per-job
rows. Refresh from the report that lands the Sunday before the session; keep
the framing even if the figures move. Note the figures are
persisted-output-based, so every-minute jobs that stay silent (the creation
monitor) don't inflate run counts — which is why "runs" is no longer the
headline.

## The demo

**The Radar** — every weekday at 8am, check the public web for news about our
customers and prospects (funding, exec changes, layoffs, acquisitions, launches,
hiring pushes), post anything meaningful to `#cs-general-everafter` with a link and one line on
why it matters.

Chosen because it needs almost no access: the public web, a list of company
names, one channel. The list is the customer logos already published on
base.ai, so it discloses nothing that isn't already public, and it can live in
the instruction itself rather than in a connected system. Given that our
constraint on access is trust rather than technical granularity, a demo whose
whole grant is three permissions makes the point better than one whose appeal is
reach.

**Build it in the channel, and know why that works.** A cron job delivers to
the conversation it was created in by default (`deliver: origin`). Without
further config, a job created by @mentioning the bot in a channel would deliver
every run *into the thread under that creation message* — the scheduler keeps
channel-born jobs pinned to their thread on purpose. `slack.cron_continuable_surface:
in_channel` (set on the VM 15 Sep) flattens that: runs whose target is their own
origin channel post as fresh top-level messages, with a seeded session behind
them so an @mention under a post gets an answer that knows the brief. The
adapter logs a warning that `reply_in_thread` is still true — that is fine and
deliberate: posts land flat, replies to them stay threaded. Do **not** set
`reply_in_thread: false`; it would flatten the bot's replies in every channel.

Two consequences of the knob: (1) every squad's job posts top-level in whatever
channel it was built in, so "the channel you build it in is the channel it posts
to" is the rule to say out loud; (2) existing jobs whose explicit target equals
their origin chat — Adi's two blog jobs, delivered to her own DM — flip from
threaded to flat in that DM. Tell Adi.

End the instruction with "then run it once now": the `run` action fires through
the normal delivery path, so the first post appears in the channel while the
room watches.

**The first-run line is insurance.** The Radar depends on live news, and the
day's news can be thin — a demo whose payoff is silence is no payoff. The
instruction therefore tells the bot to open its very first run with one visible
line — *"Radar is live. Base is learning how to build agents across the
organization — first pass below."* — so the channel gets a post within a minute
regardless, with any real items underneath. From the second run on, `[SILENT]`
applies as normal.

Rehearse the identical build the day before and note which item to point at —
ideally about an account someone in the room owns. The payoff beat is turning to
that person and asking "did you know that?"

## Before presenting

- [ ] **Replace the expired GitHub token** in `~/.hermes/.env` on the VM. The
      `GITHUB_TOKEN` there is a fine-grained PAT that started returning
      HTTP 401 on 12 Sep at 18:50; the Jira ↔ GitHub reconciler was blocked on
      every run since, and the read-only GitHub MCP uses the same variable.
      Mint a new fine-grained PAT with the same read scopes (pull requests,
      actions, contents/metadata), swap the value, and confirm with
      `GH_TOKEN=<new> gh api user -q .login` before restarting the gateway.
- [ ] **Refresh the opening figures** (runs / errors / messages, and the
      board itself) from the weekly automation activity report
- [ ] **Verify the access claims on the "Access is by role" slide** against the current
      `config.yaml`. It's written from principles plus the roles that exist;
      being loosely accurate about what the agent can reach is worse than
      saying less.
- [ ] Confirm each squad's **driver** — curiosity over seniority
- [ ] Create a Slack channel per squad; give the **drivers** roles in
      `config.yaml` — champions already have access, it is the drivers who get
      blocked
- [ ] Brief the four champions: circulate, advise, unblock, sign off
      worksheets, never take the keyboard; print each a toolset-by-role sheet
      (`hermes tools rbac`)
- [ ] Generate the four claw images into `assets/img/` and rebuild; verify the
      claw-story dates
- [ ] Write six scope cards (one channel / database / folder each)
- [ ] Point the creation monitor and weekly activity report at a public channel
- [ ] Invite the bot to `#cs-general-everafter` (not a member yet), assemble the watch list, rehearse the demo, keep a screen
      recording as fallback
