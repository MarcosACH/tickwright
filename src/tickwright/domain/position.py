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
    """

    symbol: str
    size: Decimal
    entry_price: Decimal
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal


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

    def view(self) -> PositionView:
        """A frozen Tier-1 snapshot of this partition's own-attribution slice."""
        return PositionView(
            symbol=self.symbol,
            size=self.signed_size,
            entry_price=self.entry_price,
            realized_pnl=self.realized_pnl,
            fees=self.fees,
            funding=self.funding,
        )

    def apply(self, event: OrderFillEvent, *, side: Side) -> tuple[PositionChange, ...]:
        """Fold ``event`` into this partition; idempotent and partition-checked.

        ``side`` rides the saga rather than the event: ``OrderFillEvent`` carries
        the trade (quantity, price, ids) and the ``Order`` carries the direction,
        so the ``ExecutionManager`` — which holds both — supplies it.

        Returns the changes this fill made, or ``()`` for a redelivered fill the
        ledger already reflects, so a caller can suppress a duplicate
        announcement (at-least-once idempotency, ADR-0025).
        """
        if event.symbol != self.symbol or event.strategy_id != self.strategy_id:
            raise InvariantViolation(
                f"fill {event.event_id} for ({event.strategy_id}, {event.symbol}) applied to "
                f"position ({self.strategy_id}, {self.symbol})"
            )
        if event.event_id in self._applied_event_ids:
            return ()  # Already reflected — duplicate delivery is a no-op.

        self._applied_event_ids.add(event.event_id)
        return self._book(
            signed=event.quantity if side is Side.BUY else -event.quantity, price=event.price
        )

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
