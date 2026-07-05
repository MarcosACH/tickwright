"""``Cache`` — the in-memory read-model of current order state (ADR-0009).

A write-through projection of the ``Store``, **never** the source of truth:
``rebuild`` reloads it from the store on startup (recovery step 2), and reads
are direct method calls — pull-then-subscribe, because startup events predate
any strategy's subscription. Writes flow only through the ``ExecutionManager``'s
checkpoint path.
"""

from tickwright.domain import Order, Store


class Cache:
    """The one owner of "what is true now": sagas projected from the ``Store``."""

    def __init__(self, *, store: Store) -> None:
        self._store = store
        self._orders: dict[str, Order] = {}
        # Last-event recency per cloid, tracked only from this session's writes so
        # the ghost cycle can protect a just-acked order the venue hasn't yet
        # propagated (ADR-0011 inv 3). Deliberately not restored on ``rebuild``:
        # a recovered saga is not "just-acked this session" — the startup barrier
        # re-proves it against venue truth before anything places (ADR-0009), the
        # same in-memory-and-reset-on-restart discipline the absence trackers keep.
        self._last_event_ns: dict[str, int] = {}

    def checkpoint(self, order: Order, *, ts_ns: int) -> None:
        """Write through: durably checkpoint ``order``, then project it.

        Store first — the projection must never be ahead of the durable record,
        or a crash between the two would recover less than readers already saw.
        """
        self._store.checkpoint(order, ts_ns=ts_ns)
        self._orders[order.cloid] = order
        self._last_event_ns[order.cloid] = ts_ns

    def rebuild(self) -> None:
        """Reload the projection from the durable store (recovery step 2)."""
        self._orders = {order.cloid: order for order in self._store.all_orders()}
        self._last_event_ns = {}

    def get_order(self, cloid: str) -> Order | None:
        """The saga for ``cloid`` as of the last checkpoint, or ``None`` if unknown."""
        return self._orders.get(cloid)

    def last_event_ts(self, cloid: str) -> int | None:
        """The ``ts_ns`` of ``cloid``'s most recent checkpoint *this session*, or
        ``None`` if it has not been written since the last ``rebuild`` — the
        recency the ghost cycle reads to protect a just-acked order (ADR-0011)."""
        return self._last_event_ns.get(cloid)

    def open_orders(
        self, *, strategy_id: str | None = None, symbol: str | None = None
    ) -> list[Order]:
        """Every non-terminal saga, optionally narrowed by strategy and symbol.

        "Open" means still awaiting a venue resolution — a ``PENDING`` intent is
        open exposure too (ADR-0008). Unfiltered, this is the reconciler's
        worklist; filtered, a strategy's ``on_start`` pull (ADR-0024).
        """
        return [
            order
            for order in self._orders.values()
            if not order.is_terminal
            and (strategy_id is None or order.strategy_id == strategy_id)
            and (symbol is None or order.symbol == symbol)
        ]
