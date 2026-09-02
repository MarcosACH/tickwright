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
from .instrument import InstrumentSpec
from .leverage import LeverageSpec
from .position import Position, PositionView

_ZERO = Decimal("0")


def position_view(
    position: Position,
    *,
    account_net: Decimal,
    mark: Decimal | None,
    mark_ts: int | None,
    leverage: LeverageSpec,
    spec: InstrumentSpec | None,
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

    ``leverage`` is the resolved ``(mode, leverage)`` pair for the symbol — one
    value rather than two, because the venue action that sets it is one signed
    action (ADR-0044 §2). It is the margin model's only operator-authored input.

    ``spec`` carries the venue-authored side of the margin model. It is
    nullable and undefaulted for opposite reasons: ``None`` is reachable —
    the reserved unattributed partition can hold a symbol outside our
    configured universe, and no spec exists for it — while a *default* would
    hand that same silence to a caller who never considered the case.
    """
    notional = _notional(account_net, mark)
    unrealized_pnl = _unrealized_pnl(position, mark)
    return PositionView(
        symbol=position.symbol,
        size=position.signed_size,
        entry_price=position.entry_price,
        realized_pnl=position.realized_pnl,
        fees=position.fees,
        funding=position.funding,
        unrealized_pnl=unrealized_pnl,
        notional=notional,
        margin_used=_margin_used(
            notional,
            leverage=leverage,
            isolated_collateral=position.isolated_collateral,
            unrealized_pnl=unrealized_pnl,
        ),
        maintenance_margin=_maintenance_margin(notional, spec=spec),
        mark_ts=mark_ts,
    )


def _maintenance_margin(notional: Decimal | None, *, spec: InstrumentSpec | None) -> Decimal | None:
    """``notional × margin_maint`` at the flat tier-0 rate (ADR-0040 §4).

    The rate is read off the spec rather than derived from ``max_leverage``, so
    this stays venue-agnostic instead of encoding Hyperliquid's "half the
    initial margin at max leverage" convention (the choice ADR-0036 made for
    the fee rates).

    Deliberately **flat**: above the venue's first margin-tier band the true
    rate steps up and carries a continuity deduction, and reproducing that is
    ADR-0040 §4's named extension point, not this function's job. Until it is
    taken, a tier-crossing position under-reports here — measured at 9.3 % on
    #152's crossing — and the gap is left to trip ADR-0040 §6's divergence
    alert. Absorbing it silently is the one outcome the deferral rules out.

    A symbol with no spec has no rate, so the answer is unknown rather than
    frictionless: a fabricated ``0`` would report a position as needing no
    maintenance at all, which is the same unknown-as-worthless mistake the mark
    rule refuses.
    """
    if notional is None or spec is None:
        return None
    return notional * spec.margin_maint


def _margin_used(
    notional: Decimal | None,
    *,
    leverage: LeverageSpec,
    isolated_collateral: Decimal,
    unrealized_pnl: Decimal | None,
) -> Decimal | None:
    """The collateral posted behind the position, by the mode's own rule.

    The two modes are **different rules, not one rule parameterised** (ADR-0040
    §3, as corrected by #142):

    - **Cross** draws ``notional / leverage`` from the shared account pool. The
      division is the whole amount: there is no per-venue haircut on the initial
      fraction, so ADR-0040 §4 declines a ``margin_init`` field rather than
      carry a constant ``1.0``.
    - **Isolated** reports its locked bucket marked to market —
      ``isolated_collateral + unrealized_pnl``. The configured leverage is *not*
      a term here: the bucket is sized at open and a later leverage change never
      re-margins a held position, so reading the setting back would report a
      number the venue has stopped holding.

    Nullability is inherited from whichever terms the mode actually uses, which
    is why the branch takes both and not a pre-selected one: cross inherits from
    ``notional`` and isolated from ``unrealized_pnl``, and the two come apart
    under offsetting partitions — a flat account-net gives cross its real ``0``
    at a mark the still-open own slice needs and does not have.
    """
    if leverage.mode == "isolated":
        if unrealized_pnl is None:
            return None
        return isolated_collateral + unrealized_pnl
    if notional is None:
        return None
    return notional / leverage.leverage


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


def account_unrealized_pnl(
    positions: Iterable[Position], marks: Mapping[str, Decimal]
) -> dict[str, Decimal | None]:
    """Per-symbol uPnL at the **account** grain — the Σ over every partition.

    ``PositionView.unrealized_pnl`` is one partition's own slice, and the venue
    holds one position per symbol, so a cross-check against venue truth needs
    the symbol's total or it is comparing a fraction against a whole (ADR-0035,
    ADR-0041 §4). Every partition counts, the reserved unattributed one
    included: the venue is holding that exposure too.

    The per-term nullability rule is **inherited** from ``_unrealized_pnl``, as
    ``_equity`` inherits it — one unknown term makes the symbol's total unknown,
    while a flat partition contributes its real zero and blocks nothing. Spelled
    afresh here, this grain and the account's would agree only until the first
    exemption that applies to one of them.
    """
    totals: dict[str, Decimal | None] = {}
    for position in positions:
        term = _unrealized_pnl(position, marks.get(position.symbol))
        running = totals.get(position.symbol, _ZERO)
        totals[position.symbol] = None if term is None or running is None else running + term
    return totals


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


__all__ = ["account_view", "position_view"]
