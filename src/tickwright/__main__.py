"""The ``tickwright`` command (or ``python -m tickwright``).

This module imports almost nothing, so it runs within milliseconds of the
process start. The engine itself takes seconds to import and build.
"""

import signal
import sys


def main() -> int:
    # The engine handles SIGUSR1 and SIGUSR2 only once ``Engine.run()`` installs
    # its handlers. Before that, the default action ends the process and the
    # operator's kill switch request is lost (issue #615). A blocked signal waits
    # in the kernel instead, and the engine acts on it once the store is open.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1, signal.SIGUSR2})

    # Imported here, after the block, because this import is the slow part.
    from tickwright.app.__main__ import main as run

    return run()


if __name__ == "__main__":
    sys.exit(main())
