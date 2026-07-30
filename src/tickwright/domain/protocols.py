"""The seam Protocols — the swappable boundaries of the engine (ADR-0015).

These are structural contracts: an adapter satisfies a seam by shape, never by
inheritance, so ``domain`` stays a pure leaf that every impl compiles against
without importing anything back. Each Protocol is deliberately minimal for this
tracer slice; later slices widen them (``Exchange.cancel``/``fetch_*``,
``Clock`` timers, strategy snapshots) as their behaviors land.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from .account import Account, AccountSpec, AccountView
from .enums import OrderState
from .events import (
    Event,
    MarketTick,
    OrderEvent,
    PlaceOrder,
    PlaceSignal,
    VenueAccountState,
    VenueOrderView,
)
from .instrument import GuardDecision, InstrumentSpec, KillSwitchState
from .order import Order
from .position import Position, PositionView

type Handler[E: Event] = Callable[[E], Awaitable[None]]
"""An async subscriber of a single event family."""


@runtime_checkable
class EventBus(Protocol):
    """Publish/subscribe transport (ADR-0023). Pub/sub only — no query surface.

    Lifecycle rides the seam because the runner starts and stops the bus in
    its ordered sequence (ADR-0024): in-memory both verbs are no-ops; Kafka
    connects on ``start`` and flushes/commits on ``close``.
    """

    def subscribe[E: Event](self, event_type: type[E], handler: Handler[E]) -> None:
        """Register ``handler`` for every published event that is an ``event_type``."""
        ...

    async def start(self) -> None:
        """Connect the transport (ADR-0024 startup step 3). In-memory: no-op."""
        ...

    async def publish(self, event: Event) -> None:
        """Publish ``event``, draining the whole reentrant cascade to quiescence."""
        ...

    async def drain(self) -> None:
        """Return once every event published so far — including events handlers
        published while draining — has been delivered (ADR-0024's shutdown
        drain). In-memory: no-op, ``publish`` already drained; Kafka: wait for
        this process's produced records to be dispatched and committed.

        Safe on a second call, like the ``close`` below: a teardown step behind
        this one that raises faults the run, and the best-effort pass re-walks
        the whole membership from the top (ADR-0024)."""
        ...

    async def close(self) -> None:
        """Stop delivering and flush anything buffered; safe on a never-started
        bus, and safe on a second call — the faulted teardown re-walks the whole
        membership, this step included (ADR-0024)."""
        ...


@runtime_checkable
class Clock(Protocol):
    """The injected source of all time (ADR-0005). Canonical unit: UTC epoch ns."""

    def timestamp_ns(self) -> int:
        """The current time as UTC epoch nanoseconds."""
        ...

    def now(self) -> datetime:
        """The current time as a timezone-aware UTC ``datetime`` (human edges only)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds``; virtual and immediate under ``ManualClock``."""
        ...

    async def sleep_until(self, ts_ns: int) -> None:
        """Park until the clock crosses ``ts_ns`` — a pure waiter (ADR-0033).

        Never advances time itself: under ``ManualClock`` it wakes only when a
        producer (``advance_to``) drives virtual time across the target, so a
        periodic cadence paces off feed-driven virtual time without racing the
        feed backward or busy-spinning.
        """
        ...


@runtime_checkable
class ReplayClock(Clock, Protocol):
    """A ``Clock`` whose virtual time a deterministic producer drives forward.

    ``ReplayFeed`` advances the clock to each row's ``ts_event`` before publishing
    (ADR-0027). Declaring the capability here — not on the concrete ``ManualClock``
    — lets the feed depend on ``domain`` alone, keeping the no-adapter-imports-an-
    adapter boundary intact (ADR-0032). ``LiveClock`` implements only ``Clock``.
    """

    def advance_to(self, ts_ns: int) -> None:
        """Advance virtual time to ``ts_ns`` (never backward)."""
        ...


@runtime_checkable
class MarketFeed(Protocol):
    """Produces ``MarketTick`` events for configured symbols (ADR-0015)."""

    async def start(self) -> None:
        """Begin producing ticks. ``ReplayFeed`` runs to end-of-file."""
        ...

    async def stop(self) -> None:
        """Stop producing ticks.

        The first step of the runner's one teardown membership, so it inherits
        that membership's property (ADR-0024, and ``Engine._teardown_steps``):
        the faulted pass re-walks from the top, so a graceful step that raises
        behind this one drives it a second time. Idempotent; safe on a feed
        that never started."""
        ...


@runtime_checkable
class Strategy(Protocol):
    """Consumes ticks, reacts to lifecycle, emits ``Signal``s (ADR-0015/0016)."""

    strategy_id: str

    async def on_tick(self, tick: MarketTick) -> None:
        """Handle a market tick; may emit signals."""
        ...

    async def on_order_event(self, event: OrderEvent) -> None:
        """Handle a canonical saga transition for one of this strategy's orders."""
        ...

    def set_next_seq(self, next_seq: int) -> None:
        """Resume the strategy-owned monotonic ``seq`` at ``next_seq``.

        Called by the engine at startup with the saga store's high-water mark
        plus one (ADR-0016) — never derived from the snapshot, so a stale
        snapshot can never reuse a consumed ``signal_id``."""
        ...

    def snapshot(self) -> bytes:
        """This strategy's state *content* as opaque bytes (ADR-0016).

        The engine persists them per ``strategy_id`` — the strategy owns what
        they mean, never where they live. Keep state minimal and
        reconstructible; seq-safety never depends on it."""
        ...

    def restore(self, data: bytes) -> None:
        """Rebuild state content from bytes a prior ``snapshot()`` returned.

        Raising here is *not* fatal: the engine logs
        ``strategy.snapshot_incompatible`` and the strategy starts fresh
        (ADR-0016 — a strategy whose code changed shape between runs must not
        fault the engine)."""
        ...


@runtime_checkable
class Portfolio(Protocol):
    """The pull-state read seam a ``Strategy`` queries for its own economics
    (ADR-0041). Reads are **method calls, never a PnL subscription** (ADR-0004).

    Deliberately three synchronous methods returning frozen ``domain`` value
    snapshots: every field of one view comes from a single read of the
    projection, so two quantities in one view can never straddle a fill, and a
    synchronous read has no ``await`` point, so two reads inside one handler
    cannot either.

    The facade a strategy holds is **already scoped to its ``strategy_id``** by
    the composition root, which is why no method takes a strategy, venue,
    account or currency argument — the account is ambient (one per process,
    ADR-0038) and the reserved unattributed partition is structurally
    unreachable through it (ADR-0041 §5).

    This Protocol exists for **dependency direction**, not swappability: a
    ``domain`` strategy must compile against a ``domain`` seam rather than
    import ``engine``. There is one implementation by decision, and telemetry,
    the CLI and reconciliation read the ``engine`` concrete instead (ADR-0041 §8).
    """

    def position(self, symbol: str) -> PositionView | None:
        """This strategy's position in ``symbol``, or ``None`` if it never
        traded it. A **flat-with-history** record is not ``None``: it reads
        ``size = 0`` with its realized PnL retained (ADR-0041 §3)."""
        ...

    def open_positions(self) -> tuple[PositionView, ...]:
        """Every partition of this strategy still holding exposure — flat
        records are excluded, which is the whole distinction from
        ``position()`` returning one."""
        ...

    def account(self) -> AccountView:
        """The account-wide shared pool, **not** filtered to this strategy:
        collateral is one pool, and reporting a scoped slice of it would be a
        fiction (ADR-0041 §2)."""
        ...


@runtime_checkable
class Store(Protocol):
    """Durable saga checkpoints and the accounting ledger (ADR-0019, ADR-0043).
    The write the crash-safety argument rests on (ADR-0008).

    Deliberately synchronous: a checkpoint is one atomic step of a handler —
    ``apply`` then persist with no yield point in between — so no other
    handler can ever observe a saga whose memory and durable states disagree.
    Throughput is explicitly not a goal; readable recovery is.

    **Every member either reaches durable storage or raises
    ``InvariantViolation``** (ADR-0019) — one contract for the seam rather than
    one per method, and a backend's own exception type is never part of it. That
    widens ADR-0014, which names a failed checkpoint *write*; reads are covered
    too, so a store call can move under the containment net later without its
    failure mode changing shape. The
    type is load-bearing, not decorative: ``InvariantViolation`` is what pierces
    the engine's strategy-containment net and faults the run (ADR-0024), so a
    driver exception crossing here would be filed as a caller's bug and survived,
    while the process silently loses the ability to make its state durable.
    ``close()`` is the deliberate exception: teardown that fails is a leaked
    resource the runner already records, not a durability claim that proved false.

    ``checkpoint_ledger`` is the one method that knows about three aggregates at
    once, which the rest of this Protocol's one-method-per-aggregate shape does
    not. That coupling is the price of the atomicity guarantee (ADR-0043 §4),
    paid deliberately: an explicit ``transaction()`` scope on this seam was
    rejected because it cannot be built by wrapping the narrow methods, and
    ``sqlite3``'s connection context manager does not nest — an inner ``with``
    commits, so an outer failure would leave the inner writes durable on the
    default backend.
    """

    def checkpoint(self, order: Order, *, ts_ns: int) -> None:
        """Durably record ``order``'s full saga state as of ``ts_ns``."""
        ...

    def get_order(self, cloid: str) -> Order | None:
        """Rebuild the checkpointed saga for ``cloid``, or ``None`` if unknown."""
        ...

    def all_orders(self) -> list[Order]:
        """Rebuild every checkpointed saga — the recovery mass-read the ``Cache``
        projection is rebuilt from (ADR-0009)."""
        ...

    def save_strategy_snapshot(self, strategy_id: str, data: bytes, *, ts_ns: int) -> None:
        """Durably record ``strategy_id``'s opaque state bytes (ADR-0016).

        The engine persists what ``Strategy.snapshot()`` returned — content is
        the strategy's business, durability is ours. Latest snapshot wins; the
        same synchronous, no-yield discipline as ``checkpoint``."""
        ...

    def load_strategy_snapshot(self, strategy_id: str) -> bytes | None:
        """The last persisted snapshot for ``strategy_id``, or ``None`` if never
        saved — the read the engine restores from at startup (ADR-0016)."""
        ...

    def save_kill_switch(self, *, tripped: bool, reason: str | None, ts_ns: int) -> None:
        """Durably record the global kill-switch state (ADR-0026).

        Sticky by design: a tripped halt must outlive the process that set it,
        so the flag is persisted here and restored before the feed starts. The
        same synchronous, no-yield discipline as ``checkpoint``."""
        ...

    def load_kill_switch(self) -> "KillSwitchState | None":
        """The persisted kill-switch state, or ``None`` if never written — the
        read the guard restores from on startup (ADR-0026). ``None`` means never
        tripped; a restored ``tripped`` halt is cleared only by an explicit
        reset."""
        ...

    def checkpoint_ledger(
        self,
        *,
        account: Account,
        positions: Sequence[Position] = (),
        order: Order | None = None,
        funding_mark: tuple[str, int] | None = None,
        ts_ns: int,
    ) -> None:
        """Durably record the accounting ledger as of ``ts_ns`` (ADR-0043 §9).

        **One transaction across every aggregate handed to it.** A fill mutates
        the order row and the ledger together, and either ordering as two
        transactions is unsound — checkpoint-first loses the fill from the
        ledger on a crash, ledger-first double-counts it (ADR-0043 §4). On paper
        that never heals: the in-process venue holds no position state, so the
        store is the sole authority. A write the backend cannot make durable
        raises ``InvariantViolation`` rather than running on."""
        ...

    def all_positions(self) -> list[Position]:
        """Every persisted partition — the ledger's recovery mass-read.

        The reserved unattributed partition comes back **unfiltered**
        (ADR-0043 §9): ADR-0034's Σ-invariant — Σ per-strategy signed size =
        account net = venue size — holds by construction only if the partition
        is restored with everything else, so filtering belongs at the
        ``Portfolio`` seam and never in the store."""
        ...

    def has_orders(self) -> bool:
        """Whether any saga history exists at all (ADR-0043 §9).

        A member of its own rather than a reuse of ``all_orders()``: the startup
        refusal asks this **before** ``cache.rebuild()``, and answering an
        existence question with the mass-read would deserialize every saga in
        the store twice on every start, on the recovery path. The ``bool`` also
        keeps the check honest about what it is entitled to know — nothing about
        the orders themselves, which belong to the ``Cache``'s recovery."""
        ...

    def funding_mark(self, symbol: str) -> int | None:
        """The last funding boundary applied to ``symbol``, or ``None`` if none
        ever was (ADR-0043 §5.2).

        The ledger's one durable idempotency record, keyed by ``symbol``
        because that is the grain of the key it is half of: ADR-0037 dedupes on
        ``(account, symbol, boundary_ts)`` with the account ambient. ``None`` is
        row absence, and it admits any boundary — nothing has been applied to
        contradict it. The advance rides ``checkpoint_ledger`` rather than a
        write of its own, so it lands in the same transaction as the funding
        line it guards: **the only write that may advance the mark is the one
        that applies the accrual.**"""
        ...

    def load_account(self) -> Account | None:
        """The persisted account, or ``None`` if the ledger was never opened.

        ``None`` is load-bearing rather than defensive (ADR-0043 §9): it is a
        live first run, it is the paper seed-genesis case, and combined with a
        non-empty ``orders`` table it is the paper-path startup refusal."""
        ...

    def history(self, cloid: str) -> list[tuple[OrderState, int]]:
        """``cloid``'s durable transition trail: one ``(state, ts_ns)`` per
        checkpoint (ADR-0008's checkpoint points).

        Declared here despite no production caller, because the trail is what the
        engine's own tests assert a saga *did* — the observation port for
        "checkpointed once, at this state" — and a seam that disclaims its test
        surface is a seam a conforming implementation cannot actually be swapped
        into. Recovery still rebuilds from the current record alone; this is the
        audit half, and audit is a real audience, not an accident."""
        ...

    def close(self) -> None:
        """Release the store's underlying resources — the final step of the
        engine's reverse shutdown (ADR-0024). Idempotent; every checkpoint
        already committed durably, so closing loses nothing."""
        ...


@runtime_checkable
class Exchange(Protocol):
    """A thin venue boundary adapter (ADR-0015): translate and emit raw facts.

    Owns no saga. ``place`` emits ``ExecutionReport``s on the bus rather than
    returning them, so the ``ExecutionManager`` drives the FSM off venue facts.

    Lifecycle rides the seam because the runner drives it in its ordered
    sequence (ADR-0024): ``start`` at step 4, ``stop`` behind the feed and the
    reconcile cadences. The two are declared as a pair and read as one.
    """

    async def start(self) -> None:
        """Connect the adapter to its venue (ADR-0024 step 4).

        The *connect* half the runner's step 4 has always named, run after the
        bus is up and **before** the startup barrier — so any refusal raised
        here precedes every order, and the barrier observes an already-aligned
        venue. A refusal is an ``InvariantViolation``: the engine faults and
        exits non-zero rather than trading against a venue it could not align.

        Refusal and failure are not the same answer, and the split is the
        adapter's to make (ADR-0024 barrier-failure policy, extended by
        ADR-0044 §6 / ADR-0046 §3): a venue that *disagrees* with this config
        refuses immediately and is never retried, while transient boot-window
        venue I/O — a blip, a rate-limit — is retried with backoff inside the
        ``startup_reconciliation_timeout`` budget before it becomes a refusal.
        The runner does not retry this call; raising is how you spend the last
        of that budget.

        That budget is **the adapter's to enforce, and only the adapter's**.
        The runner neither retries this call nor bounds it — the timeout it
        holds is applied to the barrier one step later, and nothing wraps this
        one — so **``start()`` must not hang**, the peer of the "never hanging"
        clause on ``stop()`` below. The asymmetry is deliberate but sharp: a
        teardown that cannot finish is bounded and faults non-zero
        (``shutdown_timeout``), whereas a *boot* wedged here has no bound and
        no operator escape — the signal handlers are installed, but the task
        that watches them is only created once this sequence returns, so SIGINT
        sets a flag nobody awaits and SIGKILL is the only way out. An adapter
        that makes a blocking venue call here owns a timeout on it; the
        composition root must hand it the budget to size that timeout with,
        since ``EngineConfig`` does not reach the adapter.
        """
        ...

    async def stop(self) -> None:
        """Release whatever ``start()`` connected (ADR-0024 reverse shutdown).

        Driven once the feed is cut and the reconcile cadences are cancelled —
        so nothing is left to call ``fetch_order`` on a released adapter — and
        still ahead of the bus *drain*, because a loop of the adapter's own that
        outlived this call would keep publishing into that drain and keep
        raising its high-water mark, so the cascade never reaches quiescence.

        Ahead of the drain, though, is not ahead of the last caller: the drain
        dispatches the cascade it is waiting on, and the strategies are still
        subscribed behind it (they stop one step later, and stopping them only
        snapshots). So an in-flight tick can still reach a strategy, and its
        ``Signal`` still reaches the ``ExecutionManager``. **Leave ``place``
        and ``cancel`` answerable** — refusing cleanly on a released link, never
        hanging and never wedging the drain — until this call has returned *and*
        the drain behind it is done. Reachable on the Kafka bus, where dispatch
        runs in the poll loop; not on the in-memory bus, whose ``publish``
        already drained the cascade before the feed was cut.

        **Safe on an adapter that never started**, the peer of ``EventBus.close``
        above: the faulted teardown walks the same ordered membership as the
        graceful one, so a ``start()`` that *refused* — or that never ran at all,
        because an earlier step faulted first — is still followed by this call.
        Release what exists; never assume a successful ``start()`` preceded you.

        **Idempotent**, for the same reason read the other way: a graceful step
        that raises *behind* this one faults the run, and the best-effort pass
        re-walks the membership from the top — so the runner may drive this
        twice in one shutdown. The second call must be a no-op, not a failure.
        """
        ...

    async def place(self, order: PlaceOrder) -> None:
        """Place ``order`` at the venue; emit the resulting raw ``ExecutionReport``(s)."""
        ...

    async def cancel(self, cloid: str) -> None:
        """Cancel the order identified by ``cloid``; emit the resulting raw
        ``ExecutionReport``. A cancel of an unknown/already-gone order is a
        benign no-op (ADR-0026)."""
        ...

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        """Venue truth for ``cloid`` — the reconciler's query-shaped direct read
        (ADR-0004), never a bus message. Returns ``None`` only when the read
        itself failed (outage): a failed read must never look like "no record"
        (ADR-0011 inv 1). A successful read always returns a view, even an
        empty one."""
        ...

    async def fetch_account_state(self) -> VenueAccountState | None:
        """Venue truth for the account — the reconcile's account-grain pull, the
        exact peer of ``fetch_order`` one grain up (ADR-0004): a query-shaped
        direct read, never a bus message.

        ``None`` means **no venue truth to compare against**, never "flat"
        (ADR-0011 inv 1 in the return type). The reconcile freezes on it and
        heals nothing — anything else would let an unanswered read pass for an
        empty book and correct a restored ledger down to it, which is the
        fabricated flat ADR-0034 forbids.

        The two paths reach that ``None`` differently and the contract is the
        same either way. On live it is a **failed read** — an outage, a timeout,
        or a response the adapter cannot parse. On paper it is the **permanent
        answer**: that venue holds resting orders, fill reports and the latest
        tick, and no position, cash or equity state at all, so it has no account
        truth to report rather than none to report *yet*.
        """
        ...

    def account_spec(self) -> AccountSpec:
        """The venue's static declarations about the account this process trades
        (ADR-0038): its qualified identity, its netting semantics, and — on
        paper — the operator's declared genesis collateral.

        A synchronous accessor and the exact peer of ``instrument_specs()``: the
        adapter is the one component that knows the venue, and this is read once
        during composition, never on a hot path."""
        ...

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        """The per-symbol ``InstrumentSpec``s this venue owns (ADR-0031).

        The adapter is the one component that knows the venue (config on paper,
        the meta endpoint on Hyperliquid); it authors the specs and exposes them
        here so the ``Engine`` can wire them into the venue-agnostic guard at
        startup. A synchronous accessor, not a bus message — it is read once
        during composition, not a per-order hot path."""
        ...


@runtime_checkable
class PreTradeGuard(Protocol):
    """The thin pre-trade boundary the ``ExecutionManager`` runs before any send
    (ADR-0017): check → quantize → verdict. Not a RiskEngine — no positions,
    exposure, or portfolio risk (those are deferred).

    Two impls satisfy it: a ``RealGuard`` (min-notional, quantization, kill
    switch) and a ``NoopGuard`` passthrough. Users may plug their own — the
    Protocol-extensibility story.
    """

    def check(self, signal: PlaceSignal) -> GuardDecision:
        """Verdict on ``signal``: approve with quantized values, or ``DENIED``.

        Failure — a size that rounds to zero, a below-min-notional order, or a
        tripped kill switch — is ``DENIED`` (ADR-0010): never sent, safe to
        recreate. Approval carries the quantized ``quantity``/``price`` the order
        is actually placed at.

        The guard's specs are **mandatory**: a ``PlaceSignal`` for a symbol it has
        no ``InstrumentSpec`` for is a composition-root wiring bug (ADR-0031) and
        raises ``InvariantViolation`` (fail-fast, ADR-0014) — it cannot quantize
        without a spec, and must not send an unquantized order. This is the
        deliberate counterpart to ``Exchange``, whose specs are *optional* venue
        config (a missing one skips the venue-side min-notional check)."""
        ...

    def trip_kill_switch(self, reason: str) -> None:
        """Halt new placements globally and durably (ADR-0026). Every subsequent
        ``check`` returns ``DENIED``; resting ``LIVE`` orders are untouched. The
        halt is persisted, so it outlives a crash."""
        ...

    def reset_kill_switch(self) -> None:
        """Clear the halt and re-enable placement (ADR-0026). The only way a
        tripped kill switch is lifted — a crash never un-halts it."""
        ...

    @property
    def kill_switch_tripped(self) -> bool:
        """Whether the kill switch is currently tripped."""
        ...
