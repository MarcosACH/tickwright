"""Instrument specs and the shared, pure quantizer (ADR-0017).

The minimal per-symbol metadata the pre-trade guard and the quantizer need —
``sz_decimals``, ``max_decimals``, optional ``max_sig_figs``, ``min_notional`` —
deliberately *not* a full instrument-provider component. Sourced from config on
the paper path and from the venue meta endpoint on Hyperliquid (ADR-0031), it is
the same shape either way, so the guard stays venue-agnostic.

The quantizer is one shared implementation of the sig-figs ∧ decimals rule
(ADR-0017), living in ``domain`` because both the ``engine`` guard and every
``venues`` adapter reuse it and neither may import the other (ADR-0032). Size
rounds **down** to ``sz_decimals`` (never exceed the strategy's intent); price
rounds toward the **passive side** (buy down, sell up).
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from .enums import Side


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentSpec:
    """Per-symbol venue metadata the guard and quantizer need (ADR-0017/0030).

    ``max_sig_figs`` is optional: absent, the price rule degenerates to a plain
    decimal-places grid (the paper config's common case); present, it adds the
    significant-figures cap (Hyperliquid has no fixed tick — a price is valid iff
    it has ≤ ``max_sig_figs`` sig figs *and* ≤ ``max_decimals − sz_decimals``
    decimal places, with integer prices always valid).
    """

    symbol: str
    sz_decimals: int
    max_decimals: int
    min_notional: Decimal
    max_sig_figs: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Approved:
    """The guard approved a ``PlaceSignal``, quantized to venue-valid values.

    ``quantity``/``price`` are what the order is actually placed at (``price`` is
    ``None`` for a MARKET). A distinct type from ``Denied`` so a caller narrows by
    ``isinstance`` and reads non-optional quantized values without a guard.
    """

    quantity: Decimal
    price: Decimal | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Denied:
    """The guard refused a ``PlaceSignal`` (``DENIED``, ADR-0010): never sent,
    safe to recreate. ``reason`` is the refusal cause for the ``OrderDenied``."""

    reason: str


type GuardDecision = Approved | Denied
"""A ``PreTradeGuard`` verdict (ADR-0017): quantized ``Approved`` or ``Denied``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class KillSwitchState:
    """The persisted global kill-switch state (ADR-0026).

    Durable and sticky: ``tripped`` outlives a crash and is cleared only by an
    explicit reset. ``reason`` is the operator's note from the trip; ``ts_ns`` is
    when the state was last written (UTC epoch ns).
    """

    tripped: bool
    reason: str | None
    ts_ns: int


def quantize_size(size: Decimal, spec: InstrumentSpec) -> Decimal:
    """Round ``size`` **down** to ``spec.sz_decimals`` decimal places (ADR-0017).

    Down, never nearest: quantization must never place more than the strategy
    intended. A size that rounds to zero is the guard's ``DENIED`` signal.
    """
    quantum = Decimal(1).scaleb(-spec.sz_decimals)
    return size.quantize(quantum, rounding=ROUND_DOWN)


def quantize_price(price: Decimal, side: Side, spec: InstrumentSpec) -> Decimal:
    """Round ``price`` to a venue-valid grid, toward the **passive side** (ADR-0017).

    Passive means a BUY rounds **down** and a SELL rounds **up**, so quantization
    never makes an order more aggressive than intended. The grid is the sig-figs
    ∧ decimals rule: a price is valid iff it has ≤ ``max_sig_figs`` significant
    figures *and* ≤ ``max_decimals − sz_decimals`` decimal places, with integer
    prices always valid. Both caps reduce to a maximum number of decimal places
    for a price of this magnitude; we round to the coarser (smaller) of the two.
    """
    decimals = _allowed_decimals(price, spec)
    quantum = Decimal(1).scaleb(-decimals)
    rounding = ROUND_DOWN if side is Side.BUY else ROUND_UP
    return price.quantize(quantum, rounding=rounding)


def below_min_notional(price: Decimal, quantity: Decimal, spec: InstrumentSpec) -> bool:
    """True if ``price × quantity`` falls **below** ``spec.min_notional`` (ADR-0017).

    The one home for the min-notional boundary rule — a peer of the quantizers,
    shared by the pre-trade guard (a LIMIT, before send → ``DENIED``) and every
    ``Exchange`` adapter (a MARKET, at the venue-known fill price → ``REJECTED``),
    so the two halves of the pre-trade/at-fill split can never disagree on what
    "min notional" means. Strictly below: a notional exactly equal to
    ``min_notional`` clears.
    """
    return price * quantity < spec.min_notional


def _allowed_decimals(price: Decimal, spec: InstrumentSpec) -> int:
    """The maximum decimal places a valid price of ``price``'s magnitude may keep.

    The decimals cap is fixed (``max_decimals − sz_decimals``). The sig-figs cap
    depends on magnitude: keeping ≤ ``max_sig_figs`` significant figures allows
    ``(max_sig_figs − 1) − adjusted`` decimal places, where ``adjusted`` is the
    exponent of the most significant digit. Clamped at 0 — never negative — so an
    integer price is always valid regardless of its significant-figure count.
    """
    decimals = spec.max_decimals - spec.sz_decimals
    if spec.max_sig_figs is not None and price != 0:
        by_sig_figs = (spec.max_sig_figs - 1) - price.adjusted()
        decimals = min(decimals, by_sig_figs)
    return max(0, decimals)
