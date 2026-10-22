"""The CLI entry: ``tickwright`` (or ``python -m tickwright.app``).

Reads ``AppSettings`` from the environment and ``.env``, wires logging, builds
the engine through the composition root, and runs the supervised lifecycle.
The process exit code is the ADR-0024 contract. 0 is a graceful stop (SIGINT or
SIGTERM). 1 is ``FAULTED``, the external supervisor's restart signal. 2 is a
boot refusal, which a restart would repeat, so the supervisor should stop.

``tickwright cancel-all`` and ``tickwright flatten`` use the ADR-0060 codes instead. 0 means done, 1
means it stopped partway, and 2 means it never booted.
"""

import argparse
import asyncio
import sys
from typing import Literal

from pydantic import ValidationError

from tickwright.observability.logging import configure_logging

from .build import StrategyLoadRefusal, UnsupportedExitRun, build_engine, build_exit_job
from .config import AppSettings


def main() -> int:
    parser = argparse.ArgumentParser(prog="tickwright")
    # No command runs the engine. ``cancel-all`` and ``flatten`` are one-shot exit
    # runs with the engine stopped (ADR-0052). An unknown command exits 2 here,
    # before anything is built, so a typo can never start a trading run.
    parser.add_argument("command", nargs="?", choices=["cancel-all", "flatten"])
    # An exit run refuses a store no engine has used, because that is almost
    # always the wrong directory. This flag says the store is new on purpose.
    parser.add_argument("--new-store", action="store_true")
    args = parser.parse_args()
    if args.command is None:
        if args.new_store:
            parser.error("--new-store only applies to cancel-all and flatten")
        return _run_engine()
    return _run_exit_job(args.command, new_store=args.new_store)


def _run_engine() -> int:
    # Everything comes from the environment/.env at runtime. This is the one
    # place that builds the env-reading skin (issue #71). A config that does not
    # validate, or a strategy entry that cannot load, fails the same way on every
    # restart. So it exits 2 and a supervisor stops retrying it (ADR-0024).
    try:
        config = AppSettings()
        configure_logging(secrets=config.secrets())
        engine = build_engine(config)
    except (ValidationError, StrategyLoadRefusal) as exc:
        print(f"tickwright: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(engine.run())


def _run_exit_job(command: Literal["cancel-all", "flatten"], *, new_store: bool) -> int:
    # A config the run cannot use exits 2 with its message and no event, because
    # logging needs a valid config (ADR-0060).
    try:
        config = AppSettings()
        configure_logging(secrets=config.secrets())
        engine = build_exit_job(config, command, new_store=new_store)
    except (ValidationError, UnsupportedExitRun) as exc:
        print(f"tickwright {command}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Building talks to no venue, so any failure here, such as a store that
        # cannot be opened, means nothing moved. That is code 2 (ADR-0060).
        print(f"tickwright {command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(engine.run())


if __name__ == "__main__":
    sys.exit(main())
