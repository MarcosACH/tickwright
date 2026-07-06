"""Deterministic identity derivation (ADR-0006).

The ``cloid`` (exchange-facing client order id) is a pure function of the
strategy-owned ``signal_id`` — stable across restart and replay, never random —
so a recomputed signal after a crash resumes the same saga instead of placing a
second order. Hyperliquid's ``cloid`` is a 128-bit hex string, so we hash the
``signal_id`` to exactly 16 bytes and render it ``0x``-prefixed.
"""

import hashlib
from dataclasses import dataclass

_CLOID_BYTES = 16  # 128 bits, per Hyperliquid's cloid width.


@dataclass(frozen=True, slots=True)
class SignalId:
    """The structured form of a ``signal_id`` — the one owner of its format.

    A ``signal_id`` is the string ``{strategy_id}:{symbol}:{seq}`` (ADR-0006), and
    it is the correctness key the whole saga and dedup are keyed on. This value
    object owns *both* directions of that format: ``Signal.signal_id`` composes it
    with ``render``, and every reader (seq high-water recovery, ADR-0016) recovers
    the fields with ``parse``. Keeping compose and parse in one place is the point
    — the wire form and the recovery read can never drift.

    ``seq`` and ``symbol`` are colon-free; only ``strategy_id`` may itself contain
    a colon, so ``parse`` splits from the right (``rsplit``) and never a naive
    left-to-right split that would misattribute such a ``strategy_id``.
    """

    strategy_id: str
    symbol: str
    seq: int

    def render(self) -> str:
        """The wire form ``{strategy_id}:{symbol}:{seq}`` (ADR-0006)."""
        return f"{self.strategy_id}:{self.symbol}:{self.seq}"

    @classmethod
    def parse(cls, signal_id: str) -> "SignalId":
        """Recover the fields from a rendered ``signal_id``.

        Raises ``ValueError`` on a string that is not a well-formed ``signal_id``
        (too few fields, or a non-integer ``seq``) — a malformed id is a bug, not
        a value to tolerate.
        """
        strategy_id, symbol, seq = signal_id.rsplit(":", 2)
        return cls(strategy_id, symbol, int(seq))


def derive_cloid(signal_id: str) -> str:
    """Derive the deterministic 128-bit hex ``cloid`` for a ``signal_id``."""
    digest = hashlib.blake2b(signal_id.encode("utf-8"), digest_size=_CLOID_BYTES)
    return "0x" + digest.hexdigest()
