"""``ScopedPortfolio``: one strategy's view of the engine's two projections (ADR-0041).

Positions and the account come from the ``PortfolioProjection``. Open orders come
from the order ``Cache``. The facade binds one ``strategy_id`` to both, so a
strategy never holds either projection and cannot reach another strategy's rows
or the unattributed partition (ADR-0041 §5).
"""

from dataclasses import dataclass

from tickwright.domain import AccountView, OpenOrderView, Order, PositionView

from .cache import Cache
from .portfolio import PortfolioProjection


@dataclass(frozen=True, slots=True)
class ScopedPortfolio:
    """The ``Portfolio`` seam's only implementation. It holds no state of its own."""

    projection: PortfolioProjection
    cache: Cache
    strategy_id: str

    def position(self, symbol: str) -> PositionView | None:
        return self.projection.position(symbol, strategy_id=self.strategy_id)

    def open_positions(self) -> tuple[PositionView, ...]:
        return self.projection.open_positions(strategy_id=self.strategy_id)

    def account(self) -> AccountView:
        return self.projection.account()

    def open_orders(self, symbol: str | None = None) -> tuple[OpenOrderView, ...]:
        orders = self.cache.open_orders(strategy_id=self.strategy_id, symbol=symbol)
        return tuple(_view(order) for order in orders)


def _view(order: Order) -> OpenOrderView:
    # A copy, because the ``Order`` is the live saga and carries ``apply()``.
    return OpenOrderView(
        signal_id=order.signal_id,
        symbol=order.symbol,
        side=order.side,
        order_type=order.order_type,
        time_in_force=order.time_in_force,
        quantity=order.quantity,
        filled_quantity=order.cum_qty,
        price=order.price,
        post_only=order.post_only,
        reduce_only=order.reduce_only,
        state=order.state,
        cancel_requested=order.cancel_requested,
    )
