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
_ONE = Decimal("1")


def position_view(
    position: Position,
    *,
    account_net: Decimal,
    account_unrealized_pnl: Decimal | None,
    account_equity: Decimal | None,
    mark: Decimal | None,
    mark_ts: int | None,
    leverage: LeverageSpec,
    spec: InstrumentSpec | None,
) -> PositionView:
    """One partition's frozen snapshot, Tier-1 and Tier-2 in one read.

    ``account_net`` is the symbol's size summed over **every** partition, the
    grain the position-grain half is computed at, and ``account_unrealized_pnl``
    is that same Σ one tier up — the whole venue position's mark-to-market,
    which isolated ``margin_used`` needs because the venue holds one collateral
    bucket per position and not one per strategy (ADR-0041 §4). It is a separate
    input rather than the ``unrealized_pnl`` this function computes, because
    those two are the same number only until foreign flow appears in the symbol
    (§5), and the view is required to carry both grains at once.

    ``account_equity`` is the whole pool's ``cash + Σ uPnL`` — cross
    ``effective_leverage``'s denominator, and the one input here that no
    ``(position, mark)`` pair can supply, since it sums over *every* position the
    account holds. It is passed in rather than recomputed so that a view and the
    ``AccountView`` beside it are read off one snapshot instead of two, and it is
    ``Decimal | None`` on the same per-term rule as everything else: an unmarked
    position anywhere in the book makes it unknown.

    ``mark`` is the latest mark the
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
    backing = _backing_equity(
        leverage=leverage,
        isolated_collateral=position.isolated_collateral,
        account_unrealized_pnl=account_unrealized_pnl,
        account_equity=account_equity,
    )
    maintenance_margin = _maintenance_margin(notional, spec=spec)
    return PositionView(
        symbol=position.symbol,
        size=position.signed_size,
        entry_price=position.entry_price,
        realized_pnl=position.realized_pnl,
        fees=position.fees,
        funding=position.funding,
        unrealized_pnl=unrealized_pnl,
        notional=notional,
        leverage=leverage.leverage,
        margin_mode=leverage.mode,
        margin_used=_margin_used(notional, leverage=leverage, backing=backing),
        maintenance_margin=maintenance_margin,
        liquidation_price=_liquidation_price(
            account_net=account_net,
            mark=mark,
            backing=backing,
            maintenance_margin=maintenance_margin,
            spec=spec,
        ),
        effective_leverage=_effective_leverage(notional, backing=backing),
        mark_ts=mark_ts,
    )


def _backing_equity(
    *,
    leverage: LeverageSpec,
    isolated_collateral: Decimal,
    account_unrealized_pnl: Decimal | None,
    account_equity: Decimal | None,
) -> Decimal | None:
    """What actually stands behind the position, by the mode's own rule.

    One quantity feeding three: it is isolated ``margin_used`` outright, it is
    the denominator ``effective_leverage`` divides by, and it is the term
    ``liquidation_price`` subtracts maintenance from. Those three are all the
    same question — *what is there to lose before the position is closed out* —
    and the whole mode split lives here rather than three times over.

    Isolated is the position's own locked bucket marked to market,
    ``isolated_collateral + unrealized_pnl`` at account-net grain (ADR-0041
    §4.1): the venue holds one bucket per position, and its balance is what a
    move eats into. Cross has no bucket — it draws on the shared pool — so its
    backing is whole-account ``equity`` (ADR-0038: one pool per process).

    The uPnL term is the symbol's account-net Σ and never one strategy's slice,
    for the same reason: the bucket backs the whole venue position.
    """
    if leverage.mode == "isolated":
        if account_unrealized_pnl is None:
            return None
        return isolated_collateral + account_unrealized_pnl
    return account_equity


def _liquidation_price(
    *,
    account_net: Decimal,
    mark: Decimal | None,
    backing: Decimal | None,
    maintenance_margin: Decimal | None,
    spec: InstrumentSpec | None,
) -> Decimal | None:
    """``mark − side · margin_available / size / (1 − l · side)`` (ADR-0040 §3).

    The paper branch only: on live this is read through from the venue, because
    re-deriving it needs the maintenance-margin tier fixed point — the tier
    depends on the position's value *at the price being solved for* — and the
    venue publishes the answer as one field. What is computed here is the
    flat-tier-0 case, exact below the first band (§4).

    ``margin_available`` is ``backing − maintenance_margin``: what the position
    can lose before it owes more than it has. ``size`` is ``|account net|`` and
    ``price`` is the **mark**, both settled against the venue by #142. The
    ``side`` factor is what puts a short's price *above* the mark, since a short
    is liquidated by a rally.

    The result is **invariant to the mark** — the mark cancels between the
    subtraction and the uPnL inside ``backing`` — which is the property that
    makes it a level rather than a reading, and #142 confirmed it against the
    venue across two marks.

    ``None`` on a flat account-net: the division is by ``size``, and a flat
    position has no ``side`` to signed-divide with either, so there is no price
    rather than an infinite one (ADR-0041 §3). That is also what live reports
    for a position the venue no longer holds, so the two paths stay identical
    instead of paper inventing a value.
    """
    if account_net == _ZERO:
        return None
    if mark is None or backing is None or maintenance_margin is None or spec is None:
        return None
    side = _ONE if account_net > _ZERO else -_ONE
    margin_available = backing - maintenance_margin
    return mark - side * margin_available / abs(account_net) / (_ONE - spec.margin_maint * side)


def _effective_leverage(notional: Decimal | None, *, backing: Decimal | None) -> Decimal | None:
    """``notional`` over the position's backing equity (ADR-0041 §4.1).

    The denominator is the decision, and it is ``_backing_equity``'s: isolated
    divides by the position's own bucket marked to market, cross by
    whole-account equity. ADR-0040 §2 originally fixed account equity for both;
    #142 settled it against the venue by moving one input nothing else moved, an
    ``updateIsolatedMargin`` top-up of +20 USDC that drove the ratio from
    ``5.0119`` to ``2.8260`` behind an unchanged position. Under an
    account-equity denominator the ratio would not have moved at all.

    Nullability is inherited term by term like the rest, plus one case that is
    not about the mark at all: a **non-positive** denominator has no ratio to
    report, and it is the only Tier-2 ``None`` a fresh mark cannot cure
    (ADR-0041 §6). Zero is reached by a closed isolated position, whose
    collateral is released, leaving ``0 + 0`` behind a flat notional (§3) —
    ``0/0`` is not ``0``; nothing is levered there. Negative is reached by
    ordinary trading, because nothing here rejects an order or liquidates a
    position (ADR-0040 §7), so equity and an isolated bucket alike keep running
    past zero. Returning the division there would report a *negative* leverage,
    which reads as de-levered and is the opposite of what has happened.

    The rest of the view stays real on the same inputs: a wiped account still has
    a notional, an unrealized PnL and margins. This field is ``None`` because the
    ratio is undefined, never because a term was missing.
    """
    if notional is None or backing is None or backing <= _ZERO:
        return None
    return notional / backing


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
    rule refuses. But the rate is a **term**, and ADR-0041 §6's rule is
    per-term: a zero notional is zero maintenance at every rate, so the missing
    spec stops mattering exactly where the missing mark does. That case is not
    hypothetical — the unattributed partition is where both go missing at once,
    since a symbol outside our configured universe is also one no ``MarkTick``
    subscription covers.
    """
    if notional == _ZERO:
        return _ZERO
    if notional is None or spec is None:
        return None
    return notional * spec.margin_maint


def _margin_used(
    notional: Decimal | None, *, leverage: LeverageSpec, backing: Decimal | None
) -> Decimal | None:
    """The collateral posted behind the position, by the mode's own rule.

    The two modes are **different rules, not one rule parameterised** (ADR-0040
    §3, as corrected by #142):

    - **Cross** draws ``notional / leverage`` from the shared account pool. The
      division is the whole amount: there is no per-venue haircut on the initial
      fraction, so ADR-0040 §4 declines a ``margin_init`` field rather than
      carry a constant ``1.0``.
    - **Isolated** reports its locked bucket marked to market — which is exactly
      ``_backing_equity``, so this returns it unchanged. The configured leverage
      is *not* a term: the bucket is sized at open and a later leverage change
      never re-margins a held position, so reading the setting back would report
      a number the venue has stopped holding.

    Both terms are **position-grain** (ADR-0041 §4.1): the uPnL inside the
    isolated branch is the symbol's account-net total, never one strategy's
    slice, because the bucket the venue holds backs the whole position.

    Nullability is inherited from whichever terms the mode actually uses, which
    is why this takes both and not a pre-selected one: cross inherits from
    ``notional`` and isolated from ``backing``, and the two come apart under
    offsetting partitions — a flat account-net gives cross its real ``0`` at a
    mark the still-open legs behind it need and do not have.
    """
    if leverage.mode == "isolated":
        return backing
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
