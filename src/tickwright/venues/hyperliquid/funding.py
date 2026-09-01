"""Live funding ingest: the venue's own payments, transformed by nothing
(ADR-0037).

Paper **computes** funding off the `Clock` and this **ingests** it — the two
`Exchange` adapters are the seam's two implementations, which is why there is no
`FundingModel` object to swap. What lands on the bus is the same keyed
`FundingAccrual` either way, so the projection's apply path carries no `if
live:`.

`amount` is `usdc` verbatim, sign included. ADR-0037 §Sign fixes that the accrual
mirrors its venue source field rather than a house convention, so a later
reconcile is a direct field compare instead of an argument about which side
negated. Nothing here may normalise, `abs()`, or flip it — a transformation on
this path is a flip bug waiting for the first short position.
"""

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from tickwright.domain import FundingAccrual, VenueFactUnsupported

from .reading import figure

_NS_PER_MS = 1_000_000


def accruals(
    fundings: Iterable[Mapping[str, Any]],
    *,
    account_id: str,
    ts_init_ns: int,
) -> tuple[FundingAccrual, ...]:
    """The venue's reported payments as keyed accruals, in venue-time order.

    `boundary_ts_ns` is the venue's own `time` — when the payment for that hour
    landed on the account — which is also `ts_event`, because the venue's
    settlement instant is the instant the fact occurred (ADR-0005). Paper's
    counterpart is its epoch-aligned boundary; the two never have to agree,
    since an account is either paper or live and the key only dedupes within one
    account's stream.

    **Sorted by venue time ascending, and that is this function's real content.**
    The venue documents no delivery order within a batch, while the ledger's
    watermark is monotonic: fed `t3, t1, t2` it applies `t3`, advances the mark
    past it, and then drops `t1` and `t2` as already-applied — real payments the
    account never sees again, because the snapshot is delivered once. Across
    deliveries the venue's documented behavior supplies that monotonicity
    (snapshot, then payments on the hour); within one batch nothing does but
    this line.

    The sort is **stable**, so records sharing a boundary keep the venue's
    delivery order. That is the common case rather than a corner — every symbol
    settles on the same hour — and there is nothing better to order them by:
    they are distinct payments keyed apart by `symbol`, so the gate sees one
    boundary either way and a tie-break would be inventing a sequence the venue
    did not report.
    """
    ordered = sorted(fundings, key=lambda record: int(record["time"]))
    return tuple(
        FundingAccrual(
            ts_event=int(record["time"]) * _NS_PER_MS,
            ts_init=ts_init_ns,
            account_id=account_id,
            symbol=record["coin"],
            boundary_ts_ns=int(record["time"]) * _NS_PER_MS,
            amount=_settled_in_usdc(record),
        )
        for record in ordered
    )


def _settled_in_usdc(record: Mapping[str, Any]) -> Decimal:
    """One payment's amount, refusing a payment denominated in anything else.

    A funding record carries no currency discriminator the way a fill carries
    `feeToken`: the amount field is *named* `usdc`, so the denomination is the
    key itself and a payment settled in another token arrives as a record with
    no `usdc` at all. This engine's money is a bare `Decimal` with USDC left
    implicit (ADR-0029), so such a payment has nowhere to go — accruing it would
    add a figure of one currency to a line of another with nothing in the ledger
    recording which token it came from.

    The failure worth guarding is the quiet one. Defaulting the read to `0` would
    accrue a zero, and a zero is indistinguishable from a boundary that genuinely
    owed nothing, so the account would under-count real money with no trace that
    it had. The refusal names the keys the record *did* carry, which is the one
    thing that tells an operator what the venue actually sent.

    `VenueFactUnsupported`, on the same reasoning as the fill fee's guard of the
    same assumption: a settled funding row never changes, so this is known
    permanent at the first read and escalates out of the seam rather than
    entering `UNREADABLE`, whose whole purpose is to find out — by waiting —
    whether a condition is durable (ADR-0048).
    """
    if "usdc" not in record:
        raise VenueFactUnsupported(
            f"funding payment on {record.get('coin')!r} reports no 'usdc' amount "
            f"(keys: {sorted(record)}): this engine's money is a bare Decimal with "
            "USDC implicit (ADR-0029), so a payment settled in another token has no "
            "home in the ledger. Retrying cannot change a settled funding row."
        )
    return figure(record["usdc"])
