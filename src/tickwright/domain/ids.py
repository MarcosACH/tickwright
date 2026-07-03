"""Deterministic identity derivation (ADR-0006).

The ``cloid`` (exchange-facing client order id) is a pure function of the
strategy-owned ``signal_id`` — stable across restart and replay, never random —
so a recomputed signal after a crash resumes the same saga instead of placing a
second order. Hyperliquid's ``cloid`` is a 128-bit hex string, so we hash the
``signal_id`` to exactly 16 bytes and render it ``0x``-prefixed.
"""

import hashlib

_CLOID_BYTES = 16  # 128 bits, per Hyperliquid's cloid width.


def derive_cloid(signal_id: str) -> str:
    """Derive the deterministic 128-bit hex ``cloid`` for a ``signal_id``."""
    digest = hashlib.blake2b(signal_id.encode("utf-8"), digest_size=_CLOID_BYTES)
    return "0x" + digest.hexdigest()
