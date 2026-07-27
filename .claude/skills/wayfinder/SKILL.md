---
name: wayfinder
description: Plan a huge chunk of work — more than one agent session can hold — as a shared map of decision tickets on the GitHub tracker, and resolve them one at a time until the way to the destination is clear.
disable-model-invocation: true
---

A loose idea has arrived — too big for one agent session, and wrapped in fog: the way from here to the **destination** isn't visible yet. Wayfinding is about finding that way, not charging at the destination. This skill charts the way as a **shared map** on the repo's GitHub tracker (`MarcosACH/tickwright`), then works its **decision tickets** — questions whose resolution is a decision, not slices of a build to execute — one at a time until the route is clear.

The destination varies per effort, and naming it is the first act of charting — it shapes every ticket. It might be a PRD to hand to `/to-spec`, a decision to lock (an ADR) before planning starts, or a change made in place like a data-structure migration. The map is domain-agnostic within this repo — engine work, an exchange integration, a recovery redesign, whatever fits the shape.

## Plan, don't do

Wayfinder is **planning** by default: each ticket resolves a decision, and the map is done when the way is clear — nothing left to decide before someone goes and does the thing (typically `/to-spec` then `/to-tickets` then `/tdd`). The pull to just do the work is usually the signal you've reached the edge of the map and it's time to hand off. An effort can override this in its **Notes** — carrying execution into the map itself — but absent that, produce decisions, not deliverables.

## Refer by name

Every map and ticket is a GitHub issue, so it has a **name** — its title. In everything the human reads — narration, the map's Decisions-so-far — refer to it by that name, never by a bare `#number` or slug. A wall of `#42, #43, #44` is illegible; names read at a glance. The number and URL don't vanish — a name wraps its link — but they ride *inside* the name, never stand in for it.

## The Map

The map is a single GitHub issue labelled `wayfinder:map` — the canonical artifact. Its tickets are **child issues** (real GitHub sub-issues) of the map.

The map is an **index**, not a store. It lists the decisions made and points at the tickets that hold their detail; a decision lives in exactly one place — its ticket — so the map never restates it, only gists it and links.

**Where the map, its child tickets, blocking, and frontier queries physically live is spelled out in [`docs/agents/issue-tracker.md` → Wayfinding operations](../../../docs/agents/issue-tracker.md#wayfinding-operations)** — the exact `gh` / REST commands for this repo. Consult it for every mechanical step below.

### The map body

The whole map at low resolution, loaded once per session. Open tickets are **not** listed — they are open child issues, found by the frontier query.

```markdown
## Destination

<what reaching the end of this map looks like — the PRD, decision, or change this effort is finding its way to. One or two lines; every session orients to it before choosing a ticket.>

## Notes

<domain; skills every session should consult; standing preferences for this effort>

## Decisions so far

<!-- the index — one line per closed ticket: enough to judge relevance, then zoom the link for the detail the ticket holds -->

- [<closed ticket title>](link) — <one-line gist of the answer>

## Not yet specified

<!-- see "Fog of war": in-scope fog you can't ticket yet; graduates as the frontier advances -->

## Out of scope

<!-- see "Out of scope": work ruled beyond the destination; closed, never graduates -->
```

### Tickets

Each ticket is a **child issue** of the map; its GitHub number is its identity. Its body is the question, sized to one fresh agent session (~100K tokens):

```markdown
## Question

<the decision or investigation this ticket resolves>
```

Each ticket carries a `wayfinder:<type>` label — one of `research`, `prototype`, `grill-with-docs`, `task` (see [Ticket Types](#ticket-types)).

**Claiming — solo-repo caveat.** The natural claim signal — assign a ticket to the session working it, and treat an unassigned open ticket as "unclaimed" — doesn't work here: this is a solo repo where **every** issue is already assigned to `@me` at creation (the assignee convention in `issue-tracker.md`), so the assignee can't double as the claim marker. Instead, **claim a wayfinder ticket by moving its project Status `Todo → In Progress`** before any work — consistent with the normal issue lifecycle. The frontier query reads Status, not assignee. Concurrent sessions are rare in a solo repo, so this is a light convention, not a lock; if you do run parallel sessions, the Status write is the thing that keeps them off each other's tickets.

Blocking uses GitHub's **native issue-dependency** relationship (`dependencies/blocked_by`), because it renders the frontier *visually* in GitHub's own UI — the human sees what's takeable without opening the map. A ticket is **unblocked** when every ticket blocking it is closed; the **frontier** is the open, unblocked, unclaimed (Status `Todo`) children — the edge of the known.

The answer isn't part of the body — it's recorded on resolution (see [Work through the map](#work-through-the-map)). Assets created while resolving a ticket (a `/research` note, a `/prototype` branch) are linked from the issue, not pasted in.

## Ticket Types

Every ticket is either **HITL** — human in the loop, worked *with* a human who speaks for themselves — or **AFK**, driven by the agent alone. A HITL ticket only resolves through that live exchange; the agent never stands in for the human's side of it (a `/grill-with-docs` agent that answers its own questions has broken this).

- **`wayfinder:research`** (AFK): Reading documentation, third-party APIs (at the pinned version), or local resources to surface a fact a decision waits on. Resolved by a **`/research` subagent**. Use when knowledge outside the current working directory is required.
- **`wayfinder:prototype`** (HITL): Raise the fidelity of the discussion by making a cheap, rough, concrete artifact to react to — a logic prototype via the `/prototype` skill. Links the prototype branch as an asset. Use when "how should this behave" (a saga table, a fill rule, an ordering) is the key question.
- **`wayfinder:grill-with-docs`** (HITL): Conversation via the `/grill-with-docs` skill, one question at a time. **The default case** — most decisions are settled by grilling, sharpening `CONTEXT.md` and writing ADRs as they crystallise.
- **`wayfinder:task`** (HITL or AFK): Manual work that must happen before a *decision* can be made — nothing to decide, prototype, or research, but the discussion is blocked until it's done. Provisioning a Hyperliquid testnet key so an API can be judged, moving data so its shape can be seen. This is the one type that *does* rather than decides — and it earns its place by unblocking a decision, not by delivering the destination. The agent drives it alone where it can (AFK); otherwise it hands the human a precise checklist (HITL). Resolved when the work is done; the answer records what was done and any resulting facts (where a key now lives, new URLs, row counts) later tickets depend on.

## Fog of war

The map is _deliberately_ incomplete: don't chart what you can't yet see. Beyond the live tickets lies the **fog of war** — the dim view of decisions and investigations you can tell are coming but can't yet pin down, because they hang on questions still open. Resolving a ticket clears the fog ahead of it, graduating whatever's now specifiable into fresh tickets — one at a time, until the way to the destination is clear and no tickets remain.

The map's **Not yet specified** section is where that dim view is written down: the suspected question, the area to revisit later. It's the undiscovered frontier _toward_ the destination — everything here is in scope, just not sharp enough to ticket. Write as loosely or as fully as the view allows; it doubles as a signpost for collaborators reading where the effort is headed.

**Fog or ticket?** The test is whether you can state the question precisely now — _not_ whether you can answer it now.

- **Ticket when** the question is already sharp — even if it's blocked and you can't act on it yet.
- **Not yet specified when** you can't yet phrase it that sharply. Don't pre-slice the fog into ticket-sized pieces: it's coarser than a ticket, and one patch may graduate into several tickets, or none, once the frontier reaches it.

**Not yet specified** excludes what's already decided (Decisions so far), what's already a live ticket, and what's out of scope (the next section).

## Out of scope

Fog only ever gathers _toward_ the destination. The destination fixes the scope, so work beyond it is **out of scope** — it isn't fog, and it doesn't belong in **Not yet specified**. It gets its own **Out of scope** section on the map: work you've consciously ruled out of _this_ effort. Scope, not sharpness, lands it here.

Out-of-scope work never graduates — the frontier stops at the destination — so it returns only if the destination is redrawn, and then as a fresh effort, not a resumption.

Ruling something out of scope is a scoping act, not a step on the route. When a ticket that already exists turns out to sit past the destination — mis-scoped in while charting, or exposed by a resolution — **close it** (a closed ticket is unambiguously off the frontier) and leave one line in the **Out of scope** section: the gist plus why it's out of scope, linking the closed ticket. It stays out of **Decisions so far**, which records the route actually walked — a scope boundary isn't a step on it.

## Invocation

Two modes. Either way, **never resolve more than one ticket per session** — with the exception of research tickets.

### Chart the map

User invokes with a loose idea.

1. **Name the destination.** Run a `/grill-with-docs` session to pin down what this map is finding its way to — the PRD, decision, or change. The destination fixes the scope, so it's settled first.
2. **Map the frontier.** Grill again, **breadth-first** this time: fan out across the whole space rather than deep on any one thread, surfacing the open decisions and the first steps takeable now. **If this surfaces no fog** — the way to the destination is already clear, the whole journey small enough for one session — you don't need a map. Stop and tell the user; hand off to `/to-spec` (or `/to-tickets`) directly.
3. **Create the map issue** (label `wayfinder:map`, added to project 2, Status `Todo`, assignee `@me`): Destination and Notes filled in, Decisions-so-far empty, the fog sketched into **Not yet specified**.
4. **Create the tickets you can specify now** as child issues of the map (create each, add to project 2, then link as a sub-issue via REST) — then wire blocking edges in a **second pass** (issues need numbers before they can reference each other). Wiring sorts them into the frontier and the blocked; everything you can't yet specify stays in the fog — the **Not yet specified** section.
5. **Fire the research subagents.** For each `wayfinder:research` ticket you just created, spin up a `/research` subagent to resolve it in parallel, capturing its cited note (per `/research`, under `docs/research/`) on a throwaway `research/<name>` branch with a context pointer from the ticket — planning artifacts stay off `main` until the effort lands. **Until**, not forever: the notes come onto `main` when the map empties (*Work through the map* step 6), so the branch is a staging area, not their home.
6. **Stop** — charting is one session's work; it hand-resolves nothing.

### Work through the map

User invokes with a map (URL or number). A ticket is **optional** — without one, you pick the next decision, not the user.

1. Load the **map** — the low-res view, not every ticket body.
2. Choose the ticket. If the user named one, use it. Otherwise take the first frontier ticket in map order. **Claim it**: move its Status to `In Progress` before any work.
3. Resolve it — **zoom as needed**: fetch the full body of any related or closed ticket on demand; invoke the skill the ticket's `wayfinder:<type>` label names (`/research`, `/prototype`, `/grill-with-docs`, or drive the task). If in doubt, use `/grill-with-docs`.
4. Record the resolution: post the answer as a **resolution comment**, **close** the issue (its Status auto-moves to `Done` on close), and **append a context pointer** to the map's Decisions-so-far (gist + link).
5. Add newly-surfaced tickets (create-then-wire); graduate any fog the answer has made specifiable, clearing each graduated patch from **Not yet specified** so it lives only as its new ticket. If the answer reveals a ticket — this one or another — sits beyond the destination, **rule it out of scope** rather than resolving it on the route. If the decision invalidates other parts of the map, update or delete those tickets.
6. **Land the research notes — once, when the frontier empties.** The moment the map has no tickets left and the destination is reachable (the hand-off to `/to-spec`), the effort has landed and step 5's "until" has expired. Open **one docs-only PR** merging `docs/research/` onto `main`, then repoint the map's research entries and each research ticket's resolution comment from `blob/<branch>/…` URLs at `main` paths. Two rules make it safe:
   - **Bodies land verbatim.** A note is dated evidence, not a maintained document; rewriting it destroys what makes it citable. Where later work refined or falsified a claim, add a **supersession header** — a short table naming what was confirmed, refined, or falsified and the ADR that now owns the answer — and leave the body untouched. Summarize the ADR's rule accurately or just link it; a header that restates a rule wrongly is worse than one that only points.
   - **Do it before the branches are the only copy.** An accepted ADR citing `docs/research/…` on a branch is one deletion away from being unciteable, and a reused branch silently 404s the link. Nothing checks these links — [#155](https://github.com/MarcosACH/tickwright/issues/155) is the worked example of both failures.
7. **Close the map** — the terminal act, once the destination's artifact exists and step 6 has run. Mechanics and the closing comment it must carry are in [`docs/agents/issue-tracker.md` → Wayfinding operations](../../../docs/agents/issue-tracker.md#wayfinding-operations), *Close the map*. The map closes on its own lifecycle, not the effort's: hand-off done, execution tracked elsewhere.

The user may run unblocked tickets in parallel, so expect other sessions to be editing the tracker concurrently.
