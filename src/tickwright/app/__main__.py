"""The CLI entry: ``tickwright`` (or ``python -m tickwright.app``).

Reads ``AppConfig`` from the environment and ``.env``, wires logging, builds
the engine through the composition root, and runs the supervised lifecycle.
The process exit code is the ADR-0024 contract: 0 = graceful stop (SIGINT/
SIGTERM), non-zero = ``FAULTED`` — the external supervisor's restart signal.
"""

import asyncio
import sys

from tickwright.observability.logging import configure_logging

from .build import build_engine
from .config import AppConfig


def main() -> int:
    # Everything comes from the environment/.env at runtime — a missing or
    # inconsistent field (feed=replay without a tick file, say) is a readable
    # pydantic validation error, which is exactly the CLI contract.
    config = AppConfig()
    configure_logging()
    engine = build_engine(config)
    return asyncio.run(engine.run())


if __name__ == "__main__":
    sys.exit(main())
