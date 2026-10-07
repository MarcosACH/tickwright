"""PROTOTYPE, throwaway. Drive the flatten saga by hand.

Run: uv run python prototypes/flatten_saga/run.py

The model is in saga.py. This file only draws it and reads commands.
"""

import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import saga as m  # noqa: E402

B, D, R = "\x1b[1m", "\x1b[2m", "\x1b[0m"

NEXT = {
    m.Phase.START: "trip the kill switch and cancel all",
    m.Phase.RECONCILE: "reconcile the books with the venue",
    m.Phase.DECIDE: "place the next attempt, or finish, or give up",
    m.Phase.SEND: "send the stored order to the venue",
    m.Phase.WAIT: "nothing. the venue moves next: [f] or [e]",
    m.Phase.DONE: "nothing. flatten is done",
    m.Phase.GAVE_UP: "nothing. flatten gave up",
}

HELP = (
    "[s] step   [f] fill all   [f 0.2] fill 0.2   [e] end the IOC\n"
    "[x -0.3] hand trade at venue   [c] crash   [b] boot   [n] reset   [q] quit"
)


def render(w: m.World) -> None:
    print("\033[2J\033[H", end="")
    print(f"{B}VENUE{R}  position {B}{w.venue.position}{R}")
    for oid, o in w.venue.orders.items():
        side = "buy" if o.side > 0 else "sell"
        print(f"  {oid}  {side} {o.size}  filled {o.filled}  {o.status}")
    parts = w.store.partitions
    total = sum(parts.values())
    gap = w.venue.position - total
    print(f"\n{B}STORE{R}  {D}(survives a crash){R}")
    print(f"  kill switch  {B}{'TRIPPED' if w.store.kill_switch else 'clear'}{R}")
    print(f"  seq high     {w.store.seq_high}")
    for p, s in parts.items():
        print(f"  {p:<17}{s:>9}")
    flag = "" if not gap else f"  {B}gap {gap:+}{R}"
    print(f"  {D}{'sum':<17}{total:>9}{R}{flag}")
    for s in w.store.sagas:
        snap = ", ".join(f"{p} {v}" for p, v in s.snapshot.items())
        print(f"  saga {s.order_id}  size {s.size:+}  filled {s.filled}  {B}{s.state}{R}")
        print(f"    {D}snapshot: {snap}{R}")
    print(f"\n{B}RUN{R}  {D}(lost on a crash){R}")
    if w.run is None:
        print(f"  {B}DOWN{R}. [b] to boot a new flatten run")
    else:
        print(f"  phase  {B}{w.run.phase}{R}   dry attempts in a row  {w.run.dry_streak}")
        print(f"  {D}next [s]: {NEXT[w.run.phase]}{R}")
    print(f"\n{B}LOG{R}")
    for line in w.log:
        print(f"  {line}")
    print(f"\n{HELP}")


def qty(arg: str) -> Decimal | None:
    try:
        return Decimal(arg)
    except InvalidOperation:
        return None


def main() -> None:
    w = m.initial()
    while True:
        render(w)
        try:
            cmd, *args = (input("> ").strip() or "s").split()
        except EOFError:
            return
        arg = args[0] if args else ""
        if cmd == "q":
            return
        if cmd == "s":
            w = m.step(w)
        elif cmd == "f":
            w = m.venue_fill(w, qty(arg) if arg else None)
        elif cmd == "e":
            w = m.venue_end(w)
        elif cmd == "x" and qty(arg) is not None:
            w = m.hand_trade(w, qty(arg))
        elif cmd == "c":
            w = m.crash(w)
        elif cmd == "b":
            w = m.boot(w)
        elif cmd == "n":
            w = m.initial()


if __name__ == "__main__":
    main()
