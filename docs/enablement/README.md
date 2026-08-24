# Agent enablement session

Materials for the internal session that introduces the team (~20 people) to what
our self-hosted Hermes agent can do, and sends them away in squads with one
automation each to own.

Two deliverables, built from the same design system:

| File | What it is |
| --- | --- |
| `dist/deck.html` | 26-slide presentation. The version shown in the room. |
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

## Session shape

Three hours, six squads by function (customer success, sales, marketing,
support, product, R&D), one champion each.

| Time | Block |
| --- | --- |
| 0:00 | Cold open — take live questions about the company, no slides |
| 0:10 | What this is: the four blocks, the filter, good-at / not-good-at |
| 0:40 | What it can and cannot see — deliberately **before** the brainstorm |
| 0:55 | Demo: build The Radar live |
| 1:10 | Break |
| 1:25 | Squad brainstorm (30 min, worksheet) |
| 1:55 | Share-back, 4 min per squad |
| 2:20 | Build block — champions build, squads watch |
| 2:50 | Commitments, Demo Day confirmed |

Two load-bearing design decisions worth preserving if you rework this:

**The trust segment runs before the brainstorm.** Unresolved anxiety about what
the agent can see suppresses idea generation in both directions — some people
won't propose anything touching data they assume is off-limits, others get
quietly uneasy about being watched.

**The worksheet has a required "access needed" field.** It turns scope into
something squads design within and own, rather than something that gets vetoed
at share-back. A squad that has to write down the access its idea needs
self-selects toward narrow ideas.

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

- [ ] **Verify the access claims on slide 10** against the current
      `config.yaml`. It's written from principles plus the roles that exist;
      being loosely accurate about what the agent can reach is worse than
      saying less.
- [ ] Pick six champions — curiosity over seniority
- [ ] Create a Slack channel per squad; assign champion roles in `config.yaml`
      so nobody is blocked on permissions during the build block
- [ ] Write six scope cards (one channel / database / folder each)
- [ ] Book Demo Day, two weeks out
- [ ] Create `#radar`, assemble the watch list, rehearse the demo, keep a screen
      recording as fallback
