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

from collections.abc import Mapping

from tickwright.domain import (
    Approved,
    Clock,
    Denied,
    GuardDecision,
    InstrumentSpec,
    PlaceSignal,
    Store,
    below_min_notional,
    quantize_price,
    quantize_size,
)
from tickwright.observability import named_event


class RealGuard:
    """The real pre-trade boundary: quantize, min-notional, durable kill switch."""

    def __init__(self, *, specs: Mapping[str, InstrumentSpec], store: Store, clock: Clock) -> None:
        self._specs = dict(specs)
        self._store = store
        self._clock = clock
        # Restore the sticky halt before anything can place (ADR-0026): a tripped
        # engine comes back tripped. ``None`` means never tripped.
        restored = store.load_kill_switch()
        self._tripped = restored.tripped if restored is not None else False

    @property
    def kill_switch_tripped(self) -> bool:
        return self._tripped

    def trip_kill_switch(self, reason: str) -> None:
        """Halt new placements globally and durably (ADR-0026)."""
        self._set_kill_switch(tripped=True, reason=reason)
        named_event("guard.kill_switch_tripped", reason=reason)

    def reset_kill_switch(self) -> None:
        """Clear the halt and re-enable placement (ADR-0026)."""
        self._set_kill_switch(tripped=False, reason=None)
        named_event("guard.kill_switch_reset")

    def _set_kill_switch(self, *, tripped: bool, reason: str | None) -> None:
        # Persist before flipping the in-memory flag: the durable record must
        # never lag the state readers act on, so a crash between the two can
        # only leave the store ahead, never behind (the fail-safe direction).
        self._store.save_kill_switch(
            tripped=tripped, reason=reason, ts_ns=self._clock.timestamp_ns()
        )
        self._tripped = tripped

    def check(self, signal: PlaceSignal) -> GuardDecision:
        if self._tripped:
            # Halt-only: every new placement is DENIED while resting LIVE orders
            # are left untouched (ADR-0026). Checked first — a halt overrides all.
            return Denied(reason="kill switch tripped")
        spec = self._specs[signal.symbol]
        quantity = quantize_size(signal.quantity, spec)
        if quantity <= 0:
            # A size that rounds to nothing is a phantom order (ADR-0017): never
            # sent, safe for the strategy to recreate at a valid size.
            return Denied(reason="size rounds to zero")
        if signal.price is None:
            # MARKET has no pre-trade price: only the venue knows the fill price,
            # so min-notional is adjudicated there (→ REJECTED), not here (ADR-0017).
            return Approved(quantity=quantity, price=None)
        price = quantize_price(signal.price, signal.side, spec)
        if below_min_notional(price, quantity, spec):
            # A LIMIT carries its own price, so notional is exact: deny locally
            # rather than emit an order the venue will reject (ADR-0017).
            return Denied(reason="below min notional")
        return Approved(quantity=quantity, price=price)


class NoopGuard:
    """A passthrough ``PreTradeGuard``: approve every signal unmodified.

    The default on the paper/test path — it makes the seam real without imposing
    a policy, so a suite can exercise the whole pipeline with the guard out of
    the way. The kill switch is inert (never tripped)."""

    @property
    def kill_switch_tripped(self) -> bool:
        return False

    def check(self, signal: PlaceSignal) -> GuardDecision:
        return Approved(quantity=signal.quantity, price=signal.price)

    def trip_kill_switch(self, reason: str) -> None:
        """Inert: a passthrough guard never halts."""

    def reset_kill_switch(self) -> None:
        """Inert: nothing to reset."""
