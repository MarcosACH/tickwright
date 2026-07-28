"""Domain enumerations: order intent, time-in-force, and the saga state set.

Values are lowercase strings so they read cleanly when embedded in idempotency
keys (e.g. ``0xabc:submitted``) and structured logs. The ``OrderState`` set is
the full ADR-0007/0010 FSM, and ``Order.apply`` enforces its transition table.
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


class Netting(StrEnum):
    """How a venue nets two opposing fills on one instrument (ADR-0034/0038).

    ``NET`` — one signed position per symbol, which is what v1 models and what
    makes ``(strategy, symbol)`` disjointness sufficient; ``HEDGE`` — long and
    short legs held side by side, the named extension point. Venue-declared on
    ``AccountSpec``, never operator config.
    """

    NET = "net"
    HEDGE = "hedge"


class ComponentState(StrEnum):
    """The shared component lifecycle (ADR-0014): ``READY → RUNNING → STOPPED``,
    plus ``FAULTED`` for an unrecoverable invariant violation (fail-fast)."""

    READY = "ready"
    RUNNING = "running"
    STOPPED = "stopped"
    FAULTED = "faulted"


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
