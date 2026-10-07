"""PROTOTYPE, throwaway. A pure model of the flatten saga for one symbol.

Question (issue #413, map #408): how does flatten reach zero? A market order can fill
partly. The position can change while flatten runs. The engine can crash halfway. This
model settles when flatten retries, when it stops, and what it resumes after a restart.

Rules it encodes (ADR-0052, ADR-0053, ADR-0054, and the probes in #416):
- Flatten trips the kill switch first. The kill switch is in the store, so a crash keeps it.
- Each attempt is one reduce-only IOC order, sized to the venue position, owned by
  `__operator__:BTC:<seq>`. An IOC ends at once: filled, partly filled, or no fill (P6).
- The venue shrinks a reduce-only order to the position (P1). If there is nothing to
  reduce, the venue rejects it.
- An attempt is placed only when the books match the venue.
- Fills split pro rata over the partitions stored with the saga, on the cumulative fill.
- After every attempt, reconcile, then decide again. Retry while the venue holds a
  position. Stop after MAX_DRY_ATTEMPTS attempts in a row that filled nothing.
- On boot, open sagas resume first: the venue's fills are booked through the same split.
  Only then does flatten reconcile and place the next seq.
- Venue flat but books not: the leftover rule moves strategy sizes into `__unattributed__`.

Pure: no I/O, no print. The TUI in run.py drives it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from tickwright.domain.enums import OrderState

OPERATOR = "__operator__"
UNATTRIBUTED = "__unattributed__"
SYMBOL = "BTC"
LOT = Decimal("0.001")
MAX_DRY_ATTEMPTS = 3
ZERO = Decimal(0)

TERMINAL = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.FAILED}
)


class Phase(StrEnum):
    START = "start"  # next step trips the kill switch and runs cancel all
    RECONCILE = "reconcile"  # next step reads the venue and heals the books
    DECIDE = "decide"  # next step places, finishes, or gives up
    SEND = "send"  # saga is in the store, the order is not sent yet
    WAIT = "wait"  # order is at the venue, waiting for fills and the IOC end
    DONE = "done"
    GAVE_UP = "gave_up"


def sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


@dataclass(frozen=True)
class Saga:
    """One flatten order as the store holds it."""

    seq: int
    size: Decimal  # signed change the order makes. -0.7 is a sell of 0.7
    snapshot: dict[str, Decimal]  # partition sizes when the order was placed
    filled: Decimal  # cumulative fill the books have split, absolute
    moved: dict[str, Decimal]  # cumulative move booked per partition
    state: OrderState

    @property
    def order_id(self) -> str:
        return f"{OPERATOR}:{SYMBOL}:{self.seq}"


@dataclass(frozen=True)
class VenueOrder:
    size: Decimal  # absolute size asked for
    side: int  # +1 buy, -1 sell
    filled: Decimal
    open: bool
    status: str


@dataclass(frozen=True)
class Venue:
    position: Decimal
    orders: dict[str, VenueOrder]  # by order id (stands in for the cloid)


@dataclass(frozen=True)
class Store:
    """What survives a crash."""

    partitions: dict[str, Decimal]
    sagas: tuple[Saga, ...]
    kill_switch: bool
    seq_high: int


@dataclass(frozen=True)
class Run:
    """What a crash loses."""

    phase: Phase
    dry_streak: int


@dataclass(frozen=True)
class World:
    venue: Venue
    store: Store
    run: Run | None
    log: tuple[str, ...]


def initial() -> World:
    partitions = {"S1": Decimal("0.500"), "S2": Decimal("0.300"), UNATTRIBUTED: Decimal("-0.100")}
    return World(
        venue=Venue(position=Decimal("0.700"), orders={}),
        store=Store(partitions=partitions, sagas=(), kill_switch=False, seq_high=0),
        run=Run(phase=Phase.START, dry_streak=0),
        log=("boot: fresh run, nothing to resume",),
    )


# --- the split, the part worth keeping ------------------------------------------------


def split_moves(
    snapshot: dict[str, Decimal], order_abs: Decimal, filled: Decimal
) -> dict[str, Decimal]:
    """Each partition's cumulative move toward zero after `filled` of `order_abs`.

    Each partition moves the same share of the way. Rounding goes down to the lot, then
    the leftover lots go out one at a time, largest remainder first. So the moves always
    sum to the fill exactly, and a full fill moves every partition to exactly zero.
    """
    exact = {p: -s * filled / order_abs for p, s in snapshot.items()}
    rounded = {p: abs(e).quantize(LOT, ROUND_DOWN) * sign(e) for p, e in exact.items()}
    target = -sign(sum(snapshot.values())) * filled
    residual = target - sum(rounded.values())
    if residual:
        way = sign(residual)
        candidates = sorted(
            (p for p in exact if sign(exact[p] - rounded[p]) == way),
            key=lambda p: abs(exact[p] - rounded[p]),
            reverse=True,
        )
        i = 0
        while residual and candidates:
            p = candidates[i % len(candidates)]
            rounded[p] += LOT * way
            residual -= LOT * way
            i += 1
    return rounded


# --- helpers --------------------------------------------------------------------------


def _say(w: World, *lines: str) -> World:
    return replace(w, log=(w.log + lines)[-12:])


def _open_saga(store: Store) -> Saga | None:
    for s in reversed(store.sagas):
        if s.state not in TERMINAL:
            return s
    return None


def _put_saga(store: Store, saga: Saga) -> Store:
    sagas = tuple(saga if s.seq == saga.seq else s for s in store.sagas)
    return replace(store, sagas=sagas)


def _reducible(position: Decimal, side: int) -> Decimal:
    """How much a reduce-only order on `side` may still trade (P1)."""
    return max(ZERO, -side * position)


def _book_fill(w: World, saga: Saga, venue_filled: Decimal, why: str) -> World:
    """Book the change since the last fill, through the split on the stored snapshot."""
    if venue_filled == saga.filled:
        return w
    moved = split_moves(saga.snapshot, abs(saga.size), venue_filled)
    parts = dict(w.store.partitions)
    deltas = []
    for p, m in moved.items():
        d = m - saga.moved.get(p, ZERO)
        parts[p] = parts.get(p, ZERO) + d
        if d:
            deltas.append(f"{p} {d:+}")
    saga = replace(saga, filled=venue_filled, moved=moved, state=OrderState.PARTIALLY_FILLED)
    store = _put_saga(replace(w.store, partitions=parts), saga)
    return _say(
        replace(w, store=store), f"{why}: cum fill {venue_filled}, split {', '.join(deltas)}"
    )


def _end_saga(w: World, saga: Saga, status: str) -> World:
    state = {
        "filled": OrderState.FILLED,
        "canceled": OrderState.CANCELLED,
        "rejected": OrderState.REJECTED,
    }[status.split(":")[0]]
    saga = replace(saga, state=state)
    w = replace(w, store=_put_saga(w.store, saga))
    if w.run is not None:
        dry = 0 if saga.filled else w.run.dry_streak + 1
        w = replace(w, run=Run(phase=Phase.RECONCILE, dry_streak=dry))
    return _say(w, f"engine: {saga.order_id} -> {state} (filled {saga.filled})")


def _reconcile(w: World) -> World:
    gap = w.venue.position - sum(w.store.partitions.values())
    if not gap:
        return _say(w, "reconcile: books match the venue")
    if not w.venue.position:
        return _say(w, f"reconcile: gap {gap:+} but venue is flat, so no entry price. gap stays")
    parts = dict(w.store.partitions)
    parts[UNATTRIBUTED] = parts.get(UNATTRIBUTED, ZERO) + gap
    return _say(
        replace(w, store=replace(w.store, partitions=parts)),
        f"reconcile: healed {gap:+} into {UNATTRIBUTED}",
    )


# --- engine actions -------------------------------------------------------------------


def step(w: World) -> World:
    """The flatten driver's next move."""
    run = w.run
    if run is None:
        return _say(w, "engine is down. boot first")
    if run.phase is Phase.START:
        w = replace(
            w, store=replace(w.store, kill_switch=True), run=replace(run, phase=Phase.RECONCILE)
        )
        return _say(
            w, 'kill switch tripped: "flatten: operator exit"', "cancel all: nothing resting"
        )
    if run.phase is Phase.RECONCILE:
        w = _reconcile(w)
        return replace(w, run=replace(run, phase=Phase.DECIDE))
    if run.phase is Phase.DECIDE:
        return _decide(w, run)
    if run.phase is Phase.SEND:
        return _send(w, run)
    if run.phase is Phase.WAIT:
        return _say(w, "waiting on the venue: [f] fill or [e] end the IOC")
    return _say(w, f"flatten is over ({run.phase}). [n] to reset")


def _decide(w: World, run: Run) -> World:
    parts = w.store.partitions
    if not w.venue.position:
        strategies = {p: s for p, s in parts.items() if p != UNATTRIBUTED and s}
        if strategies:
            moved = dict(parts)
            for p, s in strategies.items():
                moved[p] = ZERO
                moved[UNATTRIBUTED] = moved.get(UNATTRIBUTED, ZERO) + s
            w = replace(w, store=replace(w.store, partitions=moved))
            w = _say(w, f"leftover rule: moved {strategies} into {UNATTRIBUTED}")
        left = w.store.partitions.get(UNATTRIBUTED, ZERO)
        w = replace(w, run=replace(run, phase=Phase.DONE))
        if left:
            return _say(w, f"DONE: venue flat. finding: {UNATTRIBUTED} holds {left}")
        return _say(w, "DONE: venue flat, every partition flat")
    if run.dry_streak >= MAX_DRY_ATTEMPTS:
        w = replace(w, run=replace(run, phase=Phase.GAVE_UP))
        return _say(w, f"GAVE UP: {run.dry_streak} attempts in a row filled nothing")
    if sum(parts.values()) != w.venue.position:
        w = replace(w, run=replace(run, phase=Phase.RECONCILE))
        return _say(w, "books do not match the venue. reconcile before placing")
    seq = w.store.seq_high + 1
    saga = Saga(
        seq=seq,
        size=-w.venue.position,
        snapshot=dict(parts),
        filled=ZERO,
        moved={},
        state=OrderState.PENDING,
    )
    store = replace(w.store, sagas=w.store.sagas + (saga,), seq_high=seq)
    w = replace(w, store=store, run=replace(run, phase=Phase.SEND))
    return _say(w, f"placed {saga.order_id}: size {saga.size:+}, saga stored with snapshot")


def _send(w: World, run: Run) -> World:
    saga = _open_saga(w.store)
    assert saga is not None
    side = sign(saga.size)
    order = VenueOrder(size=abs(saga.size), side=side, filled=ZERO, open=True, status="open")
    if not _reducible(w.venue.position, side):
        order = replace(order, open=False, status="rejected: reduce only would increase position")
    venue = replace(w.venue, orders={**w.venue.orders, saga.order_id: order})
    w = replace(w, venue=venue)
    w = _say(w, f"sent {saga.order_id} reduce-only IOC. venue: {order.status}")
    if not order.open:
        return _end_saga(w, saga, order.status)
    saga = replace(saga, state=OrderState.LIVE)
    return replace(w, store=_put_saga(w.store, saga), run=replace(run, phase=Phase.WAIT))


# --- venue actions --------------------------------------------------------------------


def _open_venue_order(w: World) -> tuple[str, VenueOrder] | None:
    for oid, o in w.venue.orders.items():
        if o.open:
            return oid, o
    return None


def venue_fill(w: World, qty: Decimal | None) -> World:
    found = _open_venue_order(w)
    if found is None:
        return _say(w, "venue: no open order to fill")
    oid, o = found
    cap = min(o.size - o.filled, _reducible(w.venue.position, o.side))
    qty = cap if qty is None else min(qty, cap)
    if qty <= 0:
        return _say(w, "venue: nothing left that reduce-only may fill")
    o = replace(o, filled=o.filled + qty)
    if o.filled == o.size:
        o = replace(o, open=False, status="filled")
    venue = Venue(position=w.venue.position + o.side * qty, orders={**w.venue.orders, oid: o})
    w = _say(replace(w, venue=venue), f"venue: {oid} filled {qty} (cum {o.filled})")
    if w.run is None:
        return _say(w, "engine is down: the fill waits at the venue")
    saga = _open_saga(w.store)
    assert saga is not None and saga.order_id == oid
    w = _book_fill(w, saga, o.filled, "engine")
    if not o.open:
        w = _end_saga(w, _open_saga(w.store) or saga, o.status)
    return w


def venue_end(w: World) -> World:
    """The IOC ends: the rest is cancelled, or the order is rejected if nothing matched."""
    found = _open_venue_order(w)
    if found is None:
        return _say(w, "venue: no open order")
    oid, o = found
    status = "canceled" if o.filled else "rejected: could not immediately match"
    o = replace(o, open=False, status=status)
    w = replace(w, venue=replace(w.venue, orders={**w.venue.orders, oid: o}))
    w = _say(w, f"venue: {oid} IOC ended -> {status}")
    if w.run is None:
        return w
    saga = _open_saga(w.store)
    assert saga is not None
    return _end_saga(w, saga, status)


def hand_trade(w: World, delta: Decimal) -> World:
    """Someone trades by hand in the venue UI. The books do not see it."""
    venue = replace(w.venue, position=w.venue.position + delta)
    return _say(
        replace(w, venue=venue), f"venue: hand trade {delta:+}, position now {venue.position}"
    )


# --- crash and boot -------------------------------------------------------------------


def crash(w: World) -> World:
    if w.run is None:
        return w
    return _say(replace(w, run=None), "CRASH: run state lost, store and venue remain")


def boot(w: World) -> World:
    """A new flatten run. Resume open sagas first, then start the job again."""
    if w.run is not None:
        return _say(w, "engine is already up. crash first")
    w = replace(w, run=Run(phase=Phase.START, dry_streak=0))
    w = _say(w, f"boot: kill switch in store = {w.store.kill_switch}")
    # Time passed while the engine was down, so any IOC at the venue has ended.
    found = _open_venue_order(w)
    if found is not None:
        oid, o = found
        status = "filled" if o.filled == o.size else ("canceled" if o.filled else "rejected")
        orders = {**w.venue.orders, oid: replace(o, open=False, status=status)}
        w = _say(
            replace(w, venue=replace(w.venue, orders=orders)),
            f"venue: {oid} ended while down -> {status}",
        )
    saga = _open_saga(w.store)
    if saga is None:
        return _say(w, "boot: no open saga to resume")
    o = w.venue.orders.get(saga.order_id)
    if o is None:
        saga = replace(saga, state=OrderState.FAILED)
        return _say(
            replace(w, store=_put_saga(w.store, saga)),
            f"resume {saga.order_id}: venue never saw it -> failed",
        )
    w = _book_fill(w, saga, o.filled, f"resume {saga.order_id}")
    w = _end_saga(w, _open_saga(w.store) or saga, o.status)
    # The resumed attempt belongs to the last life. This run starts its job from the top.
    return replace(w, run=Run(phase=Phase.START, dry_streak=0))
