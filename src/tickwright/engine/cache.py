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

    def rebuild(self) -> None:
        """Reload the projection from the durable store (recovery step 2)."""
        self._orders = {order.cloid: order for order in self._store.all_orders()}

    def get_order(self, cloid: str) -> Order | None:
        """The saga for ``cloid`` as of the last checkpoint, or ``None`` if unknown."""
        return self._orders.get(cloid)
