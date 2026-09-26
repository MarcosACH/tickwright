"""The pre-trade guard adapters (ADR-0017): ``RealGuard`` and ``NoopGuard``.

The ``PreTradeGuard`` Protocol lives in ``domain``; the two impls live here. The
guard is the thin pre-trade boundary the ``ExecutionManager`` runs before any
send — *not* a RiskEngine. ``RealGuard`` concentrates the quantization/min-notional
subtleties and the halt semantics here, behind a small interface, so they never
leak into the manager or each venue adapter. ``NoopGuard`` is the passthrough
twin (tests/paper) that keeps the seam real.

The kill switch is global, halt-only, and durable (ADR-0026): tripped, every new
``PlaceSignal`` is ``DENIED`` while resting ``LIVE`` orders are untouched. Its
state is persisted through the ``Store`` and restored on construction, so a halt
outlives a crash and is cleared only by an explicit reset.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Final

from tickwright.domain import (
    Approved,
    Clock,
    Denied,
    GuardDecision,
    InstrumentSpec,
    InvariantViolation,
    PlaceSignal,
    PreTradeReading,
    Side,
    Store,
    below_min_notional,
    duration_ns,
    quantize_price,
    quantize_size,
)
from tickwright.observability import NamedEvent, named_event


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolLimits:
    """One symbol's caps (ADR-0051). ``None`` means that cap is off."""

    max_order_size: Decimal | None = None
    """In coins, checked against the quantized quantity. An order that only
    reduces the position skips it."""
    max_order_value: Decimal | None = None
    """In USD, checked against the quantized quantity times a price. A buy limit
    uses its quantized limit price. A sell limit uses that price or the latest
    mark, whichever is higher. A market order uses the latest mark. An order that
    only reduces the position skips it and needs no mark."""
    max_position: Decimal | None = None
    """In coins, checked against the worst-case position on the order's side."""

    def __post_init__(self) -> None:
        # A cap of zero or less would deny every order. That is a typo, not a
        # policy, so it stops the boot instead (ADR-0051). Every field here is a
        # cap, so a new cap is checked without being listed. If a field that is
        # not a cap ever joins, go back to a named list.
        for cap_field in fields(self):
            cap = getattr(self, cap_field.name)
            if cap is not None and cap <= 0:
                raise ValueError(f"{cap_field.name} must be positive, got {cap}")


@dataclass(frozen=True, slots=True, kw_only=True)
class PreTradeLimits:
    """The pre-trade caps ``RealGuard`` enforces (ADR-0051). Empty means no limits.

    It lives in ``engine`` beside its only reader, so ``AppConfig`` has a typed
    target without the engine importing ``app``."""

    symbols: Mapping[str, SymbolLimits] = field(default_factory=dict)
    """A symbol with no entry has no per-symbol caps."""
    mark_max_age_seconds: float = 10.0
    """How old a mark may be and still value a market order or a sell limit
    against a max order value. Its own setting, not the reconcile band's, which
    does another job."""
    max_orders_per_window: int | None = None
    """How many placements the whole engine may approve inside one window."""
    window_seconds: float | None = None
    """The length of the rate cap's sliding window."""

    def __post_init__(self) -> None:
        # A bad age stops the boot, not the first market order.
        duration_ns(self.mark_max_age_seconds, name="mark_max_age_seconds")
        # Half a rate cap cannot be enforced, and silently dropping it would
        # leave the user thinking a cap is on.
        if (self.max_orders_per_window is None) != (self.window_seconds is None):
            raise ValueError("set both max_orders_per_window and window_seconds, or neither")


NO_LIMITS: Final = PreTradeLimits()
_NO_SYMBOL_LIMITS: Final = SymbolLimits()


class RealGuard:
    """The real pre-trade boundary: quantize, min-notional, caps, durable kill switch."""

    def __init__(
        self,
        *,
        specs: Mapping[str, InstrumentSpec],
        store: Store,
        clock: Clock,
        limits: PreTradeLimits = NO_LIMITS,
    ) -> None:
        self._specs = dict(specs)
        self._store = store
        self._clock = clock
        self._limits = limits
        self._mark_max_age_ns = duration_ns(
            limits.mark_max_age_seconds, name="mark_max_age_seconds"
        )
        # Restore the sticky halt before anything can place (ADR-0026): a tripped
        # engine comes back tripped. ``None`` means never tripped.
        restored = store.load_kill_switch()
        self._tripped = restored.tripped if restored is not None else False
        # The times of approved placements inside the rate cap's window. It
        # lives in memory, so a restart starts it empty (ADR-0051).
        self._approved_ns: deque[int] = deque()
        self._window_ns = None
        if limits.window_seconds is not None:
            self._window_ns = duration_ns(limits.window_seconds, name="window_seconds")

    @property
    def kill_switch_tripped(self) -> bool:
        return self._tripped

    def trip_kill_switch(self, reason: str) -> None:
        """Halt new placements globally and durably (ADR-0026)."""
        self._set_kill_switch(tripped=True, reason=reason)
        named_event(NamedEvent.GUARD_KILL_SWITCH_TRIPPED, reason=reason)

    def reset_kill_switch(self) -> None:
        """Clear the halt and re-enable placement (ADR-0026)."""
        self._set_kill_switch(tripped=False, reason=None)
        named_event(NamedEvent.GUARD_KILL_SWITCH_RESET)

    def _set_kill_switch(self, *, tripped: bool, reason: str | None) -> None:
        # Persist before flipping the in-memory flag: the durable record must
        # never lag the state readers act on, so a crash between the two can
        # only leave the store ahead, never behind (the fail-safe direction).
        self._store.save_kill_switch(
            tripped=tripped, reason=reason, ts_ns=self._clock.timestamp_ns()
        )
        self._tripped = tripped

    def check(self, signal: PlaceSignal, reading: PreTradeReading) -> GuardDecision:
        if self._tripped:
            # Halt-only: every new placement is DENIED while resting LIVE orders
            # are left untouched (ADR-0026). Checked first — a halt overrides all.
            return Denied(reason="kill switch tripped")
        spec = self._specs.get(signal.symbol)
        if spec is None:
            # A traded symbol with no spec is a composition-root wiring bug
            # (ADR-0031 sources a spec per symbol at startup): fail fast (ADR-0014)
            # rather than send an unquantized order the venue would mishandle.
            # Unlike the Exchange, whose specs are optional venue config, the
            # guard's are mandatory — it cannot quantize without one.
            raise InvariantViolation(
                f"no InstrumentSpec wired for symbol {signal.symbol!r}; the composition "
                "root must source a spec for every traded symbol (ADR-0031)"
            )
        quantity = quantize_size(signal.quantity, spec)
        if quantity <= 0:
            # A size that rounds to nothing is a phantom order (ADR-0017): never
            # sent, safe for the strategy to recreate at a valid size.
            return Denied(reason="size rounds to zero")
        # MARKET has no pre-trade price: only the venue knows the fill price, so
        # min-notional is adjudicated there (→ REJECTED), not here (ADR-0017).
        # It still falls through to the caps below (ADR-0051).
        price = None
        if signal.price is not None:
            price = quantize_price(signal.price, signal.side, spec)
            if below_min_notional(price, quantity, spec):
                # A LIMIT carries its own price, so notional is exact: deny locally
                # rather than emit an order the venue will reject (ADR-0017).
                return Denied(reason="below min notional")
        symbol_limits = self._limits.symbols.get(signal.symbol, _NO_SYMBOL_LIMITS)
        # The position if every open order on this side fills, and then this
        # one too (ADR-0051).
        direction = 1 if signal.side is Side.BUY else -1
        before = reading.account_net_size + direction * reading.open_remainder
        worst_case = before + direction * quantity
        # An order that shrinks the worst case without crossing zero cannot add
        # exposure, so no cap on order size or value may stop it. Ending at zero
        # is a full close, not a cross, for a long and a short alike.
        same_side = (worst_case > 0) == (before > 0)
        reduces = abs(worst_case) < abs(before) and (worst_case == 0 or same_side)
        cap = symbol_limits.max_order_size
        if cap is not None and quantity > cap and not reduces:
            return Denied(reason=f"above max order size {cap}")
        cap = symbol_limits.max_order_value
        if cap is not None and not reduces:
            value_price = price
            # A buy limit fills at its price or better, so its price is the value.
            # A market order has no price. A sell limit below the bid fills near
            # the bid on a real venue, so its own price can hide most of its
            # value (#391). Both need the mark (ADR-0051).
            if value_price is None or signal.side is Side.SELL:
                if reading.mark is None:
                    # With no mark the guard cannot prove the order fits.
                    return Denied(reason=f"no mark for max order value {cap}")
                # The guard's clock is the replay clock on replay, so a recorded
                # file is judged in its own time, not the wall clock's.
                age_ns = self._clock.timestamp_ns() - reading.mark.ts_event
                if age_ns > self._mark_max_age_ns:
                    return Denied(reason=f"stale mark for max order value {cap}")
                # A market order can fill worse than the mark, so this cap is
                # close for it, not exact.
                mark_price = reading.mark.price
                value_price = mark_price if value_price is None else max(value_price, mark_price)
            if quantity * value_price > cap:
                return Denied(reason=f"above max order value {cap}")
        cap = symbol_limits.max_position
        if cap is not None:
            # A reducing order always passes, so a user can shrink a position
            # that is already past the cap. An order that crosses zero opens a
            # new side, so it gets no such pass.
            if abs(worst_case) > cap and not reduces:
                return Denied(reason=f"above max position {cap}")
        limits = self._limits
        if limits.max_orders_per_window is not None and self._window_ns is not None:
            now_ns = self._clock.timestamp_ns()
            # A slot counts for exactly one window after its placement.
            while self._approved_ns and now_ns - self._approved_ns[0] >= self._window_ns:
                self._approved_ns.popleft()
            if len(self._approved_ns) >= limits.max_orders_per_window:
                return Denied(
                    reason=f"above max orders per window {limits.max_orders_per_window} "
                    f"in {limits.window_seconds}s"
                )
            self._approved_ns.append(now_ns)
        return Approved(quantity=quantity, price=price)


class NoopGuard:
    """A passthrough ``PreTradeGuard``: approve every signal unmodified.

    The default on the paper/test path — it makes the seam real without imposing
    a policy, so a suite can exercise the whole pipeline with the guard out of
    the way. The kill switch is inert (never tripped)."""

    @property
    def kill_switch_tripped(self) -> bool:
        return False

    def check(self, signal: PlaceSignal, reading: PreTradeReading) -> GuardDecision:
        return Approved(quantity=signal.quantity, price=signal.price)

    def trip_kill_switch(self, reason: str) -> None:
        """Inert: a passthrough guard never halts."""

    def reset_kill_switch(self) -> None:
        """Inert: nothing to reset."""
