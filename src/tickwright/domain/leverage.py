"""The per-symbol leverage & margin-mode input (ADR-0040 §5, ADR-0044 §2).

The one *operator-authored* value in a package otherwise made of outputs, which
is why it gets its own module: filing it under ``instrument.py`` (identical
venue metadata across paths) or ``position.py`` (an output) invites exactly the
confusion those two ADRs spent two sections preventing.

**Venue-agnostic and per-symbol.** Its consumer, the ``PortfolioProjection``
margin model, is venue-agnostic and needs it on both paths, so it can never
live in a venue's config block — a ``TICKWRIGHT_PAPER__*`` value must never
govern a live run (ADR-0042 §1).

Two values, not one: a ``LeverageSpec`` is what an operator writes for a symbol,
and a ``LeverageBook`` is the **resolved** map both consumers receive. The
distinction is the module's whole reason to be a type rather than a
``Mapping`` — see ``LeverageBook``.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, Self

from .errors import LeverageOutOfBounds
from .instrument import InstrumentSpec

type MarginMode = Literal["cross", "isolated"]
"""How a symbol's collateral is posted: pooled across the account, or ring-fenced
per position. The venue's own two states, spelled as it spells them."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LeverageSpec:
    """One symbol's margin mode and leverage — **one value, not two maps**.

    ``updateLeverage {asset, isCross, leverage}`` sets both in a single signed
    action (ADR-0044 §2), so splitting them into two per-symbol maps would let
    config express a state (mode set, leverage unset) the venue has no way to
    hold, and would need a rule for reconciling the two halves at every read.

    Defaults are ADR-0040 §5's safest pair — ``1x`` isolated, full-notional
    collateral per position — so an absent entry is a complete conservative
    specification rather than a hole. Leverage is thus off by default and opted
    into per symbol.
    """

    mode: MarginMode = "isolated"
    leverage: int = 1


DEFAULT_LEVERAGE: Final = LeverageSpec()
"""ADR-0040 §5's safest pair, named once.

Both halves of "what does a symbol nobody configured get" — the resolution that
fills it in and the read that misses the book — answer with *this* object, so
the two cannot drift apart by being two literals in two modules.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class LeverageBook:
    """The **resolved** map: one ``LeverageSpec`` per strategy-traded symbol.

    The sparse thing an operator writes (``AppConfig.leverage``) and the
    complete thing both consumers must receive are different values, and until
    this type they were the same one — a ``Mapping[str, LeverageSpec]`` that
    carried its completeness in prose at six signatures and in a comprehension
    at the composition root. A ``LeverageBook`` says it instead: ``resolve`` is
    the completion, so a consumer holding one holds a map that has been through
    it.

    What it does **not** claim is which symbol set it was resolved over — that
    input is ``AppConfig.strategies``, which no ``domain`` type may see
    (ADR-0044 §2: an ``Exchange`` knows nothing of strategies). The root passes
    that set in; the book owns everything downstream of it, which is the
    default an unnamed symbol takes, the read that misses, and the bound.

    An **empty** book is a legitimate value, not an unresolved one: it is a run
    with nothing traded, and it is what a direct construction that never
    exercises ``validate_against`` takes rather than carrying a resolution only
    the root can do.
    """

    entries: Mapping[str, LeverageSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Copied rather than aliased: the book is frozen so that two consumers
        # cannot disagree about a symbol, and sharing the caller's dict would
        # leave that guarantee to whoever still holds the other reference.
        object.__setattr__(self, "entries", dict(self.entries))

    @classmethod
    def resolve(cls, configured: Mapping[str, LeverageSpec], *, traded: Iterable[str]) -> Self:
        """Complete the sparse ``configured`` map over the ``traded`` symbol set.

        The scope is every symbol the configured strategies declare (ADR-0044
        §3), so each unconfigured traded symbol takes ``DEFAULT_LEVERAGE``
        rather than being left for a consumer to interpret — the resolution is
        what keeps the margin model and the venue reading the same numbers by
        construction, because neither one decides.

        Config validation has already refused an entry naming an untraded
        symbol, so this never has to decide what a dead key means: a
        ``configured`` entry outside ``traded`` is silently dropped here, which
        is unreachable from the composition root.
        """
        return cls(entries={symbol: configured.get(symbol, DEFAULT_LEVERAGE) for symbol in traded})

    def for_symbol(self, symbol: str) -> LeverageSpec:
        """The margin mode and leverage this run computes ``symbol`` against.

        Falls back to the same ``DEFAULT_LEVERAGE`` the resolution fills in, so
        a read can never be more permissive than the map, whatever the caller
        asks for. A symbol outside the traded set has no position to value in
        the first place.
        """
        return self.entries.get(symbol, DEFAULT_LEVERAGE)

    def validate_against(self, specs: Mapping[str, InstrumentSpec]) -> None:
        """Refuse a book any instrument cannot carry (ADR-0044 §9).

        Two conditions, one pass, one raise. Every symbol in the book must have
        an ``InstrumentSpec`` — the book is the strategy-traded set, so a symbol
        the exchange publishes no spec for is a symbol this run cannot trade at
        all — and its leverage must satisfy ``1 ≤ leverage ≤ spec.max_leverage``,
        inclusive at both ends.

        The two are reported as **separate clauses** because they send an
        operator to different places: an out-of-bounds leverage is theirs to
        lower, while a missing spec is a hole in the exchange's instrument
        universe that the leverage they never configured had nothing to do with.

        One shared implementation because ADR-0044 §9 requires the two paths to
        refuse *identically*: leaving each adapter its own check is how paper
        comes to accept a leverage live rejects, and the divergence then
        surfaces only on promotion. The same argument that put
        ``below_min_notional`` in ``domain``.

        Raising rather than returning a verdict: a caller has nothing to decide.
        Every offence is collected before raising, so one start reports them all.
        """
        unbounded: list[str] = []
        out_of_bounds: list[str] = []
        for symbol in sorted(self.entries):
            spec = specs.get(symbol)
            if spec is None:
                unbounded.append(symbol)
                continue
            configured = self.entries[symbol].leverage
            if not 1 <= configured <= spec.max_leverage:
                out_of_bounds.append(f"{symbol} {configured} not in 1..{spec.max_leverage}")
        if not unbounded and not out_of_bounds:
            return
        clauses = ["leverage refused at startup (ADR-0044 §9)."]
        if unbounded:
            clauses.append(
                "The exchange publishes no InstrumentSpec for these traded symbols, so nothing "
                f"bounds them (ADR-0031): {', '.join(unbounded)}."
            )
        if out_of_bounds:
            clauses.append(
                f"Configured leverage outside its instrument's bound: {'; '.join(out_of_bounds)}."
            )
        raise LeverageOutOfBounds(" ".join(clauses))


EMPTY_LEVERAGE_BOOK: Final = LeverageBook()
"""A book with nothing in it — the default every direct construction takes.

Not an *unresolved* book: it is a run with nothing traded, so ``for_symbol``
answers ``DEFAULT_LEVERAGE`` for everything and ``validate_against`` has nothing
to refuse. Named here so the five consumers that default it share one object
rather than each building a call into its own signature.
"""
