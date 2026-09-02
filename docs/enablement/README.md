# Off Your Plate — Agent Building Adventure

Materials for the internal session that introduces the team (~20 people) to what
our self-hosted Hermes agent can do, and has them design, build and demo their
first automation in squads — all inside a single three-hour session.

**Program:** *Off Your Plate*. **Session:** *Agent Building Adventure*.

Two deliverables, built from the same design system:

| File | What it is |
| --- | --- |
| `dist/deck.html` | 31-slide presentation. The version shown in the room. |
| `dist/playbook.html` | Long-form facilitator playbook. The version used to prepare. |

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
| `S` | Speaker notes panel |
| `O` | Jump menu (all slide titles) |
| `Home` / `End` | First / last slide |
| `Esc` | Close jump menu |

Touch swipe works. The URL carries `#12`, so reloading mid-session keeps your
place. Every slide has speaker notes in `data-notes` — that's where the
facilitation lives, not on the slides.

The last two slides are marked ⚑ **facilitator only** (prep checklist and
failure-mode table). Don't project them.

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

Every item in the seeded idea menus is tagged `script` / `agent` / `both`, so
the distinction gets reinforced twenty-four more times during the design phase.

## Squads

Cohesive around a workflow, not a department — which is why QA sits with the R&D
lead rather than with the developers, and why the CEO is inside a squad instead
of observing.

| Squad | Members | Shared cause | First port of call |
| --- | --- | --- | --- |
| Go-To-Market | Adi · Ohad · Gal Biran | Demand → pipeline → close | Shachar |
| Customer | Yonat · Rona · Uri | Onboarding → retention → expansion | Tal |
| Product | Itamar · Roni · Vivian | Discovery → spec → feedback | Shachar |
| Release Pipeline | Elad · Ben · Ori | Ticket → PR → QA → deploy | Gil |
| Dev Experience | Leetal · Nir · Yahav · Yiftach | The daily developer loop | Gal Briner |
| Internal Ops | Pazit · Ayelet · Ariel | Running the company itself | Tal |

Release Pipeline is deliberately the pipeline BA-282 came out of, so the opening
story doubles as that squad's brief. Internal Ops is furthest from an obvious
idea and most likely to stall — check on them first.

**Show the trust segment to Ariel (CISO) before the day.** He is a participant,
which makes the 0:32 block a security briefing delivered with the security
officer in the room. Best case he co-signs it; worst case he corrects something
you would otherwise have said wrong in front of everyone.

## Session shape

**One session, three hours.** Six squads of 3–4, each built around one shared
workflow rather than one department: Go-To-Market, Customer, Product, Release
Pipeline, Dev Experience, Internal Ops. Each squad names its own **driver** (the
person at the keyboard, who owns the build and needs the `config.yaml` role).
The four champions sit in **no** squad — they float, advise and unblock, and are
briefed never to take the keyboard. There is no follow-up
Demo Day — squads demo inside the session, and design and build are one
continuous 90-minute block that squads manage themselves, with no checkpoint
between.

| Time | Block |
| --- | --- |
| 0:00 | Open on the numbers: yesterday's runs, the board of live automations, BA-282 |
| 0:10 | What this is: the four blocks, scripts vs agents, the filter, good-at / not-good-at |
| 0:32 | What it can and cannot see — deliberately **before** the brainstorm |
| 0:42 | Demo: build The Radar live |
| 0:54 | Break |
| 1:04 | Squad brief |
| 1:08 | **Design it, then build it — one 90-minute block** |
| 2:38 | Show us what you made — 3 min per squad, live |
| 2:55 | What happens tomorrow, champion commitments |

Two load-bearing design decisions worth preserving if you rework this:

**The trust segment runs before the brainstorm.** Unresolved anxiety about what
the agent can see suppresses idea generation in both directions — some people
won't propose anything touching data they assume is off-limits, others get
quietly uneasy about being watched.

**The worksheet has a required "access needed" field.** It turns scope into
something squads design within and own, rather than something vetoed later. A
squad that has to write down the access its idea needs self-selects toward
narrow ideas. With no checkpoint between design and build, the facilitator *is*
the checkpoint — circulate in the first ten minutes of the build block and
shrink anything with a long access list before a squad invests in it.

**The demos are the accountability loop, and something has to carry it
afterwards.** A single session with no follow-up meeting decays inside a week.
The replacement already exists and is already running: the automation-creation
monitor announces every new automation the moment it appears, and the weekly
activity report posts what everything did. Point both at a channel the whole
company can see and the scoreboard becomes ambient rather than a meeting.

## The opening

Do **not** open with a capability tour or a live "ask it anything" demo. The
strongest asset available is that this is already working at volume, built by
three people around their day jobs. Lead with evidence:

1. **The figures from yesterday** — 8 automations, 1,274 runs, 0 errors,
   74 messages. The last two numbers are the point: the silence is deliberate,
   because an automation that speaks every time it runs gets muted inside a
   week. Reuse this when the Radar posts nothing on a quiet day.
2. **The board** — all eight, with owners and engines. Point at two columns:
   *owners* (three people, not a department) and *engine* (six of eight are
   script + agent together, so the scripts-vs-agents teaching lands later as an
   observation about our own system rather than as theory).
3. **BA-282** — marked Done while a pull request was still open. The reconciler
   caught it fifteen minutes later, posted a warning, added a Notion
   deployment-check note and updated the tracking state. Nobody asked it to
   look. Don't over-explain; the mechanism is unpacked at the worked-example
   slide as a deliberate callback.

**Keep the honesty beat.** Several live automations run flawlessly without
substantiating that they changed anything. Saying *"it ran is not the same as
it helped"* costs nothing and buys real credibility — but the bar for the
in-session demos is deliberately lower (*it runs*); "it helped" is what the
Sunday report tests.

These numbers are a snapshot — refresh them from the activity report before
presenting, and keep the ratio framing even if the figures move.

## The demo

**The Radar** — every weekday at 8am, check the public web for news about our
customers and prospects (funding, exec changes, layoffs, acquisitions, launches,
hiring pushes), post anything meaningful to `#radar` with a link and one line on
why it matters.

Chosen because it needs almost no access: the public web, a list of company
names, one channel. The list is the customer logos already published on
base.ai, so it discloses nothing that isn't already public, and it can live in
the instruction itself rather than in a connected system. Given that our
constraint on access is trust rather than technical granularity, a demo whose
whole grant is three permissions makes the point better than one whose appeal is
reach.

Rehearse the identical build the day before and note which item to point at —
ideally about an account someone in the room owns. The payoff beat is turning to
that person and asking "did you know that?"

## Before presenting

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
- [ ] Brief the four champions: circulate, advise, unblock, never take the
      keyboard
- [ ] Write six scope cards (one channel / database / folder each)
- [ ] Point the creation monitor and weekly activity report at a public channel
- [ ] Create `#radar`, assemble the watch list, rehearse the demo, keep a screen
      recording as fallback
