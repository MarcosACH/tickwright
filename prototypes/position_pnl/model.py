"""PROTOTYPE — position & PnL state model (wayfinder ticket #119, map #107).

Question this answers
---------------------
Does the two-level position/PnL state model fixed by ADR-0034 hold up when you
push it through the hard cases by hand? Specifically:

  * The **account-level net aggregate** (the sole reconciliation anchor): one
    netted position per (account, symbol), average-cost, realized-on-reduce,
    unrealized-via-mark, with a clean long<->short flip across zero (entry
    resets on the residual). This must reproduce what a one-way (``NET``) venue
    would report as ``szi`` / ``entryPx`` / realized on the same fill sequence.

  * The **per-strategy overlay** (the strategy read-API partition): each
    strategy's own position from its own fills, *never reconciled* against the
    venue, bridged to the anchor by the single invariant
    ``Sigma(per-strategy signed size per symbol) = account net size = szi``.

The load-bearing thing to *see*, not just assert: on a ``NET`` venue under the
disjointness rule (a symbol is owned by exactly one strategy per account),
per-strategy collapses to per-(account, symbol) **exactly** — same size, same
realized, same unrealized, no realized<->unrealized reclassification. And when
two strategies *share* a symbol (what a future ``HEDGE`` venue would allow, and
what disjointness deliberately forbids on ``NET``), the size invariant still
holds but the realized/unrealized *split* diverges — which is exactly why
per-strategy PnL is never reconciled.

Scope (per the ticket): position aggregate only. Fees and funding are separate
cash-ledger tickets (#116 / #117) and are **out of scope here**. All money is
``Decimal`` (ADR-0029).

This module is the part worth keeping: a pure ``(position, fill) -> position``
reducer plus pure valuation/aggregation helpers, no I/O. The TUI in ``tui.py``
is a throwaway shell over it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from tickwright.domain.enums import Side

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Fill:
    """A raw position-changing fact, tagged with who owns it and where.

    Mirrors the fields of the real ``FillReport`` that matter to *position*
    (``strategy_id``, ``symbol``, ``Side``, ``quantity``, ``price``); the
    account is implicit (single account in this prototype). ``quantity`` is
    unsigned — ``Side`` carries the direction, exactly as the domain models it.
    """

    strategy_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal

    @property
    def signed_qty(self) -> Decimal:
        """Direction-signed size: +qty for BUY, -qty for SELL."""
        return self.quantity if self.side is Side.BUY else -self.quantity


@dataclass(frozen=True, slots=True)
class Position:
    """Average-cost netted position: the pure economic state a fill updates.

    ``net_qty`` is signed (>0 long, <0 short, 0 flat). ``avg_entry`` is
    meaningful only while ``net_qty != 0`` and is reset to 0 on going flat.
    ``realized`` is the cumulative average-cost realized PnL from reduces /
    closes / flips (position only — no fees, no funding).
    """

    symbol: str
    net_qty: Decimal = ZERO
    avg_entry: Decimal = ZERO
    realized: Decimal = ZERO

    @property
    def is_flat(self) -> bool:
        return self.net_qty == ZERO

    def unrealized(self, mark: Decimal) -> Decimal:
        """Unrealized PnL against a mark. Signed ``net_qty`` handles both sides:

        long (+qty): profit when mark > entry; short (-qty): profit when mark <
        entry. Zero when flat (``net_qty == 0``).
        """
        return (mark - self.avg_entry) * self.net_qty


def apply_fill(pos: Position, fill: Fill) -> Position:
    """The reducer: fold one fill into a position with average-cost accounting.

    Four regimes, gated on the relationship between the current signed
    ``net_qty`` and the fill's signed size:

      * **open from flat** — entry := fill price, no realized.
      * **add (same side)** — weighted-average the entry, no realized.
      * **reduce / full close (opposite side, |fill| <= |net|)** — realize on the
        closed portion at the old entry; entry unchanged on a partial, reset on
        a full close.
      * **flip through zero (opposite side, |fill| > |net|)** — realize the
        *entire* old position, then the residual opens a fresh position on the
        other side at the fill price.
    """
    assert fill.symbol == pos.symbol, "fill routed to the wrong symbol"
    signed = fill.signed_qty

    if pos.net_qty == ZERO:  # open from flat
        return replace(pos, net_qty=signed, avg_entry=fill.price)

    same_side = (pos.net_qty > ZERO) == (signed > ZERO)
    if same_side:  # add to position -> weighted-average entry
        new_net = pos.net_qty + signed
        new_entry = (pos.avg_entry * abs(pos.net_qty) + fill.price * abs(signed)) / abs(new_net)
        return replace(pos, net_qty=new_net, avg_entry=new_entry)

    # opposite side: reduce, full close, or flip through zero
    closed = min(abs(pos.net_qty), abs(signed))
    pos_sign = Decimal(1) if pos.net_qty > ZERO else Decimal(-1)
    # long closed by a sell: (exit - entry) * qty ; short closed by a buy:
    # (entry - exit) * qty. Unified via the sign of the position being closed.
    realized_delta = (fill.price - pos.avg_entry) * closed * pos_sign
    new_net = pos.net_qty + signed
    new_realized = pos.realized + realized_delta

    if new_net == ZERO:  # full close -> flat, entry reset
        return replace(pos, net_qty=ZERO, avg_entry=ZERO, realized=new_realized)
    if (new_net > ZERO) == (pos.net_qty > ZERO):  # partial reduce -> entry held
        return replace(pos, net_qty=new_net, realized=new_realized)
    # flipped through zero -> residual opens fresh at the fill price
    return replace(pos, net_qty=new_net, avg_entry=fill.price, realized=new_realized)


# --- Aggregation: the two levels, folded from the same fill log ---------------


def account_positions(fills: list[Fill]) -> dict[str, Position]:
    """The **account-level net aggregate**, one netted position per symbol.

    All fills for a symbol are applied in order *regardless of strategy* — this
    is precisely a ``NET`` venue's netting, so each result is what the venue
    reports as ``szi`` / ``entryPx`` / realized. This is the reconciliation
    anchor.
    """
    out: dict[str, Position] = {}
    for f in fills:
        pos = out.get(f.symbol, Position(symbol=f.symbol))
        out[f.symbol] = apply_fill(pos, f)
    return out


def strategy_positions(fills: list[Fill]) -> dict[tuple[str, str], Position]:
    """The **per-strategy overlay**, one position per (strategy, symbol).

    A *different partition* of the same fills: each strategy nets only its own
    fills. Never reconciled against the venue (the venue has no per-strategy
    truth); bridged to the anchor by the size invariant below.
    """
    out: dict[tuple[str, str], Position] = {}
    for f in fills:
        key = (f.strategy_id, f.symbol)
        pos = out.get(key, Position(symbol=f.symbol))
        out[key] = apply_fill(pos, f)
    return out


def size_invariant(fills: list[Fill]) -> dict[str, tuple[Decimal, Decimal, bool]]:
    """Per symbol: (account net size, Sigma per-strategy size, do they match?).

    The bridge invariant from ADR-0034: position *size* is linear and sums, so
    this must hold on **every** fill sequence, disjoint or shared.
    """
    acct = account_positions(fills)
    strat = strategy_positions(fills)
    out: dict[str, tuple[Decimal, Decimal, bool]] = {}
    for symbol, pos in acct.items():
        strat_sum = sum((p.net_qty for (_, sym), p in strat.items() if sym == symbol), start=ZERO)
        out[symbol] = (pos.net_qty, strat_sum, pos.net_qty == strat_sum)
    return out


def pnl_reclassification(fills: list[Fill], marks: dict[str, Decimal]) -> dict[str, dict]:
    """Per symbol: does the realized/unrealized split survive the partition?

    Compares the account aggregate's (realized, unrealized) against the *sum* of
    the per-strategy overlays'. Under disjointness (one strategy owns the
    symbol) these are identical — the partition collapses to exact. When
    strategies share a symbol, realized/unrealized are **not** linear under
    netting, so the split diverges even though size still sums — the
    reclassification ADR-0034 says must never be reconciled per-strategy.
    """
    acct = account_positions(fills)
    strat = strategy_positions(fills)
    out: dict[str, dict] = {}
    for symbol, pos in acct.items():
        mark = marks.get(symbol, pos.avg_entry)
        owners = [(k, p) for k, p in strat.items() if k[1] == symbol]
        strat_realized = sum((p.realized for _, p in owners), start=ZERO)
        strat_unreal = sum((p.unrealized(mark) for _, p in owners), start=ZERO)
        out[symbol] = {
            "shared": len(owners) > 1,
            "acct_realized": pos.realized,
            "strat_realized": strat_realized,
            "realized_match": pos.realized == strat_realized,
            "acct_unreal": pos.unrealized(mark),
            "strat_unreal": strat_unreal,
            "unreal_match": pos.unrealized(mark) == strat_unreal,
        }
    return out
