"""The signing key reaches no log line (issue #23, ADR-0020/0021).

``AppConfig`` owns the inventory of its own secrets — ``secrets()`` — and the
CLI wires it into ``configure_logging`` at startup, so the Hyperliquid signing
key is scrubbed from every structured record whether it leaks through a field
value, an interpolated message, or an exception string. The default paper
path carries no secrets at all.
"""

from io import StringIO
from pathlib import Path

import structlog

from tickwright.app.config import AppConfig
from tickwright.observability.logging import configure_logging

# Anvil's account #0 — a publicly-known throwaway key, safe in a test file.
TEST_SIGNING_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _config(tmp_path: Path, **overrides: object) -> AppConfig:
    (tmp_path / "ticks.jsonl").touch()
    defaults: dict[str, object] = {
        "replay": {"path": tmp_path / "ticks.jsonl"},
        "sqlite": {"path": tmp_path / "saga.db"},
    }
    return AppConfig(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_the_signing_key_appears_in_no_log_line(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        hyperliquid={"signing_key": TEST_SIGNING_KEY, "symbols": ["BTC"], "testnet": True},
    )
    stream = StringIO()
    configure_logging(stream=stream, secrets=config.secrets())

    # The two leak vectors redaction must close: a sensitive-named field, and
    # the raw key interpolated into an arbitrary string (an exception message,
    # a URL, a repr) under an innocent name.
    log = structlog.get_logger()
    log.info("venue.connect", signing_key=TEST_SIGNING_KEY)
    log.info("venue.error", error=f"request failed for key {TEST_SIGNING_KEY}: refused")

    output = stream.getvalue()
    assert TEST_SIGNING_KEY not in output
    assert "[REDACTED]" in output


def test_the_paper_path_carries_no_secrets(tmp_path: Path) -> None:
    # The default stack needs no key: nothing to register, nothing to leak.
    assert _config(tmp_path).secrets() == ()
