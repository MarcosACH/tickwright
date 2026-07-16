"""The opt-in gate for the ``live`` testnet suite (issue #73).

A dedicated run-gate, deliberately keyed on a name that maps onto **no**
``AppConfig`` field: ``TICKWRIGHT_HYPERLIQUID__SIGNING_KEY`` is a config field
source (ADR-0030), so gating collection on its presence made a valid dummy key
— exactly what CI's hermeticity guard exports — read as "run the live suite".
Keying on ``TICKWRIGHT_LIVE_TESTNET`` instead lets that guard widen past
``tests/app`` while the live suite (ADR-0022) stays out of the CI gate.

The live suite still reads the signing key for the key itself; it just no
longer decides collection from it.
"""

import os
from collections.abc import Mapping

LIVE_TESTNET_ENV = "TICKWRIGHT_LIVE_TESTNET"

_FALSY = frozenset({"", "0", "false", "no", "off"})


def live_testnet_enabled(env: Mapping[str, str] = os.environ) -> bool:
    """Whether the opt-in flag enrols the live testnet suite for collection.

    A falsy value (``0``/``false``/``no``/``off``, empty) reads as off, so a
    developer who exports the flag to *disable* the suite is not surprised into
    an opt-in; anything else present is a truthy opt-in.
    """
    return env.get(LIVE_TESTNET_ENV, "").strip().lower() not in _FALSY
