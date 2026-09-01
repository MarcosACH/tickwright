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
from typing import Any

from tickwright.domain import FundingAccrual

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
    """
    return tuple(
        FundingAccrual(
            ts_event=int(record["time"]) * _NS_PER_MS,
            ts_init=ts_init_ns,
            account_id=account_id,
            symbol=record["coin"],
            boundary_ts_ns=int(record["time"]) * _NS_PER_MS,
            amount=figure(record["usdc"]),
        )
        for record in fundings
    )
