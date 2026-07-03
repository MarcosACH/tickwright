"""The domain: events, value types, id derivation, the order saga, and the seam
Protocols. Depends on nothing outside the standard library (ADR-0032).

This package *defines* the seams every adapter and the engine compile against.
It is the stable contract of the system; keep it log-free and dependency-free.
"""

from .enums import AggressorSide, OrderState, OrderType, Side, TimeInForce
from .errors import InvariantViolation
from .events import (
    Event,
    ExecutionReport,
    FillReport,
    MarketTick,
    OrderCancelled,
    OrderDenied,
    OrderEvent,
    OrderFailed,
    OrderFilled,
    OrderFillEvent,
    OrderLive,
    OrderPartiallyFilled,
    OrderPlaced,
    OrderRejected,
    OrderStatusReport,
    OrderSubmitted,
    PlaceOrder,
    PlaceSignal,
    Signal,
)
from .ids import derive_cloid
from .order import Order
from .protocols import (
    Clock,
    EventBus,
    Exchange,
    Handler,
    MarketFeed,
    ReplayClock,
    Store,
    Strategy,
)

__all__ = [
    # enums
    "AggressorSide",
    "OrderState",
    "OrderType",
    "Side",
    "TimeInForce",
    # errors
    "InvariantViolation",
    # events + value types
    "Event",
    "ExecutionReport",
    "FillReport",
    "MarketTick",
    "Order",
    "OrderCancelled",
    "OrderDenied",
    "OrderEvent",
    "OrderFailed",
    "OrderFillEvent",
    "OrderFilled",
    "OrderLive",
    "OrderPartiallyFilled",
    "OrderPlaced",
    "OrderRejected",
    "OrderStatusReport",
    "OrderSubmitted",
    "PlaceOrder",
    "PlaceSignal",
    "Signal",
    # id derivation
    "derive_cloid",
    # seam Protocols
    "Clock",
    "EventBus",
    "Exchange",
    "Handler",
    "MarketFeed",
    "ReplayClock",
    "Store",
    "Strategy",
]
