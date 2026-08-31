"""``.agents/tools/doc-slice``: section slicing, and the ``--amendments`` inversion.

The ADR corpus is *append-corrected* — a section's closing ``**(…**)**`` blocks carry
the current truth and the prose above them is frequently the retired version, so a
reading rule that follows document order reads the superseded text. ``--amendments``
inverts that: it prints the correction blocks and drops the prose they superseded.
The decision and its rationale live in ``docs/agents/adr-reading.md``; this file pins
the mechanism, it does not redefine it.

Two things are asserted that a hand-written fixture could not:

* the **regression** the rule exists for — ADR-0040 §4's two field defaults and §5's
  placement exist *only* inside amendment blocks (issue #266), so each is read back
  through the tool rather than assumed;
* **balance across the whole corpus**, parametrised over ``docs/adr/*.md`` as it is on
  disk. The opener vocabulary is arbitrary prose and the counts drift with every new
  amendment, so the fixture is generated from the corpus rather than transcribed.

Driven through a real shell — the subject is a shell tool's stdout and exit codes, so
nothing here is mocked (a process boundary).
"""

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_TOOL = _ROOT / ".agents" / "tools" / "doc-slice"
_ADR_DIR = _ROOT / "docs" / "adr"
_ADR_0040 = _ADR_DIR / "0040-reported-margin-leverage-liquidation-model.md"

# Every ADR on disk, so a new one joins the balance check by existing.
_ADRS = sorted(_ADR_DIR.glob("*.md"))

# One emitted block per opener: the tool's own accounting, checked against the raw
# delimiter count rather than against a number written down here.
_OPENER = "**("
_CLOSER = "**)**"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run doc-slice, never raising — the exit code is part of what is asserted."""
    return subprocess.run(
        [str(_TOOL), *args], capture_output=True, text=True, check=False, cwd=_ROOT
    )


def _blocks(stdout: str) -> list[str]:
    """Split the emitted blocks on their ``--- <line>  <heading>`` banners.

    Anchored to a line start, as the banner is: a block body may itself contain
    ``--- ``, and splitting on it anywhere would miscount the very thing the corpus
    balance test counts.
    """
    return [chunk for chunk in re.split(r"(?m)^--- ", stdout) if chunk.strip()]


def _body(block: str) -> str:
    """The block's text, with its ``<line>  <heading>`` banner line removed."""
    return block.split("\n", 1)[1].rstrip("\n")


def _mask_code(body: str) -> str:
    """Replace code spans and fences with an asterisk-free placeholder.

    Masked, never deleted: deleting ``*`SQLiteStore`*`` would join its two italic
    markers into a phantom ``**`` and report a well-formed block as broken (issue
    #269). A backtick span carries no emphasis, so its interior must not be counted.
    """
    fenced = re.sub(r"(?ms)^```.*?^```", "CODE", body)
    return re.sub(r"`[^`]*`", "CODE", fenced)


class TestSectionSlicing:
    """The pre-existing modes, pinned so ``--amendments`` cannot regress them."""

    def test_toc_lists_line_level_and_heading(self) -> None:
        result = _run(str(_ADR_0040))

        assert result.returncode == 0
        assert "  2  4. Flat maintenance margin" in result.stdout

    def test_a_section_stops_at_the_next_same_or_higher_heading(self) -> None:
        result = _run(str(_ADR_0040), "5. Leverage and margin mode")

        assert result.returncode == 0
        assert result.stdout.startswith("## 5. Leverage and margin mode")
        assert "## 6." not in result.stdout

    def test_an_unmatched_heading_exits_one(self) -> None:
        result = _run(str(_ADR_0040), "no such heading")

        assert result.returncode == 1
        assert "no heading matches" in result.stderr


class TestAmendmentsRegression:
    """The two facts issue #266 named: read them back, do not assume them."""

    def test_section_4_yields_both_field_defaults(self) -> None:
        """`max_leverage` defaults to 1 and `margin_maint` to 0 — stated only in the block."""
        result = _run("--amendments", str(_ADR_0040), "4. Flat maintenance margin")

        assert result.returncode == 0, result.stderr
        assert "`margin_maint` defaults to **`0`**" in result.stdout
        assert "`max_leverage` defaults to **`1`**, *not* `0`" in result.stdout

    def test_section_5_yields_the_appconfig_placement(self) -> None:
        """The block's home is `AppConfig.leverage`; `PaperExchangeConfig` only as retracted."""
        result = _run("--amendments", str(_ADR_0040), "5. Leverage and margin mode")

        assert result.returncode == 0, result.stderr
        assert "**`AppConfig.leverage: dict[str, LeverageSpec]`**" in result.stdout
        # The superseded placement survives only inside its own retraction, never as a
        # standing statement of where the block lives.
        assert "This ADR placed it in `PaperExchangeConfig`. That is wrong" in result.stdout

    def test_the_superseded_prose_is_dropped(self) -> None:
        """§5's prose says where the block is *not*; amendments-only must not carry it."""
        section = _run(str(_ADR_0040), "5. Leverage and margin mode").stdout
        amendments = _run("--amendments", str(_ADR_0040), "5. Leverage and margin mode").stdout

        assert "which stays the identical venue-metadata shape" in section
        assert "which stays the identical venue-metadata shape" not in amendments
        assert len(amendments) < len(section)


class TestAmendmentsMechanics:
    def test_each_block_is_banner_labelled_with_its_line_and_section(self) -> None:
        result = _run("--amendments", str(_ADR_0040))

        assert result.returncode == 0, result.stderr
        banners = [ln for ln in result.stdout.splitlines() if ln.startswith("--- ")]
        assert len(banners) == _ADR_0040.read_text().count(_OPENER)
        assert any("4. Flat maintenance margin" in ln for ln in banners)
        assert any("5. Leverage and margin mode" in ln for ln in banners)

    def test_a_file_with_no_amendments_prints_nothing(self, tmp_path: Path) -> None:
        doc = tmp_path / "plain.md"
        doc.write_text("# Title\n\nOrdinary prose with **bold** in it.\n")

        result = _run("--amendments", str(doc))

        assert result.returncode == 0
        assert result.stdout == ""

    def test_a_bold_run_before_a_paren_is_not_a_closer(self, tmp_path: Path) -> None:
        """`(paper **generates**, live **ingests**)` ends a bold run, not a block.

        ADR-0043 carries two of these. Keying on the delimiter *pair* rather than on a
        naive `**)` match is what keeps them out: a closer is only looked for once an
        opener has been seen.
        """
        doc = tmp_path / "falsepos.md"
        doc.write_text(
            "# Title\n\n"
            "Two ingress paths (paper **generates**, live **ingests**). Prose.\n\n"
            "**(Amended by ADR-0044:** the real one.**)** Trailing prose.\n"
        )

        result = _run("--amendments", str(doc))

        assert result.returncode == 0, result.stderr
        assert _blocks(result.stdout) == [
            "5  Title\n**(Amended by ADR-0044:** the real one.**)**\n"
        ]

    def test_several_blocks_on_one_line_are_split(self, tmp_path: Path) -> None:
        """ADR-0038 §2 runs three back-to-back on a single line."""
        doc = tmp_path / "run.md"
        doc.write_text(
            "# Title\n\n"
            "- Prose. **(Answered by ADR-0042 §2:** first.**)** **(Refined by ADR-0040:** "
            "second.**)** **(Extended by ADR-0044:** third.**)**\n"
        )

        result = _run("--amendments", str(doc))

        assert result.returncode == 0, result.stderr
        assert len(_blocks(result.stdout)) == 3
        assert "third.**)**" in result.stdout
        assert "Prose." not in result.stdout

    def test_a_block_spanning_lines_is_emitted_whole(self, tmp_path: Path) -> None:
        doc = tmp_path / "multiline.md"
        doc.write_text(
            "# Title\n\n"
            "**(Corrected by #142:** the opener.\n\n"
            "| lower bound | max leverage |\n"
            "|---|---|\n"
            "| $0 | 40x |\n\n"
            "the closer.**)**\n\nAfter.\n"
        )

        result = _run("--amendments", str(doc))

        assert result.returncode == 0, result.stderr
        assert "| $0 | 40x |" in result.stdout
        assert "After." not in result.stdout

    def test_a_fenced_sample_inside_a_block_survives(self, tmp_path: Path) -> None:
        """The fence skip guards *openers*; inside a block it would elide the evidence.

        ADR-0040 §4's tier-crossing correction is carried by a fenced measurement, and
        an amendment printed without it states a conclusion with nothing behind it —
        the silent drop this flag exists to prevent.
        """
        doc = tmp_path / "fenced.md"
        doc.write_text(
            "# Title\n\n"
            "**(Corrected by #152:** the flat rate is falsified above the first band:\n\n"
            "```\n"
            "flat 1/(2·40) = 0.0125 -> 150.9207   x\n"
            "notional × 0.02 − 75  = 166.4731     ok\n"
            "```\n\n"
            "which is R3's form.**)**\n\nAfter.\n"
        )

        result = _run("--amendments", str(doc))

        assert result.returncode == 0, result.stderr
        assert "notional × 0.02 − 75  = 166.4731     ok" in result.stdout
        assert result.stdout.count("```") == 2
        assert "After." not in result.stdout

    def test_an_opener_inside_a_code_fence_starts_no_block(self, tmp_path: Path) -> None:
        """Why the fence skip exists: a delimiter shown in a sample is not a block."""
        doc = tmp_path / "sample.md"
        doc.write_text(
            "# Title\n\n"
            "```\n"
            "doc-slice --amendments <file>   # blocks read **(like this**)**\n"
            "```\n\n"
            "Ordinary prose.\n"
        )

        result = _run("--amendments", str(doc))

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_an_unclosed_block_reports_its_opening_line_and_exits_three(
        self, tmp_path: Path
    ) -> None:
        """A drifted corpus fails loudly: silent under-elision is worse than no flag."""
        doc = tmp_path / "unclosed.md"
        doc.write_text("# Title\n\nProse.\n\n**(Amended by ADR-0044:** never closed.\n")

        result = _run("--amendments", str(doc))

        assert result.returncode == 3
        assert "unclosed amendment block" in result.stderr
        assert "line 5" in result.stderr

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        result = _run("--amendments", str(tmp_path / "absent.md"))

        assert result.returncode == 2
        assert "not a file" in result.stderr

    def test_no_arguments_prints_the_whole_usage_header(self) -> None:
        """Usage ends where the header does, not at a transcribed line number.

        A hardcoded range truncates in silence the moment the header grows, and the
        part it drops is the tail — where the domain note that answers the misuse is.
        """
        result = _run()

        assert result.returncode == 2
        assert "doc-slice --amendments <file>" in result.stderr
        assert "outside the domain" in result.stderr
        assert "set -euo pipefail" not in result.stderr


class TestCorpusBalance:
    """Generated from `docs/adr/` as it stands, not transcribed from issue #266.

    The counts drift with every new amendment, so the assertion is the invariant —
    one emitted block per opener, every block closed — rather than a number.
    """

    def test_the_corpus_is_not_empty(self) -> None:
        assert len(_ADRS) >= 50

    @pytest.mark.parametrize("adr", _ADRS, ids=lambda p: p.stem)
    def test_every_opener_pairs_with_a_closer(self, adr: Path) -> None:
        text = adr.read_text()
        result = _run("--amendments", str(adr))

        assert result.returncode == 0, result.stderr
        assert len(_blocks(result.stdout)) == text.count(_OPENER)
        assert text.count(_OPENER) == text.count(_CLOSER)

    @pytest.mark.parametrize("adr", _ADRS, ids=lambda p: p.stem)
    def test_every_emitted_block_is_verbatim_from_the_source(self, adr: Path) -> None:
        """A block is one contiguous range of the file, so nothing may drop out of it.

        Stronger than any per-feature assertion: whatever the printer learns to skip
        next — fences, comments, tables — a body that is no longer a substring of the
        source is an elision, and elision is the failure mode this flag exists to avoid.
        """
        text = adr.read_text()
        result = _run("--amendments", str(adr))

        assert result.returncode == 0, result.stderr
        for block in _blocks(result.stdout):
            assert _body(block) in text

    @pytest.mark.parametrize("adr", _ADRS, ids=lambda p: p.stem)
    def test_every_emitted_block_has_even_emphasis_parity(self, adr: Path) -> None:
        """``**`` runs come out even, so the block renders the emphasis it was written with.

        Delimiter balance — asserted above — does not catch this: an opener that never
        closes its own bold run leaves the whole block off by one, so the phrases meant
        to be bold render plain and the connective prose renders bold, with a literal
        ``**`` leaking past the closer (issue #269, ADR-0040 §5). The rule is the
        *Shape* bullet of ``docs/agents/adr-reading.md``; this makes it enforced rather
        than remembered.
        """
        result = _run("--amendments", str(adr))

        assert result.returncode == 0, result.stderr
        for block in _blocks(result.stdout):
            banner, body = block.split("\n", 1)
            assert _mask_code(body).count("**") % 2 == 0, f"{adr.name} block at {banner}"
