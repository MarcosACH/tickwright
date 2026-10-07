# How the paper exchange handles reduce-only orders

A reduce-only order may only shrink a position. Paper must treat one the way Hyperliquid does, or a
strategy behaves one way on paper and another on real money. The testnet probes give the venue
rules ([#416](https://github.com/MarcosACH/tickwright/issues/416), P1 to P3 and P7). This ADR
decides how paper copies them. Decided in
[#414](https://github.com/MarcosACH/tickwright/issues/414), part of
[#408](https://github.com/MarcosACH/tickwright/issues/408).

## Decision

- **Paper checks against the account net.** Hyperliquid holds one net position per symbol. It
  knows nothing about strategy partitions. So paper reads the account net, not the partition of
  the strategy that sent the order. Strategy A long 1 and strategy B short 1 make a flat account.
  A reduce-only sell from A is then rejected, on paper and on the venue.
  **(Corrected by ADR-0058:** two strategies cannot trade one symbol (ADR-0038). The other
  partition in a symbol is the unattributed one. Strategy A long 1 and an unattributed short 1 make
  the flat account here. The rule does not change.**)**
- **At placement, paper copies the venue table.**

  | Account net vs order | Paper and Hyperliquid |
  | --- | --- |
  | Opposite side, at least the order size | Accepted as sent |
  | Opposite side, smaller than the order | Shrunk to the net size |
  | Flat, or same side as the order | `REJECTED`: "reduce-only order would increase position" |

  The same rules hold for market, IOC, and GTC orders.
- **A shrunk order ends with `CANCELLED` for the cut part.** The saga keeps the size the strategy
  asked for. It reaches `FILLED` only when fills add up to that size, and a `FILLED` status report
  moves no saga. So after the fills, paper sends `CANCELLED` with the reason "reduce-only shrink".
  The saga goes `PARTIALLY_FILLED → CANCELLED`, a move it already allows. The Hyperliquid adapter
  must end a shrunk order the same way. The venue answers `filled` there (P1), so without this the
  saga stays open forever. That adapter change ships in the same slice.
- **After every fill, paper re-checks its resting reduce-only orders in that symbol.** If the net
  is smaller than an order's working size, the order shrinks to the net. If the net is zero or on
  the order's side, paper cancels the order with the reason "reduce-only cancelled". This copies
  P2, where the venue cancels in the same message as the closing fill. On paper, only paper's own
  fills move the net, so there is no other moment to check.
- **Each resting order is capped on its own.** Paper does not split the net between them. The
  probes did not test this on the venue. No outcome depends on it, because the first order to close
  the position cancels the rest.
- **A shrunk order never grows back.** If the position grows again, the working size stays where it
  was shrunk to. The probes did not test this on the venue either.
- **Paper reads the store net plus its own fills the store has not applied yet.** Paper publishes a
  fill from inside a bus handler. Both buses queue it, and the store moves only after that handler
  returns. Without the list, two reduce-only sells of 1 against a long 1 both fill, and the account
  ends short 1. Paper reads the applied fills and the net with no await between them. So no store
  write can land between the two reads. A fill counts only while its order row lacks
  `{cloid}:fill:{trade_id}`. The order row and the position move in one transaction (ADR-0043
  §4), so no fill is counted twice. Paper drops a fill from the list once the store shows it
  applied. The list is empty when the engine is idle. The store stays the only authority for the
  position (ADR-0043 §4).
- **A reduce-only order skips the minimum notional.** Hyperliquid applies its $10 minimum to opening
  orders only (P7). A plain order keeps today's check, even when it would close a position. That is
  stricter than the venue, so paper never accepts an order the venue would refuse. A strategy that
  wants to close dust sends it reduce-only.
  **(Corrected by ADR-0058:** P7 only tested an order that closes the whole position. The P9
  probes show Hyperliquid rejects any order under $10 that leaves part of the position open,
  reduce-only or not, IOC or GTC. It accepts an order that covers the whole position, and a
  reduce-only order larger than the position. So paper skips the minimum for a reduce-only order
  only when its size is at least the account net. Any other reduce-only order under $10 is
  rejected. A plain order keeps today's check. Decided in
  [#435](https://github.com/MarcosACH/tickwright/issues/435).**)**

## Considered options

- **Reject an oversized order.** Simpler to read, but Hyperliquid shrinks it (P1). A strategy tested
  on paper would then meet different behavior on real money.
- **Check against the strategy's partition.** It reads more naturally to a strategy author. But the
  venue never sees partitions, so paper would accept orders the venue rejects.
- **Leave a shrunk order open.** No new status is needed. But the saga never ends, so the order
  counts as working forever.
- **Re-check resting orders on every tick.** Nothing moves the net between fills on paper, so this
  is work with no effect.
- **Paper keeps its own net, seeded from the store at start.** No list of in-flight fills is needed.
  But it is a second copy of the position, the drift ADR-0043 §4 rules out.
- **Drop a fill from the list when paper sees the saga's fill event.** The store applies the fill
  before that event is published. The in-memory bus can deliver a new order in that gap. Paper would
  then count the fill twice, once in the store and once on the list.

## Consequences

- Paper needs a read of the store's applied fills. The composition root injects it, the way it
  injects `account_net` today.
- The reasons "reduce-only shrink" and "reduce-only cancelled" live on paper's status report only.
  The saga's `OrderCancelled` has no reason field, so a strategy cannot tell these cancels from any
  other. Giving `OrderCancelled` a reason is a separate decision.
- A reduce-only order can still flip one strategy's partition. With A long 1 and B long 1, a
  reduce-only sell of 2 from A is accepted, and A ends short 1. The "never flip" promise holds for
  the account net only. Whether the guard stops this belongs to the question below.
  **(Corrected by ADR-0058:** two strategies cannot trade one symbol (ADR-0038), so the A and B
  example cannot happen. The flip is still possible against the unattributed partition. The guard
  now denies a strategy's reduce-only order that would flip its own position.**)**
- How the pre-trade guard treats a reduce-only order is still open in
  [#408](https://github.com/MarcosACH/tickwright/issues/408). That includes the guard's own minimum
  check for limit orders.
  **(Resolved by ADR-0058**, decided in
  [#435](https://github.com/MarcosACH/tickwright/issues/435).**)**
- The `reduce_only` field on `PlaceSignal` lands with the #408 PRD.
