# Reading append-corrected ADRs

`docs/adr/` is **append-corrected**. A decision is never rewritten in place; it is followed by a
`**( … **)**` amendment block that narrows, extends or withdraws it. So the closing blocks carry
the current truth and the prose above them is frequently the retired version, which inverts the
usual reading order: **read a section's amendments first, its prose second or not at all.**

```bash
.agents/tools/doc-slice --amendments <file>                    # every block, banner-labelled by section
.agents/tools/doc-slice --amendments <file> <heading-substr>   # one section's blocks
```

Each block prints under a `--- <line>  <heading>` banner, so `| grep '^---'` is a cheap index of
which sections were ever corrected: 986 characters for ADR-0040's 17 blocks, against 45,602 for the
file. `doc-slice <file> <heading>` still prints the whole section when the original reasoning is
what you need. `/why` wants the full history; `/tdd` and `/code-review` want the resolved state.

This is the mechanism the reading rules in [`CLAUDE.md` → Context Discipline](../../CLAUDE.md) and
[`.claude/skills/tdd/SKILL.md`](../../.claude/skills/tdd/SKILL.md) point at. Decided in
[#266](https://github.com/MarcosACH/tickwright/issues/266).

## Why the inversion, and not a stop-reading rule

The rule this replaces was "read the section, stop at the first amendment marker". Measured against
the corpus it is worse than no rule, because the facts a slice needs live *inside* the blocks.
ADR-0040 is the worked case, and both halves of issue #190 shipped from it:

- **§4** states no default for either field it adds. `max_leverage` defaults to `1` (**not** `0`,
  which would make ADR-0044 §9's `1 ≤ leverage ≤ max_leverage` unsatisfiable) and `margin_maint`
  defaults to `0`. Both appear only in the section's closing block.
- **§5** says where the per-symbol leverage block is *not* (`InstrumentSpec`) and never says where
  it is. That it is a venue-agnostic `AppConfig.leverage: dict[str, LeverageSpec]`, #190's central
  design decision, is stated only in the `**(Amended by ADR-0044 §2` block. The superseded
  `PaperExchangeConfig` placement sits in the ADR's *Consequences*.

An agent reading in document order and stopping at the markers gets the retired placement and no
defaults. `tests/test_doc_slice.py` reads both facts back through the tool, so the regression that
motivated this cannot return quietly.

Two alternatives were weighed and dropped. A maintained "current state" block per section has the
highest fidelity and adds a docs-sync obligation to every future amendment, which is a standing cost
paid forever for a reading convenience. Prose guidance alone, with no tooling, has nothing to fail
when an agent skips it. One mechanism ships, per the repo's two-implementations bar.

## Why the delimiter pair, and never the opening words

Blocks are unreliably *opened* and reliably *delimited*. Measured 2026-08-31 across 50 files: 76
openers, 35 of which begin a line and 41 of which are mid-paragraph, with **36 distinct first
words**. Several are not verbs at all (`**(Neither field's *default* …`, `**(Two of those three
reasons …`, `**(Six landed …`, `**(stop() …`). That is arbitrary prose, not an extensible word list,
and any detector keyed on it under-elides in silence.

`**(` … `**)**` holds instead. Regenerate the census rather than trusting these numbers, which drift
with every amendment:

```bash
python3 - <<'PY'
import pathlib
for p in sorted(pathlib.Path("docs/adr").glob("*.md")):
    t = p.read_text()
    print(p.name, t.count("**("), t.count("**)**"))
PY
```

Two details the tool depends on:

- **A closer is only looked for after an opener.** A bold run that ends just before a `)` produces a
  bare `**)` with no block in sight (ADR-0043's `(paper **generates**, live **ingests**)`, and nine
  more across the corpus). Scanning for closers only while inside a block drops all of them.
- **The closer is exactly `**)**`.** Four sites closed with a bare `**)` or a `)**` and were
  normalised in #266 (ADR-0035 *Placement*, ADR-0038 *`AccountSpec`* and *Account exclusivity*,
  ADR-0043 §1). Two of them needed the opener's bold run closed as well — appending the closer
  alone left a dangling `**` in the rendered text, which is why the shape below is stated as a
  pair of rules and not one. All 50 files now balance, and `tests/test_doc_slice.py` asserts that
  per file over `docs/adr/*.md` as it stands, so a new ADR joins the check by existing.

## Writing an amendment

A block is a delimiter pair living **inside** a bold run, so both halves have to hold:

- **Shape.** Open with `**(`, close the opener's own bold run at the end of its header
  (`**(Amended by ADR-0044 §2:**`), leave the body plain, and close with `**)**` and nothing else.
- **The `**` runs in the block come out even.** `**(header**)**` is three runs, not four: the first
  two pair, the third has nothing to close, and the reader gets a literal `**` after the `)`. The
  short inline note is where this bites, having no header colon to invite the close — fold the
  clause the note annotates into the block body rather than closing the opener early. That reads
  better through the flag too, which would otherwise print a block with no content in it.

`doc-slice --amendments` exits **3** naming the opening line when a block is left unclosed, which is
also what the corpus test fails on. Failing loudly is the point: a detector that silently drops a
correction is worse than the raw file. Nothing is elided from *inside* a block either — fenced
samples included, since a fenced measurement is often the evidence the amendment rests on — and
`tests/test_doc_slice.py` holds every emitted body to being a verbatim substring of its source.

Amendment text is 16.8 % of the corpus (87,768 of 523,871 characters, 76 blocks running 265 to
3,645 characters, median 945), so on a lightly-corrected section the flag is most of the saving and
on a heavily-corrected one it is little. ADR-0040 §4 is 76 % amendment by character. There the flag
buys correctness, not bytes, and that was always the more expensive of the two to get wrong.
