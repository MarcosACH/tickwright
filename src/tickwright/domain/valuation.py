"""Tier-2 view assembly: pure functions over an explicit input set (ADR-0041).

Every quantity here is **recomputed on read** from the current position and the
current mark, and **nothing here is ever stored** (ADR-0034's two-tier rule,
ADR-0043 §3). That is the whole reason it is a module of functions rather than
methods on ``Position``/``Account``: the position-grain numbers are computed off
the symbol's *account-net* size — an aggregation over every partition, not a
field of any one of them (ADR-0035) — and the account's read every position at
once, so an aggregate method's argument list would end up being the rest of the
model. The aggregates keep only the queries their own state plus a mark answers.

Assembling a whole view in one call is what makes it **internally coherent by
construction**: every field comes from one ``(position, mark)`` read, so two
fields of one view can never straddle a fill (ADR-0041 §1).
"""

from collections.abc import Iterable, Mapping
from decimal import Decimal

from .account import Account, AccountView
from .position import Position, PositionView

_ZERO = Decimal("0")


def position_view(
    position: Position,
    *,
    account_net: Decimal,
    mark: Decimal | None,
    mark_ts: int | None,
) -> PositionView:
    """One partition's frozen snapshot, Tier-1 and Tier-2 in one read.

    ``account_net`` is the symbol's size summed over **every** partition, the
    grain the position-grain half is computed at; ``mark`` is the latest mark the
    projection holds for this symbol and ``mark_ts`` its observation instant,
    both ``None`` when it holds none. Every input is explicit and required —
    there is no defaulting, because each default would be a wrong answer a caller
    could take silently: a missing ``account_net`` reads as a flat book, and a
    missing ``mark`` as a valuation that was merely never made.

    The mark's **value** is deliberately not a field of the result: a strategy
    judges freshness for itself (it holds a ``Clock``, and the read path is
    clock-free by ADR-0039), but the mark stays an accounting input rather than
    becoming a strategy signal.

    Nullability is **per-term, not per-field** (ADR-0041 §6): a Tier-2 field
    reads ``None`` only when the mark is absent *and its own terms need it*, so
    a flat slice still reads a real ``0``.
    """
    return PositionView(
        symbol=position.symbol,
        size=position.signed_size,
        entry_price=position.entry_price,
        realized_pnl=position.realized_pnl,
        fees=position.fees,
        funding=position.funding,
        unrealized_pnl=_unrealized_pnl(position, mark),
        notional=_notional(account_net, mark),
        mark_ts=mark_ts,
    )


def account_unrealized_pnl(
    positions: Iterable[Position], marks: Mapping[str, Decimal]
) -> dict[str, Decimal | None]:
    """Per-symbol unrealized PnL summed over **every** partition — the Tier-2
    peer of ``account_net_size``, and the account-grain reconcile's left-hand
    side against the venue's own per-position figure.

    Computed at the account grain because that is the grain the venue reports
    at: it holds one position per symbol and knows nothing of partitions, so a
    per-strategy comparison would have no right-hand side (ADR-0034/0041 §4).

    ``None`` for a symbol whose Σ needs a mark it does not have, per-**term** as
    everywhere else here: a partition traded flat contributes its real zero and
    never blocks the sum, so a symbol reads ``None`` only when something actually
    held is unpriced. A caller comparing against a venue must treat that as *not
    compared* rather than as zero — the divergence would be explained by the
    missing mark, not by the ledger.
    """
    totals: dict[str, Decimal | None] = {}
    for position in positions:
        # ``get``'s default covers the first partition of a symbol; a stored
        # ``None`` from an earlier one is returned as-is and poisons the rest of
        # the fold, which is the per-term rule at the Σ level.
        running = totals.get(position.symbol, _ZERO)
        own = _unrealized_pnl(position, marks.get(position.symbol))
        totals[position.symbol] = None if running is None or own is None else running + own
    return totals


def _notional(account_net: Decimal, mark: Decimal | None) -> Decimal | None:
    """The symbol's exposure at position grain — ``|account net| × mark``.

    A flat account-net is the per-term exemption, and it is a different fact
    from a flat *slice*: two strategies holding offsetting legs read a real
    ``0`` notional here while each still carries its own open exposure.
    """
    if account_net == _ZERO:
        return _ZERO
    if mark is None:
        return None
    return abs(account_net) * mark


def _unrealized_pnl(position: Position, mark: Decimal | None) -> Decimal | None:
    """The own-slice mark-to-market, or ``None`` when it genuinely needs a mark.

    A flat slice is the per-term exemption: ``0 × (mark − entry)`` is zero at
    every mark, so reporting ``None`` there would withhold an answer nothing was
    missing for.
    """
    if position.is_flat:
        return _ZERO
    if mark is None:
        return None
    return position.unrealized_pnl(mark)


def account_view(
    account: Account,
    *,
    positions: Iterable[Position],
    marks: Mapping[str, Decimal],
) -> AccountView:
    """The account-wide pool's frozen snapshot — one collateral bucket.

    Never scoped to a strategy: collateral is one pool per process (ADR-0038),
    and reporting a slice of it would be a fiction (ADR-0041 §2). So
    ``positions`` is **every** partition, the reserved unattributed one
    included — anything the account is holding backs the same bucket, whether or
    not this engine placed it.

    ``marks`` carries only the symbols a mark has been seen for; a symbol absent
    from it is what makes the Σ unknown.
    """
    return AccountView(cash=account.cash, equity=_equity(account, positions, marks))


def _equity(
    account: Account, positions: Iterable[Position], marks: Mapping[str, Decimal]
) -> Decimal | None:
    """``cash + Σ uPnL``, or ``None`` the moment one term cannot be computed.

    The condition is per-**term**, so a flat partition contributes its real zero
    and never blocks the sum: its uPnL is zero at every mark, mark or no mark.
    A held partition whose symbol has no mark does block it, and that is the
    honest answer — the alternative is a partial sum reported as the whole.

    The Σ **inherits** that rule from ``_unrealized_pnl`` rather than restating
    it: it is one rule at two grains, and the account's half is the one with no
    unit test of its own for each future exemption. Spelled twice, the two would
    agree until the first term that is exempt at one grain and not the other,
    and the disagreement would surface as an equity that is silently ``None``.
    """
    total = account.cash
    for position in positions:
        term = _unrealized_pnl(position, marks.get(position.symbol))
        if term is None:
            return None
        total += term
    return total


__all__ = ["account_unrealized_pnl", "account_view", "position_view"]
