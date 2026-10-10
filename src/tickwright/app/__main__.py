"""The CLI entry: ``tickwright`` (or ``python -m tickwright.app``).

Reads ``AppSettings`` from the environment and ``.env``, wires logging, builds
the engine through the composition root, and runs the supervised lifecycle.
The process exit code is the ADR-0024 contract: 0 = graceful stop (SIGINT/
SIGTERM), non-zero = ``FAULTED`` — the external supervisor's restart signal.

``tickwright cancel-all`` uses the ADR-0060 codes instead. 0 means done, 1
means it stopped partway, and 2 means it never booted.
"""

import argparse
import asyncio
import sys

from pydantic import ValidationError

from tickwright.observability.logging import configure_logging

from .build import UnsupportedExitRun, build_engine, build_exit_job
from .config import AppSettings


def main() -> int:
    parser = argparse.ArgumentParser(prog="tickwright")
    # No command runs the engine. ``cancel-all`` is a one-shot exit run with
    # the engine stopped (ADR-0052). An unknown command exits 2 here, before
    # anything is built, so a typo can never start a trading run.
    parser.add_argument("command", nargs="?", choices=["cancel-all"])
    command = parser.parse_args().command
    if command is None:
        return _run_engine()
    return _run_cancel_all()


def _run_engine() -> int:
    # Everything comes from the environment/.env at runtime — a missing or
    # inconsistent field (feed=replay without a tick file, say) is a readable
    # pydantic validation error, which is exactly the CLI contract. This is the
    # one place that builds the env-reading skin (issue #71).
    config = AppSettings()
    configure_logging(secrets=config.secrets())
    engine = build_engine(config)
    return asyncio.run(engine.run())


def _run_cancel_all() -> int:
    # A config the run cannot use exits 2 with its message and no event, because
    # logging needs a valid config (ADR-0060).
    try:
        config = AppSettings()
        configure_logging(secrets=config.secrets())
        engine = build_exit_job(config)
    except (ValidationError, UnsupportedExitRun) as exc:
        print(f"tickwright cancel-all: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(engine.run())


if __name__ == "__main__":
    sys.exit(main())
