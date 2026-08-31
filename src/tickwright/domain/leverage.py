"""The per-symbol leverage & margin-mode input (ADR-0040 §5, ADR-0044 §2).

The one *operator-authored* value in a package otherwise made of outputs, which
is why it gets its own module: filing it under ``instrument.py`` (identical
venue metadata across paths) or ``position.py`` (an output) invites exactly the
confusion those two ADRs spent two sections preventing.

**Venue-agnostic and per-symbol.** Its consumer, the ``PortfolioProjection``
margin model, is venue-agnostic and needs it on both paths, so it can never
live in a venue's config block — a ``TICKWRIGHT_PAPER__*`` value must never
govern a live run (ADR-0042 §1).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

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


def validate_leverage_bounds(
    leverage: Mapping[str, LeverageSpec], specs: Mapping[str, InstrumentSpec]
) -> None:
    """Refuse a resolved map any instrument cannot carry (ADR-0044 §9).

    ``1 ≤ leverage ≤ spec.max_leverage``, inclusive at both ends, for every
    symbol in the **resolved** map — which is the strategy-traded set, so a
    symbol with no ``InstrumentSpec`` is a traded symbol nothing bounds and is
    refused on the same pass rather than skipped.

    One shared implementation because ADR-0044 §9 requires the two paths to
    refuse *identically*: leaving each adapter its own check is how paper comes
    to accept a leverage live rejects, and the divergence then surfaces only on
    promotion. The same argument that put ``below_min_notional`` here.

    Pure and raising, not returning a verdict: a caller has nothing to decide.
    Every offence is collected before raising, so one start reports them all.
    """
    offences: list[str] = []
    for symbol in sorted(leverage):
        spec = specs.get(symbol)
        if spec is None:
            offences.append(f"{symbol}: no instrument spec to bound its leverage")
            continue
        configured = leverage[symbol].leverage
        if not 1 <= configured <= spec.max_leverage:
            offences.append(f"{symbol}: leverage {configured} outside 1..{spec.max_leverage}")
    if offences:
        raise LeverageOutOfBounds(
            "configured leverage is not carryable by the instrument — " + "; ".join(offences)
        )
