---
name: blast-radius
description: "Find what a change could break somewhere else before it ships, beyond the diff, and prove the one fact it's safe because of by running real code instead of writing it up. Use for 'blast radius of X', 'what could this break', or reviewing a small diff you don't trust."
disable-model-invocation: true
---

# Blast radius

Find what a change breaks somewhere else, before it ships. Use for "blast radius of X", "what could this break", or reviewing a small diff you don't trust yet.

Companion to `how` and `why`. `how` tells you what the code does. `why` tells you why it's shaped that way. Blast radius tells you what it breaks somewhere else.

Listing the callers is not the job. The agent can grep those in a second. The job is the breakage grep won't show you.

This is not `/code-review`. That skill is the merge gate and hands back ID'd BLOCKING/WARN/NIT comments the implementing agent has to resolve. This one issues no IDs, gates nothing, and never labels `ralph:ready`. Run it on a diff you don't trust, whether or not the gate has already run.

## Don't trust your own writeup

A blast-radius writeup that sounds right is worthless. It reads as convincing whether or not it's true, and that is the trap you are walking into. So don't hand back the writeup. Find the one or two facts the whole thing depends on and prove them by running code. Words are where you start, not what you ship.

### How sure are you

For each fact the change's safety depends on, get it as far down this list as is cheap, and say where it stopped.

1. You said so. Worthless on its own.
2. You pointed at the line. A real `file:line`, or the library's own source.
3. You showed the bad case can't happen. You walked the failure step by step and it doesn't reach.
4. You ran it. A script or test that calls the real code and fails loud if you're wrong.
5. You reproduced it in the running app.

Any safety fact you can't get to step 4, say so out loud. Don't write it up as settled. Step 4 is usually one small script that imports the same library the app ships and calls the exact function you're worried about.

## Steps

1. Read the change. The diff, the symbols it adds, changes, and deletes, and what it now does differently, including the part the diff doesn't spell out. Use `why` step 2 to pull the PR and commits.
2. Find the one fact it's safe because of. Most changes that look scary are safe because of a single fact, like "this call only drops already-dead cache entries and does nothing else". Find that fact. If it holds, most of the scary cases die at once. Spend your time here, not on a long list of maybes.
3. Look where grep stops. Read the source of the library you call at the version `uv.lock` pins, not the current docs for it. Work out when things run: what an `await` lets in between two lines that read as one, task cancellation and teardown order, whether `InMemoryBus` and `KafkaBus` deliver it the same way, what a saga step does on its second pass after recovery. Follow what a symbol search misses. A store column and the migration that adds it, SQLite and Postgres disagreeing under the same contract, the JSON the venue returns, an event another process replays, config that exists only as a line in `.env.example`, code three hops downstream.
4. Be honest about each risk. Give it a real chance of happening and a real cost if it does. Keep the risks you confirmed; list the ones you checked and cleared separately. Same rules as `why`. Cite a real `file:line`, a search that finds nothing is still an answer, and never make up a caller or an API.
5. Prove the one fact. Write the proof as a test under `tests/`, or as a throwaway script you run with `uv run python <path>`. Everything goes through the project `.venv`. Never install into the global environment and never add a dependency to prove a point. If the proof touches config, build `AppConfig` directly and never `AppSettings`, or an exported `TICKWRIGHT_*` in your shell decides the answer instead of the code (`src/tickwright/app/config.py`). Run it and paste what happened. A throwaway script stays throwaway. Show it in the writeup, don't commit it. If you can't prove it cheaply, mark it unproven. Don't round up.
6. For a change that fans across layers, split the reading, not the judgement. Send parallel readers at one layer each, `feed`, `strategy`, `exchange`, `engine`, `store`, the way `why` step 3 spawns investigators, then merge what they found yourself and prove the one fact once. Asking several agents the same whole question and averaging the answers buys nothing.

## Where the radius hides in this repo

- **ADRs, amendments first.** `docs/adr/` is the densest record of what a change is allowed to break, and reading one in document order hands you the retired decision. A section's `**( ... **)**` blocks carry the current truth and the prose above them is often the version it replaced, so read the blocks before the prose, or instead of it. `.agents/tools/doc-slice <file>` lists the TOC, `.agents/tools/doc-slice <file> <heading-substr>` prints one section, `--amendments` prints only the corrections. The rules and a worked counter-example are in [`docs/agents/adr-reading.md`](../../../docs/agents/adr-reading.md). A writeup built on a superseded ADR reads exactly as convincing as one built on the live rule, which is the failure this whole skill exists to catch.
- **Invariants.** Walk [`docs/agents/invariants.md`](../../../docs/agents/invariants.md), the canonical behavior lock. Every entry cites its ADR. A change that breaks one is a finding, and the ADR is the citation. Never copy the list into your writeup, link it. A copy that falls behind narrows the check silently.
- **The mechanical gates see what grep can't, and they're cheap.** `uv run lint-imports` proves the change didn't invert a dependency direction (ADR-0032). `uv run mypy .` catches the signature that moved out from under a caller. `uv run pytest` is the rest. Run them before you write about what might break, not after.

## What to hand back

- **What it does.** What changed, including the part that isn't obvious.
- **The one fact it's safe because of.** State it, say which step you got it to, and show the proof. If you couldn't prove it, write unproven.
- **Risks.** Only the real ones. Each names how it breaks, the `file:line`, how likely and how bad, and how to check. Paste the proof for the ones that matter.
- **Cleared.** What you checked and why it's fine.
- **Before you merge.** The cheapest test or repro that catches the real bug, including the script you wrote.

Write it through `unslop` and cite real code. Nothing secret goes in the writeup. No signing key, no funded testnet address, no pasted `.env`. `TICKWRIGHT_HYPERLIQUID__SIGNING_KEY` is env-only and redacted from logs, so don't be the one who prints it.

**Reply:** the writeup above, with the one safety fact either proven or marked unproven.