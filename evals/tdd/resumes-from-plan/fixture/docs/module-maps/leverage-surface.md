# Module Map: Leverage surface (perps)

Scaled-down stand-in for a real map in this repo. Large on purpose: reading it
whole is the mistake the case is watching for, and a section is what a careful
reader takes instead.

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

## Modules

### Leverage (`src/domain/leverage.py`)

Holds `LeverageSetting` (leverage plus margin mode) and `InstrumentSpec` (the
venue-sourced half of the bound: `max_leverage` and `margin_maint`), and the one
shared `check_leverage` both exchange adapters call. Pure: no I/O, no venue
knowledge, no config parsing. Errors name every offending symbol at once rather
than one per restart.

### universe (`src/venues/hyperliquid/universe.py`)

Turns one `meta.universe` entry from the venue into an `InstrumentSpec`. This is
where the leverage cap enters the process. An entry whose cap cannot be read is
refused rather than defaulted — a silently defaulted cap of `1` looks like a
conservative choice and is actually a wrong one, because it rejects every
legitimate configured leverage on that symbol.

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

### paper exchange (`src/adapters/paper/exchange.py`)

Validates the completed leverage map at `start()` through the shared domain
check and then never writes it anywhere: paper has no venue to push a setting
to. Its account spec carries the configured genesis collateral and the account
label, which is what makes the projection's startup check possible at all.

### hyperliquid exchange (`src/venues/hyperliquid/exchange.py`)

Runs the same shared check at `start()`, behind the account-mode boot gate,
whose premise the margin model depends on. The boot-time leverage push and the
post-boot drift alert are separate slices and are not in this map.

### app (`src/app/config.py`, `src/app/build.py`)

`config.py` holds the pure config model and the env-reading skin over it; only
the CLI entrypoint may build the skin. `build.py` completes the sparse leverage
map over the strategy-declared symbol set and injects the one completed map into
both consumers — the only scope holding both inputs, since an exchange knows
nothing of strategies.

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
