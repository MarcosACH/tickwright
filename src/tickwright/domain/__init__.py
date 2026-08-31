"""The domain: events, value types, id derivation, the order saga, and the seam
Protocols. Depends on nothing outside the standard library (ADR-0032).

This package *defines* the seams every adapter and the engine compile against.
It is the stable contract of the system; keep it log-free and dependency-free.
"""

from .account import Account, AccountSpec, AccountView, venue_cash
from .economics import fill_fee, funding_amount, funding_boundaries
from .enums import (
    AggressorSide,
    ComponentState,
    Netting,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
)
from .errors import (
    InvariantViolation,
    LeverageOutOfBounds,
    StartupReconciliationTimeout,
    StoreAccountMismatch,
    VenueAccountModeUnsupported,
    VenueFactUnsupported,
    VenueReadUnresolvable,
)
from .events import (
    CancelSignal,
    Event,
    ExecutionReport,
    FillReport,
    FundingAccrual,
    MarketTick,
    MarkTick,
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
    VenueAccountState,
    VenueOrderView,
    VenuePositionState,
    VenueReadFailure,
)
from .figures import exact_figure
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
from .leverage import (
    DEFAULT_LEVERAGE,
    EMPTY_LEVERAGE_BOOK,
    LeverageBook,
    LeverageSpec,
    MarginMode,
)
from .order import Order
from .position import Position, PositionChange, PositionView, account_net_size
from .protocols import (
    Clock,
    EventBus,
    Exchange,
    Handler,
    MarketFeed,
    Portfolio,
    PreTradeGuard,
    ReplayClock,
    Store,
    Strategy,
)
from .valuation import account_view, position_view

__all__ = [
    # enums
    "AggressorSide",
    "ComponentState",
    "Netting",
    "OrderState",
    "OrderType",
    "Side",
    "TimeInForce",
    # errors
    "InvariantViolation",
    "LeverageOutOfBounds",
    "StartupReconciliationTimeout",
    "StoreAccountMismatch",
    "VenueAccountModeUnsupported",
    "VenueFactUnsupported",
    "VenueReadUnresolvable",
    # events + value types
    "DEFAULT_LEVERAGE",
    "EMPTY_LEVERAGE_BOOK",
    "Account",
    "AccountSpec",
    "AccountView",
    "Approved",
    "CancelSignal",
    "Denied",
    "Event",
    "ExecutionReport",
    "FillReport",
    "FundingAccrual",
    "GuardDecision",
    "InstrumentSpec",
    "KillSwitchState",
    "LeverageBook",
    "LeverageSpec",
    "MarginMode",
    "MarkTick",
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
    "Position",
    "PositionChange",
    "PositionView",
    "Signal",
    "VenueAccountState",
    "VenueOrderView",
    "VenueReadFailure",
    "VenuePositionState",
    # id derivation
    "derive_cloid",
    "SignalId",
    # figure guard
    "exact_figure",
    # quantization
    "below_min_notional",
    "quantize_price",
    "quantize_size",
    # boundary economics
    "fill_fee",
    "funding_amount",
    "funding_boundaries",
    # the Σ-invariant's left-hand side
    "account_net_size",
    # Tier-2 view assembly (pure, never stored)
    "account_view",
    "position_view",
    "venue_cash",
    # seam Protocols
    "Clock",
    "EventBus",
    "Exchange",
    "Handler",
    "MarketFeed",
    "Portfolio",
    "PreTradeGuard",
    "ReplayClock",
    "Store",
    "Strategy",
]
