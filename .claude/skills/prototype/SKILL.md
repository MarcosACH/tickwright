---
name: prototype
description: Build a throwaway logic prototype to answer a design question — a tiny interactive terminal app that drives a state model by hand. Use when the user wants to sanity-check whether a state machine, saga, or data shape feels right before committing to it.
---

# Prototype

A prototype is **throwaway code that answers a question**. Tickwright is a headless engine, so there is exactly one shape of question worth prototyping here: **"does this logic / state model feel right?"** — a saga transition table, a reconciliation rule, a fill model, an event ordering. Build a tiny interactive terminal app that pushes the state machine through cases that are hard to reason about on paper. (There is no UI branch — the engine has no UI.)

If the question is really about wording, config, or API ergonomics rather than *behavior under cases*, a prototype is the wrong tool; use `/grill-with-docs` or a `/research` note instead.

## Rules

1. **Throwaway from day one, and clearly marked as such.** Put the prototype next to the module it's prototyping for (so context is obvious), named so a casual reader sees it's a prototype, not production — e.g. `src/tickwright/<area>/_prototype_<question>.py` or a `prototypes/` scratch dir. Never wire it into the package's `__all__` or import it from real code.
2. **One command to run**: `uv run python <path>`. The user must be able to start it without thinking. Don't add a new task runner or dependency just for the prototype — stay inside `uv`.
3. **No persistence by default.** State lives in memory. Persistence is the thing a prototype *checks*, not something it depends on. If the question is specifically about the `Store`, hit a `SQLiteStore(":memory:")` or a clearly-named `PROTOTYPE-wipe-me.db`, never a real store.
4. **Skip the polish.** No tests, no error handling beyond what makes it runnable, no abstractions. The point is to learn something fast.
5. **Surface the state.** After every action, print the full relevant state so the user sees exactly what changed.
6. **Match the domain types that matter to the question, skip the rest.** Use `Decimal` for prices/quantities (ADR-0029) and the real `OrderState` / event names from `CONTEXT.md` when the question is about them — a prototype that lies about the types can't answer the question. Skip the real bus, real clock, and real async wiring unless *they* are the question.

## Process

### 1. State the question

Before writing code, write down — in a comment at the top of the file — what state model you're prototyping and what question you're answering. One paragraph. A logic prototype that answers the wrong question is pure waste; make the question explicit so it can be checked later, whether the user is watching now or returning to it AFK.

### 2. Isolate the logic in a portable, pure module

Put the bit that's actually answering the question behind a small, **pure** interface that could be lifted out and dropped into the real codebase later. The TUI around it is throwaway; the logic module shouldn't be. Pick the shape that fits the question:

- **A pure reducer** — `(state, action) -> state`. Good when actions are discrete events and state is a single value (a saga stepping through order events).
- **A state machine** — explicit states and legal transitions. Good when "which actions are even legal right now" is part of the question.
- **A small set of pure functions** over a plain dataclass. Good when there's no implicit current state — just transformations (a fill model over a book snapshot).

Keep it pure: no I/O, no terminal code, no `print` for control flow. The TUI imports it and calls into it; nothing flows the other way. This is what makes the prototype useful past its own lifetime — the validated reducer / machine / function set lifts into the real module on its own.

### 3. Build the smallest TUI that exposes the state

A lightweight loop that, on every tick, clears the screen (`print("\033[2J\033[H", end="")`) and re-renders the whole frame — the user should always see one stable view, not an ever-growing scrollback. Each frame, in this order:

1. **Current state**, pretty-printed one field per line (or formatted with `pprint` / `dataclasses.asdict`). Bold field names with `\x1b[1m…\x1b[0m`, dim derived/context values with `\x1b[2m…\x1b[0m`. No styling library needed.
2. **Keyboard shortcuts** at the bottom: `[f] fill  [c] cancel  [r] reconcile  [q] quit`.

Behaviour: initialise a single in-memory state object and render frame one; read one keystroke (or one `input()` line) at a time and dispatch to a handler that produces the next state; re-render the full frame after every action; loop until quit. The whole frame should fit on one screen.

### 4. Hand it over

Give the user the run command (`uv run python <path>`). They drive it; the interesting moments are "wait, that shouldn't be possible" or "huh, I assumed X" — those are bugs in the **idea**, which is the whole point. Add actions if they ask; prototypes evolve.

### 5. Capture the answer and the prototype

Once it has answered its question:

1. **Fold the validated decision into the real code** — the pure reducer / machine / function set lifts into the real module; record the decision where it belongs (an ADR for a load-bearing one, otherwise the driving issue or commit).
2. **Capture the prototype itself as a primary source**: commit it to a throwaway `prototype/<name>` branch, out of `main`, and leave a context pointer to that branch on the implementation issue. Capture the **answer** too — the verdict and the question it settled. `main` keeps only the validated decision, never the TUI shell.

## Anti-patterns

- **Don't add tests.** A prototype that needs tests is no longer a prototype.
- **Don't wire it to the real bus, exchange, or store** unless that wiring is the question. In-memory only.
- **Don't generalise.** No "what if we wanted X later." One question.
- **Don't blur the logic and the TUI.** If the reducer references `print` or escape codes, it's no longer portable. Keep the TUI a thin shell over a pure module.
- **Don't ship the TUI shell into production.** The logic module behind it is the only part worth keeping.
