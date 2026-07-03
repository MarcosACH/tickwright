"""Domain enumerations: order intent, time-in-force, and the saga state set.

Values are lowercase strings so they read cleanly when embedded in idempotency
keys (e.g. ``0xabc:submitted``) and structured logs. The ``OrderState`` set is
the full ADR-0007 FSM; this v1 tracer only exercises the happy-path subset
(``PENDING`` → ``SUBMITTED`` → ``FILLED``), but the vocabulary lands whole.
"""

from enum import StrEnum


class Side(StrEnum):
    """The side of an order intent."""

    BUY = "buy"
    SELL = "sell"


class AggressorSide(StrEnum):
    """The taker side of a trade tick (Hyperliquid ``trades.side``)."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """Order types the engine models in v1 (ADR-0012: the two fundamentals)."""

    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    """Time-in-force policies the engine models in v1 (ADR-0012)."""

    GTC = "gtc"
    IOC = "ioc"


class OrderState(StrEnum):
    """The order-lifecycle saga states (ADR-0007 / ADR-0010)."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    LIVE = "live"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    DENIED = "denied"
    REJECTED = "rejected"
    FAILED = "failed"
