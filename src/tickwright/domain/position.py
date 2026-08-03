"""The per-``(strategy, symbol)`` economic aggregate (ADR-0035).

``Position`` is to the accounting surface what ``Order`` is to the saga: the one
place the algorithm the whole surface's correctness rests on — average-cost
accounting — is written, unit-testable with zero infrastructure. It is
idempotent on ``event_id`` so a redelivered fill, a reconciler synthetic and a
restart replay all converge (ADR-0025), and it raises ``InvariantViolation``
rather than silently skipping an application it cannot make (ADR-0014).

``strategy_id`` is ``str | None``, ``None`` being the reserved unattributed
partition holding flow the engine never placed (ADR-0038).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from .enums import Side
from .errors import InvariantViolation
from .events import OrderFillEvent

_ZERO = Decimal("0")


class PositionChange(StrEnum):
    """What a fill did to a position — the aggregate's own classification.

    Returned by ``apply`` so the caller can announce it without re-deriving it
    from before/after states. A flip through zero returns ``(CLOSED, OPENED)``:
    the residual opens a fresh average-cost record, so it is genuinely two
    facts (ADR-0045 §2).
    """

    OPENED = "opened"
    CHANGED = "changed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionView:
    """The frozen read-only snapshot the ``Portfolio`` seam returns (ADR-0041 §1).

    A distinct type from the mutable aggregate, which moves under the reader on
    the next fill. Every Tier-1 field is a real ``Decimal``, never ``None`` —
    readable even in the recovery window before any mark (ADR-0041 §6).

    Assembled by ``domain.valuation``, never by the aggregate itself: the
    position-grain half is computed off the symbol's *account-net* size, which
    no single partition holds (ADR-0035).

    **No field defaults**, the Tier-2 ones included. The nullability is real —
    an absent mark is a genuine state — but *defaulting* to it is a different
    thing: it would let a caller construct a view that claims "no mark was ever
    seen" without going near the per-term rule that decides when that is true.
    That is the same silent-wrong-answer ``position_view`` refuses for its own
    inputs, and refusing it here is what makes the assembly function the only
    way to obtain a view whose fields all came from one ``(position, mark)``
    read (ADR-0041 §1). ``kw_only`` throughout, so this costs no field ordering.
    """

    symbol: str
    size: Decimal
    entry_price: Decimal
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    unrealized_pnl: Decimal | None
    """This partition's own open exposure marked to the latest mark, Tier-2 and
    recomputed on every read (ADR-0034).

    ``None`` when the mark is absent **and this term needs it** — a flat slice
    reads ``0``, because ``0 × (mark − entry)`` needs no mark (ADR-0041 §6).
    Never fabricated as a zero on a real position: unknown and worthless are
    different answers, and only one of them is safe to trade on."""
    notional: Decimal | None
    """The symbol's exposure at position grain: ``|account net size| × mark``.

    **Not** this strategy's slice — the venue holds one position per symbol and
    keys every economic property of it there, never per strategy (ADR-0041 §4),
    so two strategies long the same symbol read one notional. A magnitude, since
    exposure has no direction. ``None`` on an absent mark unless the account nets
    flat, where ``|0| × mark`` is zero at every mark."""
    mark_ts: int | None
    """The observation instant of the mark this view was valued at, ``None`` when
    the projection holds none for the symbol (ADR-0041 §6).

    Staleness is **exposed, not decided**: there is no max-age on the read path,
    so a strategy — which holds a ``Clock`` — compares this to now and judges for
    itself. Without it a stale-but-present mark would be undetectable, since a
    frozen mark still produces real numbers."""


def account_net_size(positions: Iterable["Position"]) -> dict[str, Decimal]:
    """The **account net size** per symbol — ADR-0034's Σ over every partition.

    ``Σ(per-strategy signed size per symbol) = account net size = venue szi`` is
    the one invariant bridging the per-strategy partitions to the venue's own
    truth, and this is its left-hand side computed. Every partition counts,
    the reserved unattributed one included: it holds flow the engine never
    placed, which the venue is nonetheless holding, so omitting it would net to
    something no venue reports (ADR-0043 §9).

    A pure fold over whatever partitions the caller has — the durable mass-read
    on the recovery path, the projection's own map on the live one — so that
    "how much of this symbol does the account hold" has exactly one definition
    to disagree with. Symbols that net to flat are kept rather than dropped:
    zero *is* the answer for a symbol traded to flat, and a caller that must
    distinguish it from "never traded" can, while one that need not can treat
    both alike.
    """
    net: dict[str, Decimal] = {}
    for position in positions:
        net[position.symbol] = net.get(position.symbol, _ZERO) + position.signed_size
    return net


@dataclass(slots=True)
class Position:
    """One ``(strategy, symbol)`` partition's Tier-1 ledger, advanced by ``apply``.

    ``signed_size`` is positive long, negative short, zero flat; ``entry_price``
    is meaningful only while non-flat and resets on a full close. ``realized_pnl``
    is **gross** — fees and funding accrue on their own lines and are never
    folded in (ADR-0045 §3).
    """

    strategy_id: str | None
    symbol: str
    signed_size: Decimal = _ZERO
    entry_price: Decimal = _ZERO
    realized_pnl: Decimal = _ZERO
    fees: Decimal = _ZERO
    funding: Decimal = _ZERO
    isolated_collateral: Decimal = _ZERO
    _applied_event_ids: set[str] = field(default_factory=set, init=False, repr=False)

    @property
    def applied_event_ids(self) -> frozenset[str]:
        """The reflected ``event_id``s — a **process-lifetime** dedup set.

        Unlike ``Order``'s identically-named set, this one is *not* durable and
        must not be made so: ADR-0043 §4 rejects a ledger-side applied set by
        name, and §3's ``positions`` schema carries no column for it. A saga's
        set is bounded by one order's fills and dies with the order; a
        position's accumulates every fill for the life of the ledger, so
        persisting it reintroduces on the hot row the unbounded growth ADR-0043
        §1 declined. ``funding_marks`` is the ledger's one durable idempotency
        record (ADR-0043 §5.2).

        So what this set guarantees is *within a run*: at-least-once redelivery
        of a fill is a no-op (ADR-0025). The restart gap is closed instead by
        writing the order checkpoint and the ledger in one transaction
        (ADR-0043 §4), which is why no set is needed here to survive one.
        """
        return frozenset(self._applied_event_ids)

    @property
    def is_flat(self) -> bool:
        """Whether the partition holds no exposure. A flat record is still a
        record: realized PnL, fees and funding are retained (P1 #119)."""
        return self.signed_size == _ZERO

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        """This partition's open exposure marked to ``mark`` (ADR-0045).

        ``signed_size × (mark − entry_price)``, so the sign rides the position
        rather than the mark's direction: a short gains as the mark falls. The
        one Tier-2 query the aggregate keeps, because its own state plus a mark
        is the whole of what it needs — everything account-net- or pool-coupled
        lives in ``valuation`` instead (ADR-0035).

        **Gross**, like realized PnL: fees and funding accrue on their own lines
        and are never folded in (ADR-0045 §3).
        """
        return self.signed_size * (mark - self.entry_price)

    def apply(self, event: OrderFillEvent, *, side: Side) -> tuple[PositionChange, ...]:
        """Fold ``event`` into this partition; idempotent and checked.

        A bookable fill belongs to this partition and moves a positive quantity;
        anything else is an ``InvariantViolation``. Stating both here is what
        lets every producer — venue ingress, reconciler synthetics, replay —
        inherit the precondition instead of each honouring it unstated.

        ``side`` rides the saga rather than the event: ``OrderFillEvent`` carries
        the trade (quantity, price, ids) and the ``Order`` carries the direction,
        so the ``ExecutionManager`` — which holds both — supplies it. Quantity is
        therefore a magnitude: a negative one would invert the saga's direction.

        Returns the changes this fill made, or ``()`` for a redelivered fill the
        ledger already reflects, so a caller can suppress a duplicate
        announcement (at-least-once idempotency, ADR-0025).
        """
        if event.symbol != self.symbol or event.strategy_id != self.strategy_id:
            raise InvariantViolation(
                f"fill {event.event_id} for ({event.strategy_id}, {event.symbol}) applied to "
                f"position ({self.strategy_id}, {self.symbol})"
            )
        if event.quantity <= _ZERO:
            # Ahead of the dedup, so the refusal burns nothing: a producer that
            # corrects the quantity and re-sends the trade is still booked. A
            # zero would take the flat branch below and leave the partition flat
            # at a non-zero entry — a state the aggregate's own contract and the
            # ``PositionChange`` catalog have no name for (ADR-0045 §2).
            raise InvariantViolation(
                f"fill {event.event_id} has non-positive quantity {event.quantity}"
            )
        if event.event_id in self._applied_event_ids:
            return ()  # Already reflected — duplicate delivery is a no-op.

        self._applied_event_ids.add(event.event_id)
        # Behind the dedup and outside ``_book``, both deliberately. Behind it,
        # because this is the fill's one gatekeeper and the fee must be charged
        # exactly once for the same reason the size may only move once. Outside
        # ``_book``, because the reducer is average-cost accounting and a fee is
        # not part of it: it accrues on this line whatever regime the fill lands
        # in, and never reaches ``entry_price`` or ``realized_pnl`` (ADR-0036).
        self.fees += event.fee
        return self._book(
            signed=event.quantity if side is Side.BUY else -event.quantity, price=event.price
        )

    def accrue_funding(self, amount: Decimal) -> None:
        """Add one boundary's signed funding to this partition's own line.

        A verb of its own rather than a field on ``apply``, because funding is
        the one accounting input that arrives on **no carrier fill** (ADR-0037):
        there is nothing to fold it into. It reaches neither ``entry_price`` nor
        ``realized_pnl`` — the same separation the fee keeps, and for the same
        reason (ADR-0045 §3) — and it is **retained through a close**: the
        payment left the account when the boundary settled, so unwinding it on
        the way to flat would invent a refund the venue never made.

        **Takes no ``event_id`` and dedups nothing**, which is the deliberate
        difference from ``apply``. A fill's gatekeeper is this aggregate's
        process-lifetime applied set; an accrual's is the *durable* per-symbol
        watermark in the ledger, at ``(symbol, boundary_ts)`` grain (ADR-0043
        §5.2) — a grain no position row is entitled to hold, since one accrual
        may be split across several of them. A second key here would shadow that
        one and answer for a boundary it cannot see.
        """
        self.funding += amount

    def _book(self, *, signed: Decimal, price: Decimal) -> tuple[PositionChange, ...]:
        """The average-cost reducer, in four regimes (P1 [#119]).

        Open from flat (entry := the fill price), add on the same side (entry
        becomes the notional-weighted mean), reduce or fully close on the
        opposite side (realize the closed portion at the *old* entry), and flip
        through zero (realize the **whole** old leg, then open the residual
        fresh at the fill price).
        """
        if self.is_flat:
            self.signed_size = signed
            self.entry_price = price
            return (PositionChange.OPENED,)

        if (self.signed_size > _ZERO) == (signed > _ZERO):
            size = abs(self.signed_size)
            added = abs(signed)
            self.entry_price = (self.entry_price * size + price * added) / (size + added)
            self.signed_size += signed
            return (PositionChange.CHANGED,)

        # Opposite side: realize against the exposure this fill closes, signed
        # with that exposure — a short closed below its entry books a profit.
        closed = min(abs(self.signed_size), abs(signed))
        direction = Decimal(1) if self.signed_size > _ZERO else Decimal(-1)
        self.realized_pnl += (price - self.entry_price) * closed * direction
        remaining = self.signed_size + signed
        self.signed_size = remaining

        if remaining == _ZERO:
            self.entry_price = _ZERO
            return (PositionChange.CLOSED,)
        if (remaining > _ZERO) == (direction > _ZERO):
            return (PositionChange.CHANGED,)  # Partial reduce: the entry holds.
        # Flipped through zero: the residual is a fresh record at the fill price.
        self.entry_price = price
        return (PositionChange.CLOSED, PositionChange.OPENED)
