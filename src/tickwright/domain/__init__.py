"""The domain: events, value types, id derivation, the order saga, and the seam
Protocols. Depends on nothing outside the standard library (ADR-0032).

This package *defines* the seams every adapter and the engine compile against.
It is the stable contract of the system; keep it log-free and dependency-free.
"""

from .enums import AggressorSide, ComponentState, OrderState, OrderType, Side, TimeInForce
from .errors import InvariantViolation, StartupReconciliationTimeout
from .events import (
    CancelSignal,
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
    VenueOrderView,
)
from .ids import SignalId, derive_cloid
from .instrument import (
    Approved,
    Denied,
    GuardDecision,
    InstrumentSpec,
    KillSwitchState,
    below_min_notional,
    quantize_price,
    quantize_size,
)
from .order import Order
from .protocols import (
    Clock,
    EventBus,
    Exchange,
    Handler,
    MarketFeed,
    PreTradeGuard,
    ReplayClock,
    Store,
    Strategy,
)

__all__ = [
    # enums
    "AggressorSide",
    "ComponentState",
    "OrderState",
    "OrderType",
    "Side",
    "TimeInForce",
    # errors
    "InvariantViolation",
    "StartupReconciliationTimeout",
    # events + value types
    "Approved",
    "CancelSignal",
    "Denied",
    "Event",
    "ExecutionReport",
    "FillReport",
    "GuardDecision",
    "InstrumentSpec",
    "KillSwitchState",
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
    "VenueOrderView",
    # id derivation
    "derive_cloid",
    "SignalId",
    # quantization
    "below_min_notional",
    "quantize_price",
    "quantize_size",
    # seam Protocols
    "Clock",
    "EventBus",
    "Exchange",
    "Handler",
    "MarketFeed",
    "PreTradeGuard",
    "ReplayClock",
    "Store",
    "Strategy",
]
