"""``PortfolioProjection`` — the write-through projection of position and
account state (ADR-0035): the economic sibling of the order ``Cache``.

It is a **separate** projection from the ``Cache``, not a widening of it —
different key, different rows, different venue anchor — and, crucially, it is
**not a bus subscriber** for fills. Tier-1 is written synchronously on the
``ExecutionManager``'s fill-apply path, so the projection is the fill's *writer*
and applies it exactly once; a strategy reading its position from the
``OrderFilled`` handler reads the state that fill just produced (ADR-0045 §1).

The concrete carries a **wider read surface than the ``Portfolio`` seam** — every
partition including the reserved unattributed one — because reconciliation,
telemetry and the CLI read this class directly and a strategy must never make
those reads (ADR-0041 §8). The scoped facade ``for_strategy`` hands out is what
implements the seam.
"""

from dataclasses import dataclass

from tickwright.domain import (
    Account,
    AccountView,
    OrderFillEvent,
    Portfolio,
    Position,
    PositionChange,
    PositionView,
    Side,
)
from tickwright.observability import NamedEvent, named_event

# What a fill did to a position → its cataloged name (ADR-0045 §2). A position
# change is never a bus event — it is an output derived from a fill already on
# the bus — so the named-event catalog is the only place it is observable from
# outside, and a ``PositionChange`` missing from this map fails with a KeyError.
_POSITION_EVENTS: dict[PositionChange, NamedEvent] = {
    PositionChange.OPENED: NamedEvent.POSITION_OPENED,
    PositionChange.CHANGED: NamedEvent.POSITION_CHANGED,
    PositionChange.CLOSED: NamedEvent.POSITION_CLOSED,
}


class PortfolioProjection:
    """The one owner of "what do I hold, and what has it earned"."""

    def __init__(self, *, account: Account) -> None:
        self._account = account
        # Keyed ``(strategy_id, symbol)`` — the materialized runtime key of the
        # logical ``(account, strategy, symbol)`` grain, the account being a
        # deployment fact (ADR-0038). ``None`` is the reserved unattributed
        # partition, reachable here but never through the seam.
        self._positions: dict[tuple[str | None, str], Position] = {}

    def apply_fill(self, event: OrderFillEvent, *, side: Side) -> None:
        """Fold a fill into its partition and the cash line — the single write
        verb of the Tier-1 path.

        ``side`` comes from the saga rather than the event: ``OrderFillEvent``
        carries the trade and the ``Order`` carries the direction, and the
        ``ExecutionManager`` holds both. A redelivered fill is a no-op in both
        aggregates and announces nothing.
        """
        key = (event.strategy_id, event.symbol)
        position = self._positions.get(key)
        if position is None:
            position = Position(strategy_id=event.strategy_id, symbol=event.symbol)
        realized_before = position.realized_pnl
        # Applied before the partition is filed, so a fill the aggregate refuses
        # — a misroute, a non-positive quantity — leaves no trace here either. A
        # partition materialized by a refusal would report a traded-flat record
        # for a symbol that was never traded, inverting ``position``'s ``None``.
        changes = position.apply(event, side=side)
        self._positions[key] = position
        if not changes:
            return
        # Realized PnL is one of the four accruing inputs to cash (ADR-0042 §4),
        # and the position is what booked it — so the delta is read off the
        # aggregate rather than recomputed here. That read is also what makes
        # the cash line idempotent without a second applied set on ``Account``:
        # ``Position.apply`` is the fill's one gatekeeper, so a redelivery books
        # nothing and the delta it contributes here is zero by construction.
        # (The early return above suppresses the *announcement*, not the accrual
        # — it is not what keeps the cash line from double-counting.)
        self._account.accrue_realized(
            position.realized_pnl - realized_before, event_id=event.event_id
        )
        for change in changes:
            named_event(
                _POSITION_EVENTS[change],
                strategy_id=event.strategy_id,
                symbol=event.symbol,
                # The size *that change* produced, not the fill's end state. The
                # two differ only on a flip, where the old leg closes at zero
                # before the residual opens: reusing the post-fill size for both
                # halves would announce a close at the residual's size and
                # invert the name. This catalog is the only place a position
                # change is observable (ADR-0045 §1), so there is nothing else a
                # reader could reconcile that against.
                size="0" if change is PositionChange.CLOSED else str(position.signed_size),
            )

    def position(self, symbol: str, *, strategy_id: str | None) -> PositionView | None:
        """One partition's frozen Tier-1 snapshot, or ``None`` if never traded."""
        position = self._positions.get((strategy_id, symbol))
        return position.view() if position is not None else None

    def open_positions(self, *, strategy_id: str | None) -> tuple[PositionView, ...]:
        """Every partition of ``strategy_id`` still holding exposure."""
        return tuple(
            position.view()
            for (owner, _symbol), position in self._positions.items()
            if owner == strategy_id and not position.is_flat
        )

    def account(self) -> AccountView:
        """The account-wide pool — one collateral bucket, never scoped."""
        return self._account.view()

    def for_strategy(self, strategy_id: str) -> Portfolio:
        """The scoped ``Portfolio`` facade the composition root injects into a
        strategy. Bound to a real ``strategy_id``, so the unattributed partition
        is structurally unreachable through it (ADR-0041 §5)."""
        return _ScopedPortfolio(projection=self, strategy_id=strategy_id)


@dataclass(frozen=True, slots=True)
class _ScopedPortfolio:
    """One strategy's view of the projection — the ``Portfolio`` seam's only impl.

    Holds no state of its own: it is the binding of a ``strategy_id`` to the
    projection, which is what lets the seam's methods take no scope argument.
    """

    projection: PortfolioProjection
    strategy_id: str

    def position(self, symbol: str) -> PositionView | None:
        return self.projection.position(symbol, strategy_id=self.strategy_id)

    def open_positions(self) -> tuple[PositionView, ...]:
        return self.projection.open_positions(strategy_id=self.strategy_id)

    def account(self) -> AccountView:
        return self.projection.account()
