---
name: module-map
description: Design an upfront module map for a planned feature, informed by the PRD and domain glossary. Use after PRD is approved but before implementation starts, to establish architectural anchors for agents.
---

# Module Map

Design the architecture **before** code exists. Output is a module map document that serves as the architectural anchor for agents during implementation.

The PRD describes *what* to build. The module map describes *the shape* it lives in.

## Glossary

Use these terms exactly. Consistent language is the point — don't drift into "component," "service," "API," or "boundary."

- **Module** — anything with an interface and an implementation (function, class, module, package, slice).
- **Interface** — everything a caller must know to use the module: types, invariants, error modes, ordering, config. Not just the type signature.
- **Implementation** — the code inside.
- **Depth** — leverage at the interface: a lot of behaviour behind a small interface. **Deep** = high leverage. **Shallow** = interface nearly as complex as the implementation.
- **Seam** — where an interface lives; a place behaviour can be altered without editing in place.
- **Adapter** — a concrete thing satisfying an interface at a seam.
- **Leverage** — what callers get from depth.
- **Locality** — what maintainers get from depth: change, bugs, knowledge concentrated in one place.

Key principles:

- **Deletion test (upfront variant)**: imagine the module doesn't exist. If complexity scatters across N callers, the module earns its keep. If callers stay simple, it's a pass-through — collapse it.
- **The interface is the test surface.**
- **One adapter = hypothetical seam. Two adapters = real seam.** Don't introduce a seam without two real adapters in scope.

## Process

### 1. Gather context

Read in this order:
- The PRD (passed as argument or in conversation context)
- `CONTEXT.md` (domain glossary) — module names MUST come from here
- `docs/adr/` — respect existing architectural decisions

For `CONTEXT.md` and ADRs, use `.agents/tools/doc-slice <file> [heading-substr]` (TOC first, then the sections relevant to the feature) rather than whole-file reads.

### 2. Draft module shapes

Walk the PRD's user stories. For each unit of behaviour, ask: *what module owns this?*

Propose a numbered list of modules. For each:

- **Name** — from the domain glossary in `CONTEXT.md`
- **Interface** — what callers need to know (types, invariants, error modes)
- **Responsibilities** — what behaviour lives inside
- **Seams** — where adapters might plug in (only if 2+ real adapters exist)
- **Depth signal** — apply the deletion test; what concentrates here?

Do NOT propose interfaces yet. The shape comes first.

### 3. Quiz the user (per module)

Drop into a grilling loop. Walk the design tree one module at a time.

For each module, ask:

- Is the grain right? (too coarse / too fine)
- Are the responsibilities cohesive, or is this two modules wearing one hat?
- Where are the real seams? (two adapters, not one)
- What sits behind the seam vs. inline?
- Does the deletion test pass — would removing this module scatter complexity?
- Are there modules missing from the map?

Side effects happen inline as decisions crystallize:

- **Naming a module after a concept not in `CONTEXT.md`?** Add the term right there.
- **Sharpening a fuzzy term during the conversation?** Update `CONTEXT.md`.
- **User rejects a module shape with a load-bearing reason?** Offer an ADR, framed as: *"Want me to record this as an ADR so future module-map sessions don't re-suggest it?"* Only when the reason would be needed by a future explorer.

Iterate until the user approves the map.

### 4. Output the module map

Save to `docs/module-maps/<feature-slug>.md`. Use the template below.

<module-map-template>
# Module Map: <Feature Name>

## Source

Reference to the PRD this map implements.

## Modules

### <Module Name>

**Interface:** What callers must know — types, invariants, error modes, ordering.

**Responsibilities:** What behaviour lives inside this module.

**Seams:** Adapters that plug in here (only list seams with 2+ real adapters).

**Depth note:** What concentrates here. Why the deletion test passes.

---

(repeat per module)

## Dependency graph

```
ModuleA → ModuleB
ModuleB → ModuleC
```

A plain ASCII or mermaid graph showing which modules depend on which. Cycles are a smell — call them out.

## Out of scope

Modules considered and rejected, with one-line reason. Prevents re-litigation.
</module-map-template>

The module map is an **architectural anchor**: agents reference it during implementation to stay inside the shape. Do NOT prescribe internal implementation — only the interface, responsibilities, and seams.
