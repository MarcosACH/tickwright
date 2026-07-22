---
name: research
description: Investigate a question against high-trust primary sources and capture the findings as a cited Markdown file under docs/research/. Use when the user wants a topic researched, docs or API facts gathered, or reading legwork delegated to a background agent.
---

# Research

Spin up a **background agent** to do the reading, so the foreground session keeps working while it investigates. (When invoked as a `wayfinder` research ticket, this is the AFK resolution path — see `.claude/skills/wayfinder/SKILL.md`.)

The background agent's job:

1. **Investigate against primary sources** — official docs, source code, specs, first-party APIs — not a secondary write-up of them. Follow every claim back to the source that owns it. For a third-party SDK, read the docs or the source **at the version this repo pins** — never a guessed signature or a blog's paraphrase.
2. **Write the findings to a single Markdown file**, citing each claim's source (URL, or `file:line` for local source). A claim without a citation is a lead, not a finding.
3. **Save it under `docs/research/`** — one file per investigation, `docs/research/<topic-slug>.md`. Create the directory if it does not exist. If a more specific convention already exists nearby (e.g. an ADR the research feeds), match that instead and say where you put it.

Keep the output a **reference document**, not a decision: research surfaces the facts a decision waits on; the decision itself is made in `/grill-with-docs`, recorded in an ADR, or resolved on the wayfinder ticket that fired the research.
