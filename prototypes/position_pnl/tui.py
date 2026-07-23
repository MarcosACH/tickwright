"""PROTOTYPE TUI — drive the position/PnL state model by hand (ticket #119).

Throwaway shell over ``model.py``. Run it, push fills through the hard cases,
watch the account anchor, the per-strategy overlay, the size invariant, and the
realized/unrealized split all update together.

    uv run python prototypes/position_pnl/tui.py

Nothing here is production code — the pure reducer in ``model.py`` is the only
part worth keeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from model import (  # type: ignore[import-not-found]  # noqa: E402  (script-dir import; throwaway)
    Fill,
    Position,
    account_positions,
    pnl_reclassification,
    size_invariant,
    strategy_positions,
)

from tickwright.domain.enums import Side

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
OFF = "\x1b[0m"


def money(d: Decimal) -> str:
    return f"{RED if d < 0 else GREEN}{d:+}{OFF}" if d else f"{d:+}"


@dataclass
class World:
    """In-memory prototype state: the fill log, marks, and the netting mode."""

    fills: list[Fill] = field(default_factory=list)
    marks: dict[str, Decimal] = field(default_factory=dict)
    mode: str = "disjoint"  # "disjoint" (NET, enforce) | "shared" (HEDGE illustration)
    note: str = ""

    def owner_of(self, symbol: str) -> str | None:
        for f in self.fills:
            if f.symbol == symbol:
                return f.strategy_id
        return None

    def add(self, fill: Fill) -> None:
        if self.mode == "disjoint":
            owner = self.owner_of(fill.symbol)
            if owner is not None and owner != fill.strategy_id:
                self.note = (
                    f"{RED}REJECTED{OFF} (NET disjointness): {fill.symbol} is owned by "
                    f"'{owner}'. On a one-way venue a second strategy on the same symbol "
                    f"needs a separate account (ADR-0034)."
                )
                return
        self.fills.append(fill)
        self.note = f"applied {fill.side.value} {fill.quantity} {fill.symbol} @ {fill.price}"


# --- Scenarios: the edge cases the ticket names --------------------------------

A, B = "momentum", "meanrev"


def _f(strat: str, sym: str, side: Side, qty: str, px: str) -> Fill:
    return Fill(strat, sym, side, Decimal(qty), Decimal(px))


SCENARIOS: dict[str, tuple[str, list[Fill], dict[str, Decimal]]] = {
    "1": (
        "reduce (long +3@100, sell 1@110 -> +2@100, realized +10)",
        [_f(A, "BTC", Side.BUY, "3", "100"), _f(A, "BTC", Side.SELL, "1", "110")],
        {"BTC": Decimal("115")},
    ),
    "2": (
        "full close (long +2@100, sell 2@120 -> flat, realized +40, entry reset)",
        [_f(A, "BTC", Side.BUY, "2", "100"), _f(A, "BTC", Side.SELL, "2", "120")],
        {"BTC": Decimal("120")},
    ),
    "3": (
        "flip through zero (long +2@100, sell 5@120 -> short -3@120, realized +40)",
        [_f(A, "BTC", Side.BUY, "2", "100"), _f(A, "BTC", Side.SELL, "5", "120")],
        {"BTC": Decimal("118")},
    ),
    "4": (
        "add to position (buy 2@100, buy 3@110 -> +5@106 weighted avg)",
        [_f(A, "BTC", Side.BUY, "2", "100"), _f(A, "BTC", Side.BUY, "3", "110")],
        {"BTC": Decimal("112")},
    ),
    "5": (
        "short-side profit (sell 2@100, buy 1@90 -> -1@100, realized +10)",
        [_f(A, "BTC", Side.SELL, "2", "100"), _f(A, "BTC", Side.BUY, "1", "90")],
        {"BTC": Decimal("95")},
    ),
    "6": (
        "disjoint collapse (momentum/BTC, meanrev/ETH -> per-strategy == per-symbol)",
        [
            _f(A, "BTC", Side.BUY, "2", "100"),
            _f(A, "BTC", Side.SELL, "1", "110"),
            _f(B, "ETH", Side.SELL, "4", "50"),
            _f(B, "ETH", Side.BUY, "1", "45"),
        ],
        {"BTC": Decimal("112"), "ETH": Decimal("48")},
    ),
    "7": (
        "SHARED-SYMBOL reclassification (both trade BTC — needs 'shared' mode)",
        [_f(A, "BTC", Side.BUY, "1", "100"), _f(B, "BTC", Side.SELL, "1", "110")],
        {"BTC": Decimal("110")},
    ),
}


# --- Rendering -----------------------------------------------------------------


def pos_line(label: str, p: Position, mark: Decimal | None) -> str:
    if p.is_flat:
        body = f"{DIM}flat{OFF}  realized={money(p.realized)}"
    else:
        side = "LONG" if p.net_qty > 0 else "SHORT"
        body = f"{side} {abs(p.net_qty)} @ {p.avg_entry}  realized={money(p.realized)}"
        if mark is not None:
            body += f"  uPnL@{mark}={money(p.unrealized(mark))}"
    return f"  {BOLD}{label:<22}{OFF} {body}"


def render(w: World) -> None:
    print("\033[2J\033[H", end="")
    mode_c = CYAN if w.mode == "disjoint" else YELLOW
    print(f"{BOLD}position & PnL state model{OFF}  {DIM}prototype #119 / ADR-0034{OFF}")
    print(
        f"mode: {mode_c}{w.mode}{OFF}  "
        f"{DIM}(disjoint=NET, one strategy per symbol; shared=HEDGE illustration){OFF}"
    )
    print(
        f"fills: {len(w.fills)}   marks: "
        + (", ".join(f"{k}={v}" for k, v in w.marks.items()) or "—")
    )
    print("─" * 78)

    acct = account_positions(w.fills)
    strat = strategy_positions(w.fills)

    if not acct:
        print(f"  {DIM}(no fills yet — press [s] for a scenario or [f] to add one){OFF}")
    for symbol in sorted(acct):
        mark = w.marks.get(symbol)
        print(f"{CYAN}▸ {symbol}{OFF}  {DIM}(account anchor = venue szi/entryPx/realized){OFF}")
        print(pos_line("ACCOUNT (anchor)", acct[symbol], mark))
        for sid, sym in sorted(k for k in strat if k[1] == symbol):
            print(pos_line(f"  strat:{sid}", strat[(sid, sym)], mark))

    print("─" * 78)
    print(f"{BOLD}size invariant{OFF}  {DIM}Σ(per-strategy size) must == account net size{OFF}")
    for symbol, (net, ssum, ok) in size_invariant(w.fills).items():
        badge = f"{GREEN}✓{OFF}" if ok else f"{RED}✗ BROKEN{OFF}"
        print(f"  {symbol}: account={net}  Σstrat={ssum}  {badge}")

    print(f"{BOLD}realized/unrealized split{OFF}  {DIM}account vs Σ per-strategy{OFF}")
    for symbol, r in pnl_reclassification(w.fills, w.marks).items():
        rmatch = f"{GREEN}exact{OFF}" if r["realized_match"] else f"{YELLOW}RECLASSIFIED{OFF}"
        umatch = f"{GREEN}exact{OFF}" if r["unreal_match"] else f"{YELLOW}RECLASSIFIED{OFF}"
        tag = f" {YELLOW}(shared symbol){OFF}" if r["shared"] else ""
        print(
            f"  {symbol}{tag}: realized acct={money(r['acct_realized'])} "
            f"Σstrat={money(r['strat_realized'])} [{rmatch}]"
        )
        print(
            f"       {DIM}uPnL{OFF} acct={money(r['acct_unreal'])} "
            f"Σstrat={money(r['strat_unreal'])} [{umatch}]"
        )

    print("─" * 78)
    if w.note:
        print(f"  {w.note}")
    print(
        f"{DIM}[f] fill  [m] mark  [s] scenario  [t] toggle mode  [u] undo  "
        f"[r] reset  [q] quit{OFF}"
    )


# --- Input handlers ------------------------------------------------------------


def ask(prompt: str) -> str:
    return input(f"  {prompt}").strip()


def do_fill(w: World) -> None:
    try:
        sid = ask("strategy id: ") or A
        sym = (ask("symbol: ") or "BTC").upper()
        side = Side.SELL if ask("side [b/s]: ").lower().startswith("s") else Side.BUY
        qty = Decimal(ask("qty: "))
        px = Decimal(ask("price: "))
    except (InvalidOperation, ValueError):
        w.note = f"{RED}bad number — fill discarded{OFF}"
        return
    w.add(Fill(sid, sym, side, qty, px))


def do_mark(w: World) -> None:
    try:
        sym = (ask("symbol: ") or "BTC").upper()
        w.marks[sym] = Decimal(ask("mark price: "))
        w.note = f"mark {sym} = {w.marks[sym]}"
    except (InvalidOperation, ValueError):
        w.note = f"{RED}bad mark price{OFF}"


def do_scenario(w: World) -> None:
    for key, (desc, _, _) in SCENARIOS.items():
        print(f"    {BOLD}{key}{OFF}  {desc}")
    choice = ask("scenario #: ")
    if choice not in SCENARIOS:
        w.note = "unknown scenario"
        return
    desc, fills, marks = SCENARIOS[choice]
    w.fills, w.marks, w.note = [], dict(marks), f"loaded: {desc}"
    for f in fills:
        w.add(f)  # replays through disjointness enforcement
    if choice == "7" and w.mode == "disjoint":
        w.note += f"  {YELLOW}(switch to 'shared' mode with [t] to see the reclassification){OFF}"


def main() -> None:
    w = World()
    while True:
        render(w)
        try:
            key = ask("> ").lower()
        except (EOFError, KeyboardInterrupt):
            break
        if key == "q":
            break
        elif key == "f":
            do_fill(w)
        elif key == "m":
            do_mark(w)
        elif key == "s":
            do_scenario(w)
        elif key == "t":
            w.mode = "shared" if w.mode == "disjoint" else "disjoint"
            w.note = f"mode -> {w.mode}"
        elif key == "u" and w.fills:
            w.fills.pop()
            w.note = "undid last fill"
        elif key == "r":
            w = World()
    print("\033[2J\033[H", end="")


if __name__ == "__main__":
    main()
