"""The domain: events, value types, id derivation, the order saga, and the seam
Protocols. Depends on nothing outside the standard library (ADR-0032).

This package *defines* the seams every adapter and the engine compile against.
It is the stable contract of the system; keep it log-free and dependency-free.
"""

from .account import Account, AccountSpec, AccountView, venue_cash
from .duration import duration_ns
from .economics import fill_fee, funding_amount, funding_boundaries
from .emitter import SignalEmitter
from .enums import (
    AggressorSide,
    ComponentState,
    Netting,
    OrderRefusal,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
)
from .errors import (
    InvariantViolation,
    LeverageOutOfBounds,
    OpenOrdersReadBeforeStart,
    StartupReconciliationTimeout,
    StoreAccountMismatch,
    StoreLockHeld,
    StoreNeverUsed,
    VenueAccountModeUnsupported,
    VenueFactUnsupported,
    VenueLeverageMismatch,
    VenueLeveragePushFailed,
    VenueReadUnresolvable,
    VenueSubscriptionUnreachable,
)
from .events import (
    CancelAllSignal,
    CancelSignal,
    CashCorrection,
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
    OrderRefusedReport,
    OrderRejected,
    OrderStatusReport,
    OrderSubmitted,
    PlaceSignal,
    ReconciliationFill,
    Signal,
    publishable_event_types,
)
from .figures import exact_figure
from .ids import SignalId, derive_cloid
from .instrument import (
    Approved,
    Denied,
    GuardDecision,
    InstrumentSpec,
    KillSwitchState,
    PreTradeReading,
    below_min_notional,
    min_notional_refuses,
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
from .order import WORKING_STATES, OpenOrderView, Order
from .ownership import SymbolOwnership
from .pacing import Backoff, Deadline
from .position import (
    OPERATOR,
    UNATTRIBUTED,
    PartitionFill,
    Position,
    PositionChange,
    PositionView,
    account_net_size,
    is_flatten,
    reserved_id_refusal,
)
from .protocols import (
    AccountAnchor,
    Clock,
    EventBus,
    Exchange,
    Handler,
    MarketFeed,
    OrderAnchor,
    Portfolio,
    PreTradeGuard,
    ReplayClock,
    Store,
    Strategy,
)
from .store_lock import StoreLockHolder
from .valuation import (
    LiquidationSource,
    SymbolValuation,
    account_valuation,
    account_view,
    position_view,
)
from .venue import (
    AccountModeVerdict,
    ExternalOrderRef,
    LimitOrder,
    MarketOrder,
    OrderRef,
    PlaceOrder,
    VenueAccountState,
    VenueOpenOrder,
    VenueOrderView,
    VenuePositionState,
    VenueReadFailure,
    place_order_of,
)

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
    "OpenOrdersReadBeforeStart",
    "StartupReconciliationTimeout",
    "StoreAccountMismatch",
    "StoreLockHeld",
    "StoreNeverUsed",
    "VenueAccountModeUnsupported",
    "VenueFactUnsupported",
    "VenueLeverageMismatch",
    "VenueLeveragePushFailed",
    "VenueReadUnresolvable",
    "VenueSubscriptionUnreachable",
    # events + value types
    "DEFAULT_LEVERAGE",
    "EMPTY_LEVERAGE_BOOK",
    "OPERATOR",
    "UNATTRIBUTED",
    "WORKING_STATES",
    "Account",
    "AccountModeVerdict",
    "AccountSpec",
    "AccountView",
    "Approved",
    "CancelAllSignal",
    "CancelSignal",
    "CashCorrection",
    "Denied",
    "Event",
    "ExecutionReport",
    "ExternalOrderRef",
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
    "OpenOrderView",
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
    "OrderRef",
    "OrderRefusal",
    "OrderRefusedReport",
    "OrderRejected",
    "OrderStatusReport",
    "OrderSubmitted",
    "PartitionFill",
    "PlaceOrder",
    "LimitOrder",
    "MarketOrder",
    "PlaceSignal",
    "Position",
    "PositionChange",
    "PositionView",
    "PreTradeReading",
    "ReconciliationFill",
    "Signal",
    "StoreLockHolder",
    "SymbolOwnership",
    "VenueAccountState",
    "VenueOpenOrder",
    "VenueOrderView",
    "VenueReadFailure",
    "VenuePositionState",
    # the event tree's leaves, for gates that must cover every family
    "publishable_event_types",
    # id derivation
    "derive_cloid",
    "SignalId",
    # figure guard
    "exact_figure",
    # quantization
    "below_min_notional",
    "min_notional_refuses",
    "quantize_price",
    "quantize_size",
    # boundary economics
    "fill_fee",
    "funding_amount",
    "funding_boundaries",
    # the Σ-invariant's left-hand side
    "account_net_size",
    # the one rule for the reserved owner ids, read by config and StrategyHost
    "reserved_id_refusal",
    # the one rule for which orders are flatten orders, read by the guard and ExecutionManager
    "is_flatten",
    # the one place a saga becomes a venue order
    "place_order_of",
    # the cash line a venue snapshot implies — the genesis and the reconcile
    # anchor read it from one place
    "venue_cash",
    # Tier-2 view assembly (pure, never stored)
    "LiquidationSource",
    "SymbolValuation",
    "account_valuation",
    "account_view",
    "position_view",
    # the one seq counter for every signal source (ADR-0016)
    "SignalEmitter",
    # retry pacing
    "Backoff",
    "Deadline",
    # the one rule for a setting given in seconds
    "duration_ns",
    # seam Protocols — ``Exchange`` composes the two anchors beside it
    "AccountAnchor",
    "Clock",
    "EventBus",
    "Exchange",
    "Handler",
    "MarketFeed",
    "OrderAnchor",
    "Portfolio",
    "PreTradeGuard",
    "ReplayClock",
    "Store",
    "Strategy",
]
