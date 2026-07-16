"""The live testnet suite's opt-in gate (issue #73).

Splits the run-gate away from ``TICKWRIGHT_HYPERLIQUID__SIGNING_KEY`` — an
``AppConfig`` field source (ADR-0030) — so CI's hermeticity guard can widen
past ``tests/app`` without the dummy hostile-env key enrolling the live suite.
The gate now keys on a dedicated ``TICKWRIGHT_LIVE_TESTNET`` opt-in flag that
maps onto no config field.
"""

from live_gate import LIVE_TESTNET_ENV, live_testnet_enabled

SIGNING_KEY_ENV = "TICKWRIGHT_HYPERLIQUID__SIGNING_KEY"


def test_gate_is_off_without_the_opt_in_flag_even_with_a_signing_key() -> None:
    # The exact CI hostile env: a valid dummy signing key, no opt-in flag.
    # This must read as "skip", or widening the guard re-enrols the live suite.
    env = {SIGNING_KEY_ENV: "0xdeadbeef"}

    assert live_testnet_enabled(env) is False
    assert LIVE_TESTNET_ENV not in env


def test_gate_is_on_when_the_opt_in_flag_is_set() -> None:
    assert live_testnet_enabled({LIVE_TESTNET_ENV: "1"}) is True


def test_gate_is_off_when_the_opt_in_flag_is_set_to_a_falsy_value() -> None:
    # A dev who exports TICKWRIGHT_LIVE_TESTNET=0 to *disable* the suite must
    # not have that read as an opt-in; presence alone would surprise them.
    for falsy in ("", "0", "false", "no", "off"):
        assert live_testnet_enabled({LIVE_TESTNET_ENV: falsy}) is False, falsy
