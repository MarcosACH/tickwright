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
    from tickwright.app.cli import main as run

    try:
        return run()
    finally:
        # Still waiting means the run ended before it could write the trip, so
        # the next start would trade. The operator has to hear that. A lost
        # SIGUSR2 is not reported, because it leaves the switch as it was.
        if signal.SIGUSR1 in signal.sigpending():
            print(
                "tickwright: a SIGUSR1 arrived before the engine could record it. "
                "The kill switch is not tripped.",
                file=sys.stderr,
            )


if __name__ == "__main__":
    sys.exit(main())
