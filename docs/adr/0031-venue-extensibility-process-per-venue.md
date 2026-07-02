# Venue extensibility: one venue per process, self-contained adapters, symbol-scoped identity

The scalability goal is that Tickwright **accepts N exchanges as decoupled, additive modules** —
each new venue its own module, none coupled to another — **without shipping N in v1** (the "two
implementations per seam" discipline caps what *ships*, never what the architecture *admits*). This
ADR fixes how a new exchange is added. Validated against the established adapter model
(self-contained per-venue packages, a data-client / execution-client split per venue) and the
ports-and-adapters style.

## One live venue per process (not one engine multiplexing venues)

An `Engine` hosts **exactly one live `Exchange`** (as ADR-0015's single-`Exchange` /
single-`ExecutionManager` topology already implies). Scaling to N exchanges is **N processes**, one
per venue — adding Kraken means deploying a second Tickwright process configured for Kraken, not
teaching one engine to route across venues.

**Rejected: one engine multiplexing N venues** (an execution router dispatching by venue, the
established execution-engine-routes-by-venue model). It would require venue-qualified identity
everywhere, per-venue reconciliation loops inside one process, and venue-aware `cloid` derivation — a
large surface that contradicts ADR-0001 (one process, one topology). The cost we accept: **no single
strategy trading across venues in one process** (cross-venue arbitrage is out of scope), which
matches the README's "engine core, not a multi-venue platform" framing.

## Identity stays symbol-scoped; venue-qualification is a documented deferral

Because one process = one venue, **venue is a deployment fact**, not an event field. Instrument
identity therefore stays a bare `symbol: str`: `signal_id = {strategy_id}:{symbol}:{seq}` (ADR-0006)
and `partition_key → symbol` (ADR-0025) are unchanged, and a symbol is unambiguous within a process.

**Standing caveat** (mirroring ADR-0003's account-scope caveat): if the process-per-venue model is
ever replaced by one engine multiplexing venues, instrument identity must gain a venue component
(`{venue}:{symbol}`, an instrument id qualified by venue) and **must not be forced onto the
bare-symbol ordering/dedup key.** `partition_key` is already a *property* (ADR-0025) precisely so
this can change without touching the bus.

## The `Exchange` adapter is the self-contained per-venue module

Each real venue is **one self-contained adapter** that fully encapsulates everything venue-specific:

- **order-model translation** — the engine's clean model (MARKET/LIMIT × GTC/IOC × `post_only`) into
  venue actions (ADR-0030's Hyperliquid MARKET→aggressive-IOC, asset indexing, TIF mapping);
- **reconciliation queries** — `fetch_*` open-orders / fill-history with the `None`-not-`[]`
  connectivity guard (ADR-0011);
- **instrument-spec sourcing** — see below;
- its own **`*Config`** (ADR-0021).

Adding a venue is therefore **one adapter module + its config + a process** — no change to the
engine, the saga, or any other adapter. The `Exchange` seam **accepts N adapters**; the two that
ship (`PaperExchange`, `HyperliquidExchange`) only *prove* the seam (the ADR-0018/0001 framing, now
stated for `Exchange` too). **No separate `InstrumentProvider` seam** is introduced — over-built for
the three fields the guard/quantizer need (ADR-0017). A venue's feed and exchange are packaged
together because they share venue knowledge; packaging is fixed by ADR-0032.

## Instrument specs are adapter-sourced, `Exchange`-exposed, `Engine`-wired

The `InstrumentSpec`s (`sz_decimals` / `max_decimals` / `max_sig_figs` / `min_notional` —
ADR-0017) are **authored by the adapter** (the
component that talks to the venue meta endpoint / holds the paper config), **exposed via the
`Exchange` Protocol** (an `instrument_specs()` accessor), and **wired into the
`PreTradeGuard`/quantizer by the `Engine` at startup** — the startup sequence already connects the
`Exchange` before starting the feed (ADR-0024 steps 4→7), a natural point to pull specs and hand them
to the guard. The guard/quantizer stay **venue-agnostic**: they receive specs, never knowing which
endpoint produced them.

**Rejected:** the composition root loading specs itself from config/meta — it leaks venue-specific
sourcing (which endpoint, which schema) out of the adapter into core wiring, breaking the
self-containment above.

## Consequences

- "Add an exchange" = add a self-contained adapter package (ADR-0032), a `*Config`, one
  composition-root arm (ADR-0032), and deploy a process. No core edit.
- The `PaperExchange` path is unaffected: it sources specs from config and has no venue endpoint.
- Multi-venue-in-one-process and a cross-venue strategy remain explicitly deferred, re-openable only
  by revisiting the process-per-venue decision and the identity caveat above.
