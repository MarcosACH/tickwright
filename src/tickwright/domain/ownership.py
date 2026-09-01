"""Which strategy owns which symbol — ADR-0034's disjointness rule as a value.

On a ``NET`` (one-way) venue two strategies over one symbol are netted into a
single real position: one's close silently moves the other's exposure and
liquidation is account-wide, so engine-side per-strategy books stay
arithmetically consistent while describing an isolation the venue does not
provide. Same-symbol isolation is a **separate account** — the venue-native
primitive — and therefore a separate process (ADR-0038). v1 ships ``NET`` only
(paper, and Hyperliquid, whose positions are ``oneWay``), so the rule is
unconditional rather than asked of the ``Exchange``; a ``HEDGE`` adapter is the
documented extension point that would relax it.

The rule gets a **type** rather than a comparison at each site because two
layers refuse it and they cannot share an exception. ``AppConfig`` refuses a
*configured* overlap at load, where pydantic turns a ``ValueError`` into a
``ValidationError``; ``StrategyHost.register`` refuses a *registered* one, where
the fail-fast class is ``InvariantViolation``. What must not differ between them
is the rule and the sentence it refuses with — so those live here and each
caller raises its own type. Without it the index, the sort and the wording exist
twice, in two layers, which is the state the reserved-``UNATTRIBUTED`` gate is
in one module over. The same split ``LeverageBook`` already makes between its
config-load dead-entry check and its ``Exchange.start()`` bound.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Self


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolOwnership:
    """Symbol → the strategy that declared it, plus the one refusal over it.

    Frozen and rebound rather than mutated, so a refused claim leaves nothing
    behind: a caller that raises never has to undo a half-recorded declaration,
    and the ``AppConfig`` validator can keep folding past one to report every
    offender in a single load.

    Deliberately not keyed by account. One account per process (ADR-0038) makes
    "disjoint per account" read as "disjoint process-wide", and a second key
    would be a dimension every caller has to supply the same constant for.
    """

    owners: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Copied rather than aliased, for the reason ``LeverageBook`` copies:
        # the value is frozen so two holders cannot disagree about a symbol, and
        # sharing the caller's dict leaves that to whoever kept the reference.
        object.__setattr__(self, "owners", dict(self.owners))

    def refusal(self, claimant: str, *, symbols: Iterable[str]) -> str | None:
        """The sentence refusing ``claimant``'s declaration, or ``None`` if clear.

        A message rather than a verdict because neither caller *decides*
        anything on a conflict — they differ only in which exception carries it,
        and a shape that returned the collisions would leave the wording to be
        written again at each site, which is the drift this type exists to stop.

        Every taken symbol is named with **its own** incumbent: the remedy is to
        move one of the two strategies onto its own account, which a reader
        cannot choose without both names, and a run that overlapped on three
        symbols should learn all three now rather than one per restart. Symbols
        the claimant declares that nobody owns are absent — they are not the
        problem, and naming them would bury the ones that are.
        """
        taken = sorted(
            (symbol, self.owners[symbol]) for symbol in set(symbols) if symbol in self.owners
        )
        if not taken:
            return None
        collisions = ", ".join(f"{symbol} (owned by {owner})" for symbol, owner in taken)
        return (
            f"{claimant} declares symbols another strategy already owns: "
            f"{collisions} — same-symbol isolation needs a separate account"
        )

    def claim(self, claimant: str, *, symbols: Iterable[str]) -> Self:
        """``self`` with every symbol in ``symbols`` recorded to ``claimant``.

        Records **unconditionally**, so a caller that skipped ``refusal`` would
        overwrite an incumbent. The two verbs stay separate rather than
        collapsing into one that raises precisely because the exception is the
        caller's to choose; ordering them is the price of that, and both call
        sites pay it on adjacent lines.
        """
        return type(self)(owners={**self.owners, **dict.fromkeys(symbols, claimant)})
