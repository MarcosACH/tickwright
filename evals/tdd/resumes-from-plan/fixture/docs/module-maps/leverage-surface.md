# Module Map: Perps margin surface

Scaled-down stand-in for a real map in this repo, and long on purpose. The whole
file is 18,224 characters (~4.5k tokens); its TOC is 1,351 and a module section
runs 275 to 2,356. Reading it whole therefore spends most of a cheap-planning
budget on one file, where the TOC plus the two sections this slice needs —
`Leverage` 849 and `universe` 719 — is 2,919, about a sixth of it. That contrast
is what the case is watching for.

The title names no module on purpose. `doc-slice` matches a heading substring and
falls back to the **first** match, so a title carrying "Leverage" would make
`doc-slice … Leverage` — the bare-name invocation the skill teaches — print this
whole file rather than the section, past every grader below.

## Source

Derived from the leverage PRD and the four ADRs governing the margin model. The
map is the architecture anchor; the ADRs are the decisions; this file fixes the
module boundaries the slices land against.

## Decisions this map fixes

- Leverage config is **venue-agnostic** and lives at the top level of the app
  config, a peer of the strategy list, never nested under a per-venue block. A
  live run must never read a paper block, and the model consuming leverage knows
  nothing about which venue it runs against.
- The configured map is **sparse**. The app layer completes it over the symbol
  set the configured strategies trade, defaulting each missing symbol to `1x`
  isolated, and injects the completed map into both consumers.
- An entry naming a symbol no configured strategy trades is **dead config** and
  is refused at load, not silently ignored.
- The bound `1 <= leverage <= InstrumentSpec.max_leverage` is checked on both
  the paper path and the live path, through one shared domain check. Leaving
  live's half to the venue would let paper compute against a leverage the venue
  rejects, surfacing only on promotion.
- `InstrumentSpec.max_leverage` defaults to `1`, not `0`: a `0` default makes
  the bound unsatisfiable for every symbol whose spec has not been fetched.
- Every accounting quantity is a `Decimal`. Floats are banned in the money path,
  and a comparison between two floats is banned outright — the rounding a float
  introduces is invisible until it accumulates across a day of fills and then
  disagrees with the venue's own statement by a few cents that nobody can trace.
- Margin is computed from the projection's own view, never from a venue read.
  The venue read is a reconciliation input, not a source of truth: a projection
  that asks the venue what its margin is cannot detect that the two disagree.

## Modules

### Position (`src/domain/position.py`)

The per-symbol leg: signed size, entry price, and the realised-PnL accumulator
that survives a flip. Immutable — every mutation returns a new `Position`, so a
projection can hold the previous one for a delta without defensive copying.

The load-bearing method is `apply_fill`, and its three cases are not symmetric.
An increase blends the entry price by notional weight. A decrease realises PnL
against the existing entry price and leaves that price untouched, because the
remaining size was opened at it. A flip is a decrease to zero followed by an
increase in the opposite direction: it realises the whole of the old leg's PnL
and then sets a fresh entry price from the residual, which is why it cannot be
expressed as a single blended update and gets its own branch.

Sizes carry the instrument's size decimals and prices its price decimals, both
quantised on construction rather than at use. Quantising late lets an unrounded
intermediate reach a comparison, and a size that is `0E-8` rather than `0` reads
as an open position to every `if position:` in the engine.

### Account (`src/domain/account.py`)

The account-grain aggregate: the cash line, the open positions, and the
identity (`AccountSpec`) that ties both to a venue and a network. `AccountSpec`
is what carries the configured genesis collateral into the startup check, and
its id is deliberately shaped per venue — `paper-<label>` is two segments
against live's `hyperliquid-<network>-<address>` three, so an id can never be
ambiguous between the two paths even in a log line stripped of context.

Equity is cash plus unrealised PnL across the open legs. Free margin is equity
minus initial margin held. Neither is stored: both are computed on read from the
cash line and the position set, because a stored derived quantity is one more
thing that can disagree with its inputs after a partial write.

### Leverage (`src/domain/leverage.py`)

Holds `LeverageSetting` (leverage plus margin mode) and `InstrumentSpec` (the
venue-sourced half of the bound: `max_leverage` and `margin_maint`), and the one
shared `check_leverage` both exchange adapters call. Pure: no I/O, no venue
knowledge, no config parsing. Errors name every offending symbol at once rather
than one per restart.

The margin mode is `isolated` or `cross`, and the distinction is not cosmetic:
under isolated margin a liquidation is bounded by the leg's own posted margin,
and under cross it can reach the whole account. The projection reads the mode
when it computes maintenance margin, so a mode that changed underneath a running
engine changes the liquidation price of a position already open — which is why
the drift alert exists as its own slice rather than as a silent re-read.

### Economics helpers (`src/domain/economics.py`)

The small pure functions the projection and both adapters share: notional from
size and price, initial margin from notional and leverage, maintenance margin
from notional and the instrument's maintenance fraction, and the liquidation
price for a leg given its margin mode.

They live here rather than as projection methods because the paper exchange
needs the same arithmetic at `start()` and at fill time, and a copy in the
adapter is a copy that drifts. The rule for adding one: it goes in this module
if it is a function of its arguments alone. Anything that needs the account's
current state belongs on the projection.

Rounding is explicit at every boundary — margin rounds **up** to the quote
increment and PnL rounds **half-even**. Rounding margin down would let an
account open a position it cannot quite hold, and the venue would reject it a
few milliseconds after the projection accepted it.

### Valuation (`src/domain/valuation.py`)

Turns a position and a mark into unrealised PnL, and an account and a mark map
into equity. Deliberately mark-driven rather than trade-driven: a trade print on
a thin book can move several percent and revert within a second, and an equity
line computed from prints would show that excursion as real, trip a risk check,
and flatten a position for no reason.

The mark is whatever the feed last pushed. The valuation module never fetches
one — it takes the map it is given, and raises rather than defaulting when a
symbol it is asked about is missing from it. A missing mark is a real condition
(the feed has not delivered yet, or has disconnected), and a zero default would
value an open position at nothing and report equity as cash.

### `domain` extended surfaces (`protocols.py`, `events.py`, `instrument.py`, `errors.py`)

`protocols.py` holds the structural types the engine is written against —
`Exchange`, `MarketFeed`, `Store`, `Clock` — and is the reason the engine
imports no concrete implementation. `events.py` holds the immutable event
records that cross the bus. `instrument.py` holds `InstrumentSpec` and the
decimal precision it carries. `errors.py` holds the typed exceptions this
surface raises: `LeverageOutOfBounds`, `StoreAccountMismatch`,
`VenueAccountModeUnsupported`.

Each error names every offending value at once rather than one per raise. An
error that reports one bad symbol per restart turns a config with four mistakes
in it into four restarts, and the operator fixing them cannot tell after the
first whether they are making progress.

### universe (`src/venues/hyperliquid/universe.py`)

Turns one `meta.universe` entry from the venue into an `InstrumentSpec`. This is
where the leverage cap enters the process. An entry whose cap cannot be read is
refused rather than defaulted — a silently defaulted cap of `1` looks like a
conservative choice and is actually a wrong one, because it rejects every
legitimate configured leverage on that symbol.

The parse is total: every field the spec needs is required, and a missing or
unparseable one raises with the symbol and the offending raw value in the
message. The venue adds fields to this payload without notice, so the parse
ignores what it does not know and refuses only what it needs and cannot read.

### PortfolioProjection (`src/engine/portfolio.py`)

The fat section. Owns the account-grain projection: positions, realised and
unrealised PnL, the cash ledger, and the leverage map it was injected with,
reachable for reads as `Engine.portfolio.leverage_for`.

Recovery is the load-bearing part. On a first start against an empty store the
projection seeds the account row from the exchange's account spec, and on every
start after that it restores the row rather than re-seeding it. The distinction
matters because the seeded values are configured values: seeding twice would let
a config edit silently rewrite history that the ledger has already been computed
against.

The startup check runs ahead of the cache rebuild and refuses a store whose
persisted account disagrees with the configured one, naming every disagreeing
field at once. It also refuses a store carrying order history with no ledger
behind it: fees that were never charged and funding that never existed must not
be backfilled as zeroes, and the cheap way to ask is a dedicated `has_orders`
query rather than a mass read of the order table.

Both conditions are gated on the declared-versus-ingested predicate — whether
the account spec carries a configured genesis at all. On a live venue there is
no configured genesis to disagree with, and a store predating the ledger is
legitimate: it heals from the venue on the next reconcile. The account id is
compared on both paths regardless.

Valuation reads flow through here too. Unrealised PnL is computed against the
last mark the projection saw, never against a trade print, so a thin book cannot
move the account equity by itself. The projection does not fetch marks; the feed
pushes them, which keeps the projection deterministic under replay.

The projection is also where the account-grain reconcile freezes when the venue
account mode cannot be verified. Freezing is the fail-closed choice: under an
unsupported account mode the clearinghouse reports only the collateral posted
into perps, so equity and free margin read an order of magnitude low with
nothing in the response indicating it.

Every mutation is idempotent on the event's id. The bus delivers at least once,
so the projection sees duplicates on every reconnect, and a projection that
applied a fill twice would double a position and halve the equity that funds it.

### LedgerReconciliation (`src/engine/ledger_reconcile.py`)

Compares the projection's derived cash line against the venue's reported one and
classifies the gap into tiers. Tier 0 is within tolerance and is logged and
dropped. Tier 1 is a cash-only difference explainable by a fee or a funding
accrual the projection has not ingested, and heals by writing the venue's figure
after re-reading the account mode. Tier 2 is a position-level disagreement and
does not heal: it freezes the account grain and alerts, because a projection
that silently adopts a position it cannot explain has stopped being a check on
anything.

The re-read before a Tier-1 heal is not defensive padding. The account mode can
change mid-session, and a heal computed under a mode that reports only perps
collateral would write an equity an order of magnitude low straight into the
ledger as truth.

### Engine extensions (`execution.py`, `cache.py`, `checkpoint.py`, `barrier.py`)

`execution.py` owns the order-lifecycle saga and its crash-safe transitions.
`cache.py` is the read-through projection cache the strategy host queries, and
it is rebuilt from the store at boot rather than persisted, so a corrupt cache
is never a recovery problem. `checkpoint.py` records the last durably applied
event so recovery is idempotent rather than replay-from-zero. `barrier.py` is
the start-up gate that holds the strategy host until recovery, reconciliation
and the leverage check have all completed.

The barrier is ordered, and the order is the argument: account mode, then the
leverage bound, then the reconcile. Each step's premise is the previous step's
conclusion, and running them concurrently to save a few hundred milliseconds at
boot buys a class of failure where a bound is validated against margin figures
read under an unverified account mode.

### paper exchange (`src/adapters/paper/exchange.py`)

Validates the completed leverage map at `start()` through the shared domain
check and then never writes it anywhere: paper has no venue to push a setting
to. Its account spec carries the configured genesis collateral and the account
label, which is what makes the projection's startup check possible at all.

Fills are deterministic: the fill model is injected, the clock is injected, and
nothing in this adapter reads the wall clock or the system random source. A
paper run replayed against the same feed produces the same ledger byte for byte,
which is what makes a paper divergence a real signal rather than noise.

### feed (`src/adapters/feed/replay.py`)

Reads a recorded tick file and pushes it onto the bus at the injected clock's
pace. It is the deterministic half of the feed seam and the one the tests use.
Marks and trade prints are separate event types on purpose, because the
valuation module consumes only the former.

### store (`src/adapters/store/`)

Two implementations behind one `Store` protocol: SQLite for the hermetic default
path and Postgres for the durable one. The contract tests run against both and
are the reason the pair is allowed to exist — two implementations of a seam is
the cap, and the second one earns its place by being the one that survives a
process restart on a real deployment.

The ledger schema fixes the genesis column as `NOT NULL` with no `CHECK`. The
constraint that matters is the startup comparison against the configured value,
not a range the database can express, and a `CHECK` would only be a second place
for the rule to live and drift.

### hyperliquid account (`src/venues/hyperliquid/account.py`)

Reads the clearinghouse state and turns it into the account-grain figures the
reconcile compares against. It also reads the account-abstraction mode, which
gates everything else this venue does.

Under a unified-account or portfolio-margin mode the clearinghouse reports only
the collateral posted into perps, and nothing in the response says so. So the
mode is read and verified at boot, and a mode that is unsupported *or
unreadable* refuses the start. Failing closed on unreadable is deliberate: the
difference between "the mode is fine" and "we could not ask" is exactly the
difference the response does not carry.

### hyperliquid preflight (`src/venues/hyperliquid/preflight.py`)

The boot-time checks that must pass before an order can be placed: the signing
key resolves to the configured account address, the account mode is supported,
the instrument specs are fetched, and the configured leverage sits inside every
fetched cap. Each check names what it read and what it expected, because a
preflight failure is read by an operator who cannot see the venue response.

### hyperliquid exchange (`src/venues/hyperliquid/exchange.py`)

Runs the same shared check at `start()`, behind the account-mode boot gate,
whose premise the margin model depends on. The boot-time leverage push and the
post-boot drift alert are separate slices and are not in this map.

Order placement is the other half. A placed order carries a client id derived
from the saga's own id, so a retry after a timeout is recognised by the venue as
the same order rather than accepted as a second one. That derivation is the only
reason a reconnect mid-place is survivable.

### funding (`src/venues/hyperliquid/funding.py`)

Ingests funding accruals as ledger entries against the account's cash line.
Funding is charged on a schedule the venue owns, so this module is a reader, not
a scheduler: it records what was charged, keyed on the venue's own accrual id so
a re-read cannot double-charge.

### strategies (`src/strategies/single_shot.py`)

The reference strategy: one order, one fill, one flat. It exists to be the
smallest thing that exercises the whole vertical, and it is what the engine
tests wire when they need a strategy that is not the subject of the test.

### app (`src/app/config.py`, `src/app/build.py`)

`config.py` holds the pure config model and the env-reading skin over it; only
the CLI entrypoint may build the skin. `build.py` completes the sparse leverage
map over the strategy-declared symbol set and injects the one completed map into
both consumers — the only scope holding both inputs, since an exchange knows
nothing of strategies.

The split between the pure model and the env skin is load-bearing rather than
stylistic. Anything that reads ambient config outside the entrypoint lets a
developer `.env` outrank a class default and wire a live venue into a path that
believes it is paper — including a test, which is why the suite builds the pure
model and never the skin.

### observability (`src/observability/catalog.py`)

The one place a log event name or a metric name is defined. It imports nothing
inward, so a rename here can never reach the money path, and the engine's
dependency contract enforces that direction rather than trusting it.

## Dependency graph

```
app  ->  domain  <-  venues
 |         ^
 v         |
engine ----+
```

`domain` depends on nothing. `venues` and `adapters` depend on `domain` only.
`app` wires everything and is depended on by nothing.

## Out of scope

The boot-time leverage push, the post-boot drift alert, and the funding
accrual schedule. Each has its own slice and its own map section elsewhere.
