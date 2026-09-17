"""``HyperliquidExchange`` — the live ``Exchange``: signed placement over
async HTTP.

A thin venue boundary (ADR-0015): translate and send, own no saga. Every
venue quirk stays here, never in the engine (ADR-0030): MARKET becomes an
aggressive IOC limit at ``latest × (1 ± slippage_bound)`` quantized per the
ADR-0017 price rule, ``post_only`` becomes ALO, LIMIT passes through as
GTC/IOC. Signing borrows the SDK's utilities only (ADR-0021) — the HTTP call
is our own async client, and the nonce comes from the injected ``Clock``
(ADR-0005), never the SDK's wall-time helper. The latest price a MARKET is
bounded against is the tick stream's — the adapter subscribes itself, like
every consumer of market data.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, assert_never

from tickwright.domain import (
    EMPTY_LEVERAGE_BOOK,
    AccountModeVerdict,
    AccountSpec,
    Clock,
    Deadline,
    EventBus,
    FillReport,
    InstrumentSpec,
    LeverageBook,
    MarketTick,
    OrderRef,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueAccountState,
    VenueOrderView,
    VenueReadFailure,
    quantize_price,
)
from tickwright.observability import NamedEvent, named_event

from . import transport
from .account import account_spec, normalize_account_state
from .config import HyperliquidConfig
from .funding import FundingIngest
from .preflight import push_leverage, reverify_account_mode, verify_account_mode
from .reading import figure, read, refuse_non_usdc
from .transport import Connect, PostJson, open_websocket
from .universe import HyperliquidUniverse

_NS_PER_MS = 1_000_000

# How far before our own ack time the fill-history read starts once the venue
# has dropped the order record. Our ack time is our clock, and the venue's fill
# time is theirs. The allowance covers the skew between the two (ADR-0011 inv 2).
# The same allowance decides whether a record read by cloid was placed before
# this saga was created, for the same reason (#354).
_ACK_SKEW_ALLOWANCE_MS = 60_000

_TIF_WIRE = {TimeInForce.GTC: "Gtc", TimeInForce.IOC: "Ioc"}


class HyperliquidExchange:
    """The live ``Exchange`` adapter for Hyperliquid perps."""

    def __init__(
        self,
        *,
        config: HyperliquidConfig,
        bus: EventBus,
        clock: Clock,
        universe: HyperliquidUniverse,
        startup_timeout_seconds: float,
        leverage: LeverageBook = EMPTY_LEVERAGE_BOOK,
        post: PostJson | None = None,
        connect: Connect = open_websocket,
    ) -> None:
        if config.signing_key is None:
            raise ValueError(
                "HyperliquidExchange needs a signing key: set TICKWRIGHT_HYPERLIQUID__SIGNING_KEY"
            )
        # Imported here, not at module top: key material and the SDK's signing
        # stack load only when a live exchange is actually built.
        from eth_account import Account

        self._config = config
        self._bus = bus
        self._clock = clock
        self._universe = universe
        # Late-bound default, exactly as ``fetch_instrument_specs`` resolves its
        # own: on the composition root's arm nothing injects this seam, so a
        # def-time-bound default argument would capture the real client and stay
        # captured — leaving the one HTTP boundary of the built adapter, and so
        # everything ``start()`` reads through it, unreachable from a test.
        self._post = post if post is not None else transport.post_json
        # ADR-0024's barrier budget, handed down rather than minted again
        # (ADR-0044 §6): the boot guards run *before* the barrier, so they
        # cannot be barrier steps, but a boot-time venue read they bound
        # separately would be a second timeout free to disagree with the first.
        self._startup_timeout_seconds = startup_timeout_seconds
        # The resolved per-symbol map, complete over the strategy-traded set and
        # the *same* one the margin model holds (ADR-0044 §2) — the composition
        # root resolves it once because an ``Exchange`` knows nothing of
        # strategies and could not resolve it for itself.
        self._leverage = leverage
        self._wallet = Account.from_key(config.signing_key.get_secret_value())
        # /info queries ask about the account, which is the key's own address
        # unless the key is an API/agent wallet acting for a master account.
        self._user_address = config.account_address or self._wallet.address
        # Read once at composition, like the instrument specs beside it: the
        # account is a deployment fact, not something that moves at runtime.
        self._account_spec = account_spec(config, address=self._user_address)
        self._latest_price: dict[str, Decimal] = {}
        # The last nonce sent: the venue requires per-address nonces to be
        # strictly increasing, and the ms-truncated clock would collide on two
        # sends inside one millisecond — this floor keeps them monotonic.
        self._last_nonce = 0
        # The MARKET slippage bound needs the latest traded price, and the tick
        # stream is where prices live (ADR-0027) — subscribe like any consumer.
        bus.subscribe(MarketTick, self.on_tick)
        # The live half of ADR-0037: the venue pays funding at its own
        # boundaries and this ingests it. Built here rather than in ``run()`` so
        # that ``stop()`` has something to close even if the runner never got as
        # far as starting the loop.
        self._funding = FundingIngest(
            config=config,
            bus=bus,
            clock=clock,
            account_id=self._account_spec.account_id,
            address=self._user_address,
            connect=connect,
        )

    async def start(self) -> None:
        """Align the venue, in order, then open the funding socket.

        Placement is request-scoped HTTP and the tick subscription is wired at
        construction, so the one thing to connect here is ``userFundings``.

        The ``userAbstraction`` mode gate is first and gates everything after it
        (ADR-0046 §3, which opens ADR-0024 step 4): a wrong mode invalidates the
        premise the leverage push's own check reasons from, so reporting
        mismatches computed against a margin model that does not apply would be
        noise on top of an error. The leverage push (ADR-0044 §7) lands behind
        it. Both refusals precede the barrier, so neither can let an order out.

        The three share **one** deadline, opened here and spent between them
        (ADR-0044 §6): they run in the same boot window against the same venue,
        so a budget each would let a boot the operator sized at one
        ``startup_reconciliation_timeout`` take three before the barrier gets
        its own. Whatever the gate spends retrying is gone from what the push
        and the funding connect have left.
        """
        deadline = Deadline.opening(clock=self._clock, budget_seconds=self._startup_timeout_seconds)
        await verify_account_mode(
            info=self._info,
            address=self._user_address,
            clock=self._clock,
            deadline=deadline,
        )
        # The same ``domain`` check paper runs, against specs this adapter
        # sourced from the meta endpoint rather than from config (ADR-0044 §9).
        # Behind the mode gate on purpose: under a pooled mode the margin model
        # this bound protects does not apply, so complaining about a leverage
        # would be noise on top of an error.
        self._leverage.validate_against(self._universe.specs)
        # Ahead of the push on purpose: §6 classifies the venue's own
        # ``"Invalid leverage value"`` as a config bug §9 should already have
        # caught, so the bound clears before anything reaches the venue.
        await push_leverage(
            info=self._info,
            send=self._send_action,
            address=self._user_address,
            book=self._leverage,
            asset_indices=self._universe.asset_indices,
            clock=self._clock,
            deadline=deadline,
        )
        # Last, once the venue is aligned. Inside ``run()`` a refused connect
        # would be paced and retried forever behind a ``RUNNING`` engine,
        # ingesting nothing (#300). Here it is retried on the same deadline as
        # the two guards above, then faults the boot.
        await self._funding.start(deadline=deadline)

    async def run(self) -> None:
        """Ingest the venue's funding payments for as long as the run lives.

        The half of ADR-0037 that is live's: the venue pays at its own
        boundaries and this reads them off `userFundings`, where paper has
        nobody to ask and generates the same keyed event off its `Clock`. Both
        reach the projection as one `FundingAccrual` on one bus, so the apply
        path carries no `if live:`.

        Supervised in the runner's `TaskGroup` for the fault channel, exactly as
        paper's generator is: this loop's failure mode is a payment the engine
        cannot represent (`VenueFactUnsupported`), and an ingest the adapter had
        spawned for itself would die of that alone — the engine running on
        `RUNNING`, accruing nothing for the rest of the process, which is this
        ADR's whole economic line failing silently. Supervised, it faults the run
        at the payment it happened on and exits non-zero.
        """
        await self._funding.run()

    async def stop(self) -> None:
        """Close the funding socket, which is what ends `run()`.

        Everything else this adapter does is scoped to the call that made it:
        placement, cancellation and every read are request-scoped HTTP, so the
        subscription is the one thing it holds open.
        """
        await self._funding.stop()

    async def on_tick(self, tick: MarketTick) -> None:
        self._latest_price[tick.symbol] = tick.price

    async def place(self, order: PlaceOrder) -> None:
        action = {
            "type": "order",
            "orders": [self._order_wire(order)],
            "grouping": "na",
        }
        # The signed action goes through the same read as every query: ``read``
        # guards the send and the pure parse, and nothing else. What sits above
        # this line is our own construction (an unknown symbol, an unsigned
        # action), where a raise is a bug in this process and keeps faulting.
        adjudication = await read(
            request="place",
            query=action,
            send=self._send_action,
            normalize=_placement_adjudication,
            cloid=order.cloid,
        )
        if isinstance(adjudication, VenueReadFailure):
            # Either way it failed, we hold no fact worth reporting: a dead send
            # leaves the order's truth unknown, and an unreadable adjudication
            # is a body we cannot read (inv 1). ``read`` named it; reconcile-by-
            # cloid resolves the in-flight order (ADR-0008 rule 2).
            return
        await self._apply_placement(order, adjudication)

    async def _apply_placement(self, order: PlaceOrder, adjudication: "_Adjudication") -> None:
        """Carry out an adjudication that has already been read (ADR-0015): the
        raw venue facts onto the bus, and on a fill the follow-up fills read.

        Outside the unreadable-body guard **by construction**, and that is the
        whole reason it is a separate step. ``publish`` dispatches subscribers
        inline and re-raises (ADR-0023), so a guard spanning this would span the
        entire cascade a report sets off — the saga, the checkpoint, the
        portfolio fold — and every member of ``UNREADABLE`` is a shape an engine
        bug takes too. Catching one here would file a bug in this process as a
        venue that answered badly, and leave the engine running against
        ``runner.py``'s guarantee that a raw handler's failure faults it.

        The same split ``read`` makes for the same reason (ADR-0048 §4): what
        the guard covers is a *pure* reading of the body, and nothing else.
        """
        match adjudication:
            case _ActionError(message=message):
                # The venue refused the whole action (bad nonce/signature/
                # rate-limit) — the order never entered the book. Name it,
                # emitting no terminal: a transient refusal must leave the order
                # resendable, and reconcile-by-cloid resolves it (ADR-0008
                # rule 2).
                self._action_rejected("place", order.cloid, message)
            case _Resting(oid=oid):
                await self._ack(order, oid=oid)
            case _Rejected(reason=reason):
                # Venue-adjudicated refusal: REJECTED, never DENIED (ADR-0010) —
                # the order was sent and judged, and the venue's reason rides along.
                await self._bus.publish(
                    self._status_report(
                        cloid=order.cloid,
                        symbol=order.symbol,
                        status=OrderState.REJECTED,
                        reason=reason,
                    )
                )
            case _Filled(oid=oid):
                # The placement response carries no trade ids, and a synthetic one
                # would double-count against reconciliation's venue-tid fills under
                # {cloid}:fill:{tid} dedup — so fetch the venue's own fill records
                # and emit those. This is the read right after placement: the fills
                # are the newest, so the whole-history read cannot miss them.
                # A `filled` answer is an ack, the same as `resting`: it carries
                # the venue's oid. The saga keeps the oid from the LIVE ack, and
                # that oid is the only key the fill history answers to once the
                # venue drops the order record (ADR-0011 inv 2). So the ack goes
                # out before the fills read. A read that fails then still leaves
                # the oid on the saga for reconcile to heal by (#328).
                await self._ack(order, oid=oid)
                # A failed fills read is already named as a *fills* read, never
                # as this placement: the placement succeeded, and calling it a
                # place failure would point triage at the wrong request. Emit
                # nothing more and let reconcile's fetch_order re-read the fills
                # and heal them (ADR-0011). Which way it failed changes nothing
                # here — there is no worklist behind this read to spare
                # (ADR-0049) — but it is checked for explicitly rather than
                # falsily, since a ``VenueReadFailure`` is truthy and would
                # otherwise be iterated.
                fills = await self._fetch_fills(cloid=order.cloid, symbol=order.symbol, oid=oid)
                if isinstance(fills, VenueReadFailure):
                    return
                for fill in fills:
                    await self._bus.publish(fill)
            case unreachable:
                # An adjudication the reader can produce and this cannot carry
                # out would otherwise be a silent no-op — the exact shape of the
                # defect ADR-0048 §4 catalogues. Checked by the type checker, so
                # growing the union fails the build rather than a live order.
                assert_never(unreachable)

    async def _ack(self, order: PlaceOrder, *, oid: int) -> None:
        """Publish the venue's ack: the order is LIVE at ``oid``.

        Both placement answers that carry an oid come here, ``resting`` and
        ``filled``. LIVE means acked with an oid, not rested (ADR-0011 inv 2).
        This is where the venue's integer oid becomes the saga's string
        ``venue_oid``. ``_order_status`` makes the reverse crossing.
        """
        await self._bus.publish(
            self._status_report(
                cloid=order.cloid,
                symbol=order.symbol,
                status=OrderState.LIVE,
                venue_oid=str(oid),
            )
        )

    def _action_rejected(self, request: str, cloid: str, reason: str) -> None:
        # The venue refused the whole action (bad nonce/signature, an action
        # rate-limit): a 200-OK `err` envelope, not a transport failure and not
        # a per-order REJECTED. Name it and stop — reconcile-by-cloid owns the
        # in-flight order (a place never landed, so a later resend is safe).
        named_event(
            NamedEvent.EXCHANGE_ACTION_REJECTED, request=request, cloid=cloid, reason=reason
        )

    async def cancel(self, ref: OrderRef) -> None:
        cloid, symbol = ref.cloid, ref.symbol
        asset = self._universe.asset_indices[symbol]
        if ref.venue_oid is None:
            # No ack yet, so the cloid is the only handle. The venue may hold
            # an earlier life's order under it too. Which one this cancels is
            # the venue's choice, and reconciliation is the backstop (#354).
            action = {"type": "cancelByCloid", "cancels": [{"asset": asset, "cloid": cloid}]}
        else:
            # The oid names exactly one order, where a cloid may not (#354).
            action = {"type": "cancel", "cancels": [{"a": asset, "o": int(ref.venue_oid)}]}
        adjudication = await read(
            request="cancel",
            query=action,
            send=self._send_action,
            normalize=_cancel_adjudication,
            cloid=cloid,
        )
        if isinstance(adjudication, VenueReadFailure):
            # An ack-lost cancel and an adjudication we cannot read prove the
            # same nothing, and get the same verdict: ``read`` named it, and
            # nothing is emitted. The cancel_requested marker was durable before
            # the send (ADR-0026), so reconciliation resolves this order either
            # way. Faulting the engine over the *shape* of the answer would
            # discard a run that was covered regardless.
            return
        await self._apply_cancellation(cloid=cloid, symbol=symbol, adjudication=adjudication)

    async def _apply_cancellation(
        self, *, cloid: str, symbol: str, adjudication: "_CancelAdjudication"
    ) -> None:
        """Carry out a cancel adjudication that has already been read — the peer
        of ``_apply_placement`` on the other write verb, outside the
        unreadable-body guard for the same reason."""
        match adjudication:
            case _ActionError(message=message):
                # The cancel action was refused, not adjudicated: a benign no-op —
                # the durable cancel_requested marker leaves it to reconciliation.
                self._action_rejected("cancel", cloid, message)
            case _CancelVerdict.CANCELLED:
                await self._bus.publish(
                    self._status_report(cloid=cloid, symbol=symbol, status=OrderState.CANCELLED)
                )
            case _CancelVerdict.ALREADY_GONE:
                # The order is filled, cancelled, or never landed: positive venue
                # truth, not an unreadable body — nothing to name, and nothing to
                # emit, since its real state arrives as its own report or through
                # reconciliation (ADR-0026). Silent *by decision*, which is why it
                # is a named verdict rather than the branch that falls off the end:
                # the accidental silence on the place verb read identically from
                # the outside and was a defect (ADR-0048 §4).
                return
            case unreachable:
                assert_never(unreachable)

    async def fetch_order(self, ref: OrderRef) -> VenueOrderView | VenueReadFailure:
        """Venue truth for ``ref``: the order record plus its fill history,
        the ADR-0011 cross-check in one read. ``unknownOid`` is positive proof
        of no record (an empty view); a read that *failed* is a
        ``VenueReadFailure`` — an outage must never look like "no record"
        (inv 1).

        The fills read is bounded to this order's own lifetime, so a busy
        account's later fills can never push its own past the venue's page cap
        and silently under-report through the ``{cloid}:fill:{tid}`` dedup.
        Where the window starts depends on who still remembers the placement.
        With a record it is the venue's own placement time, exact whatever our
        clock skew. Once the venue has dropped the record it is our ack time
        less a skew allowance. A saga that never checkpointed as LIVE has no
        ack time either, so its read is the whole recent history.

        A failed fills read fails the whole read and carries its own cause
        out. Never a partial view: with a record that would read as "no
        fills", and without one as "no record" (inv 1).
        """
        record = await self._order_status(ref)
        if isinstance(record, VenueReadFailure):
            # The read failed and ``read`` already named which way. Which way is
            # carried out rather than collapsed: an outage says the venue is
            # unreachable and the reconciler's whole pass should stop, while an
            # unreadable body says only that *this* order cannot be read, from a
            # venue answering everything else at full speed (ADR-0049). Neither
            # may ever be mistaken for an empty book.
            return record
        if record is _OrderStatusRead.NO_RECORD:
            # unknownOid: a *successful* read that positively has no record.
            # Either the order never landed, or the venue has since dropped the
            # record while still holding the fills. The ref's oid tells the two
            # apart, and only the second has a history to read (ADR-0011 inv 2).
            if ref.venue_oid is None:
                return VenueOrderView(status=None)
            since_ms = (
                None
                if ref.acked_ts_ns is None
                else ref.acked_ts_ns // _NS_PER_MS - _ACK_SKEW_ALLOWANCE_MS
            )
            fills = await self._fetch_fills(
                cloid=ref.cloid, symbol=ref.symbol, oid=int(ref.venue_oid), since_ms=since_ms
            )
            if isinstance(fills, VenueReadFailure):
                return fills
            return VenueOrderView(status=None, fills=tuple(fills))
        if ref.venue_oid is None and _placed_before(record, created_ts_ns=ref.created_ts_ns):
            # A read by cloid answered with an order placed before this saga
            # existed: an earlier life's, not ours. Its fills are not read, and
            # to the reconciler this is a miss, which the in-flight budget
            # already knows how to count (#354).
            return VenueOrderView(status=None)
        fills = await self._fetch_fills(
            cloid=ref.cloid, symbol=record.coin, oid=record.oid, since_ms=record.timestamp
        )
        if isinstance(fills, VenueReadFailure):
            return fills
        status = self._status_report(
            cloid=ref.cloid, symbol=record.coin, status=record.state, venue_oid=str(record.oid)
        )
        return VenueOrderView(status=status, fills=tuple(fills))

    async def fetch_account_state(self) -> VenueAccountState | None:
        """Venue truth for the account: one ``clearinghouseState`` read, the
        whole account-and-positions snapshot in one response.

        ``None`` only when the read itself failed — an outage must never read as
        a flat book (ADR-0011 inv 1), exactly as ``fetch_order`` carries it. No
        ``cloid``: this grain is the whole account, not one order.
        """
        state = await read(
            request="clearinghouseState",
            query={"type": "clearinghouseState", "user": self._user_address},
            send=self._info,
            normalize=normalize_account_state,
        )
        # One grain, one order, no worklist: this read has nothing behind it to
        # protect, so the two failure causes buy it nothing and it collapses
        # them (ADR-0049). Its caller — the ``StartupBarrier`` — retries the
        # whole sequence either way.
        return None if isinstance(state, VenueReadFailure) else state

    async def verify_account_mode(self) -> AccountModeVerdict:
        """The boot gate's question, asked again before a cash heal writes a
        venue number to disk (ADR-0046 §4).

        Delegated to the same module the gate lives in, so the allowlist and the
        ``userAbstraction`` read have one owner across both paths; what differs
        is only the terminal state, and that difference is stated there.

        No ``Deadline`` is opened: the budget ``start()`` spends is a startup
        concept, and in flight the retry is the cadence itself.
        """
        return await reverify_account_mode(info=self._info, address=self._user_address)

    async def _order_status(self, ref: OrderRef) -> "_OrderDecode | VenueReadFailure":
        """The venue's ``orderStatus`` record for ``ref``, in saga vocabulary.

        By the oid once the saga holds one. The cloid is derived from the
        signal id, and the venue keeps every order ever placed under it, one
        per life of the account, so a read by cloid can answer with an earlier
        life's order. An oid names exactly one (#354). The venue takes either
        key under the same field. The cloid still names the saga in the log.
        """
        key: int | str = ref.cloid if ref.venue_oid is None else int(ref.venue_oid)
        return await read(
            request="orderStatus",
            query={"type": "orderStatus", "user": self._user_address, "oid": key},
            send=self._info,
            normalize=_decode_order_status,
            cloid=ref.cloid,
        )

    async def _fetch_fills(
        self, *, cloid: str, symbol: str, oid: int, since_ms: int | None = None
    ) -> list[FillReport] | VenueReadFailure:
        """This order's fills from the venue's fill history, by its oid. Recent
        fill rows also carry an undocumented ``cloid``. It is absent on older
        fills and on some accounts, so the oid is the only key this read uses
        (``docs/research/hyperliquid-order-status-retention.md``).

        ``since_ms`` bounds the read to fills at or after a known placement time
        (``userFillsByTime``), so an aged order's fills sit at the front of the
        window rather than risking the venue's page cap; without it the whole
        recent history is read (``userFills``), safe only right after placement
        when this order's fills are the newest. A ``VenueReadFailure`` on a read
        that failed — a body we could not parse or a transport that died, both
        named by ``read`` and neither ever silent truth.

        Both queries name themselves ``userFills``, and the mismatch with the
        windowed ``type`` is deliberate rather than missed: the ``request`` label
        names the **read**, not the endpoint (ADR-0048 §6). This is one read at
        one grain — the same fills, for the same order, differing only in how far
        back it looks — and an operator triaging it wants the two windows under
        one name. Deriving the label from ``query["type"]`` would split one read's
        history across two labels for a distinction no triage turns on.
        """
        query: dict[str, Any] = (
            {"type": "userFills", "user": self._user_address}
            if since_ms is None
            else {"type": "userFillsByTime", "user": self._user_address, "startTime": since_ms}
        )

        def rows(response: object) -> list[FillReport]:
            if not isinstance(response, list):
                # Not a container we can walk. Raised rather than answered here,
                # so this function only ever *reads* and ``read`` owns the verdict
                # for every way this read can fail.
                raise TypeError(f"fills response is not a list: {type(response).__name__}")
            # A row inside it can fail on its own — a missing field, a figure that
            # is not a number or not a string, a row that is not even a mapping —
            # and ``oid`` is dereferenced by the filter itself, so a malformed row
            # belonging to *another* order poisons this read too. Correctly: the
            # whole body is one answer, and a partial list would read as the whole
            # truth (ADR-0011 inv 1).
            return [
                FillReport(
                    ts_event=int(entry["time"]) * _NS_PER_MS,
                    ts_init=self._clock.timestamp_ns(),
                    cloid=cloid,
                    symbol=symbol,
                    trade_id=str(entry["tid"]),
                    # A non-finite figure raises nothing on the way in, so without
                    # ``figure`` a ``NaN`` quantity would ride into a
                    # ``FillReport``, poison ``cum_qty`` by arithmetic and leave
                    # its equality cross-check permanently disagreeing — durably,
                    # since the store round-trips ``"NaN"`` back on recovery. A
                    # re-typed one is the same verdict for a different reason: it
                    # would land a fill priced off a figure ``json.loads`` had
                    # already rounded into a ``float``, no longer provably the
                    # one the venue sent.
                    quantity=figure(entry["sz"]),
                    price=figure(entry["px"]),
                    # Read, never reconstructed (ADR-0036): the venue's figure
                    # already carries this account's volume tier, any referral
                    # discount, and its own 6-dp truncation — none of it
                    # knowable from a schedule here. Its ``crossed`` flag is
                    # baked in for the same reason, which is why the maker/taker
                    # bit is not carried onto the report: on this path there is
                    # nothing left to select with it.
                    fee=_fee_settled_in_usdc(entry),
                )
                for entry in response
                if entry["oid"] == oid
            ]

        return await read(
            request="userFills", query=query, send=self._info, normalize=rows, cloid=cloid
        )

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        """The meta-sourced per-symbol specs (ADR-0031), for the Engine to wire
        into the guard. A copy, so a caller can never mutate the universe."""
        return dict(self._universe.specs)

    def account_spec(self) -> AccountSpec:
        """The venue's static declaration about this process's account, composed
        by ``account.py`` — the one module that knows what qualifies one."""
        return self._account_spec

    async def _info(self, query: dict[str, Any]) -> object:
        return await self._post(f"{self._config.api_url}/info", query)

    def _status_report(
        self,
        *,
        cloid: str,
        symbol: str,
        status: OrderState,
        venue_oid: str | None = None,
        reason: str | None = None,
    ) -> OrderStatusReport:
        now = self._clock.timestamp_ns()
        return OrderStatusReport(
            ts_event=now,
            ts_init=now,
            cloid=cloid,
            symbol=symbol,
            status=status,
            venue_oid=venue_oid,
            reason=reason,
        )

    def _order_wire(self, order: PlaceOrder) -> dict[str, Any]:
        # Field order matters: the venue re-encodes the JSON action with
        # msgpack to verify the signature, so the wire must serialize exactly
        # as it was hashed — a/b/p/s/r/t/c, matching the SDK's encoder.
        return {
            "a": self._universe.asset_indices[order.symbol],
            "b": order.side is Side.BUY,
            "p": _wire_decimal(self._limit_price(order)),
            "s": _wire_decimal(order.quantity),
            "r": False,  # reduce_only is deferred (ADR-0030)
            "t": {"limit": {"tif": self._wire_tif(order)}},
            "c": order.cloid,
        }

    def _limit_price(self, order: PlaceOrder) -> Decimal:
        if order.order_type is OrderType.LIMIT:
            if order.price is None:
                raise ValueError(f"LIMIT order {order.cloid} has no price")
            return order.price
        # MARKET → aggressive IOC limit (ADR-0030): no native market type at
        # the venue, so the order is priced through the book with a bound. The
        # quantizer's passive rounding keeps the bound honest — a buy rounds
        # down, a sell up, never past the slippage cap.
        latest = self._latest_price.get(order.symbol)
        if latest is None:
            raise ValueError(f"no market tick cached for {order.symbol!r}; cannot bound MARKET")
        bound = (
            1 + self._config.slippage_bound
            if order.side is Side.BUY
            else 1 - self._config.slippage_bound
        )
        return quantize_price(latest * bound, order.side, self._universe.specs[order.symbol])

    def _wire_tif(self, order: PlaceOrder) -> str:
        if order.order_type is OrderType.MARKET:
            return "Ioc"
        if order.post_only:
            return "Alo"
        return _TIF_WIRE[order.time_in_force]

    async def _send_action(self, action: dict[str, Any]) -> object:
        from hyperliquid.utils.signing import sign_l1_action

        # The clock's ms, floored to stay strictly above the last nonce: two
        # sends inside one millisecond still get increasing nonces, which the
        # venue requires per address.
        nonce = max(self._clock.timestamp_ns() // _NS_PER_MS, self._last_nonce + 1)
        self._last_nonce = nonce
        signature = sign_l1_action(
            self._wallet, action, None, nonce, None, not self._config.testnet
        )
        payload = {"action": action, "nonce": nonce, "signature": signature}
        return await self._post(f"{self._config.api_url}/exchange", payload)


@dataclass(frozen=True, slots=True)
class _OrderRecord:
    """The venue's decoded ``orderStatus`` order record: the coin it lives
    under, the venue oid, its placement timestamp (ms), and its status already
    in saga vocabulary. ``fetch_order`` bounds the fills read at ``timestamp``
    and reports ``state``."""

    coin: str
    oid: int
    timestamp: int
    state: OrderState


class _OrderStatusRead(Enum):
    """An ``orderStatus`` read that succeeded and holds no order record.

    One member, and it stays an enum for that reason: ``NO_RECORD`` is a
    *positive* claim — the venue says this cloid never landed — and the type is
    what keeps it from ever being written as the ``VenueReadFailure`` that means
    the read failed. The two are opposite answers (ADR-0008's resend gate turns
    on which), so they must not share a representation.
    """

    NO_RECORD = "no_record"  # unknownOid — the venue positively has no record


# What the ``orderStatus`` decode below answers with. Never a
# ``VenueReadFailure``: that is ``read``'s to return, and it means something else
# entirely.
_OrderDecode = _OrderRecord | _OrderStatusRead


def _placed_before(record: "_OrderRecord", *, created_ts_ns: int | None) -> bool:
    """Whether the venue placed ``record`` before the saga was created, by more
    than the skew allowance between our clock and the venue's. A saga with no
    stamp cannot tell, and keeps the record."""
    if created_ts_ns is None:
        return False
    return record.timestamp < created_ts_ns // _NS_PER_MS - _ACK_SKEW_ALLOWANCE_MS


def _decode_order_status(response: object) -> _OrderDecode:
    """Decode a venue ``orderStatus`` response into its order record, or the
    venue's positive ``unknownOid``. Any shape outside those two raises into
    ``UNREADABLE`` — a failed read, never venue truth.

    A status outside the saga taxonomy is a failed read like any unreadable
    body, and refusing it *here* rather than after the read is what puts it
    inside ``read``'s naming — the venue's own string reaches the operator,
    which on a body that parsed cleanly is the entire triage.
    """
    match response:
        case {"status": "unknownOid"}:
            return _OrderStatusRead.NO_RECORD
        case {
            "status": "order",
            "order": {
                "order": {"coin": str(coin), "oid": int(oid), "timestamp": int(timestamp)},
                "status": str(status),
            },
        }:
            return _OrderRecord(coin=coin, oid=oid, timestamp=timestamp, state=_order_state(status))
    raise ValueError("unrecognized orderStatus response")


def _order_state(status: str) -> OrderState:
    """The saga vocabulary for a venue order-status string, raising into
    ``UNREADABLE`` for a status we cannot map (freeze, never misclassify).

    The venue's taxonomy is a long list of specific causes, but every entry
    resolves by suffix: ``…Rejected`` refusals, ``…Canceled`` / ``…Cancel``
    removals (``canceled``, ``marginCanceled``, ``scheduledCancel``, …).
    """
    match status:
        case "open":
            return OrderState.LIVE
        case "filled":
            return OrderState.FILLED
        case _ if status.endswith(("anceled", "ancel")):
            return OrderState.CANCELLED
        case _ if status.endswith("ejected"):
            return OrderState.REJECTED
    raise ValueError(f"unmappable order status {status!r}")


@dataclass(frozen=True, slots=True)
class _ActionError:
    """A Hyperliquid action-level refusal (the ``{"status": "err", ...}``
    envelope): the whole order/cancel action was rejected before adjudication —
    a bad nonce or signature, an action rate-limit — distinct from a per-order
    ``error`` status and never a transport failure. ``message`` is the venue's
    reason string, for the operator's triage."""

    message: str


def _action_outcome(response: object) -> list[Any] | _ActionError:
    """The ``statuses`` array out of an ok /exchange action response (dicts for
    orders, bare strings for cancels), or an ``_ActionError`` for the venue's
    documented action-level ``err`` envelope.

    A shape that is neither raises into ``UNREADABLE``. It used to fault the
    engine from here, on the reasoning that a body we cannot parse is a genuine
    failure — which is true, and is not the same as saying it is *this* layer's
    to answer. An unreadable body is a failed read wherever it is read (inv 1),
    and both write verbs already hold the backstop that makes the milder verdict
    safe: nothing is reported, and reconcile-by-cloid resolves the order.
    """
    match response:
        case {"status": "ok", "response": {"data": {"statuses": list(statuses)}}}:
            return statuses
        case {"status": "err", "response": message}:
            return _ActionError(str(message))
    raise ValueError(f"unrecognized Hyperliquid action response: {response!r}")


@dataclass(frozen=True, slots=True)
class _Resting:
    """The venue booked the order: it is LIVE at ``oid``.

    The oid is the venue's integer, as on ``_Filled`` and ``_OrderRecord``. It
    becomes the saga's string ``venue_oid`` in one place, ``_ack``.
    """

    oid: int


@dataclass(frozen=True, slots=True)
class _Rejected:
    """The venue judged the order and refused it, with its own reason."""

    reason: str


@dataclass(frozen=True, slots=True)
class _Filled:
    """The order filled on arrival. Carries only the oid, because the placement
    response has no trade ids and the fills must be read from the venue's own
    records (ADR-0011)."""

    oid: int


# One placement adjudication, read. Four members for the three per-order
# statuses the venue documents plus the action-level refusal that precedes
# adjudication — a union rather than one record with optional fields, so a
# combination the venue cannot produce is one this adapter cannot represent.
_Adjudication = _ActionError | _Resting | _Rejected | _Filled


class _CancelVerdict(Enum):
    """A cancel the venue adjudicated, either way it went.

    ``ALREADY_GONE`` is a *named* outcome rather than the branch that falls off
    the end, and that is the whole reason this is an enum and not a ``bool``:
    the adapter deliberately emits nothing for it, and an accidental silence
    looks exactly the same from outside (ADR-0048 §4).
    """

    CANCELLED = "cancelled"  # the venue confirmed it
    ALREADY_GONE = "already_gone"  # filled, cancelled, or never landed


# One cancel adjudication, read — the verdict, or the action-level refusal that
# precedes any verdict. The peer of ``_Adjudication`` on the other write verb.
_CancelAdjudication = _ActionError | _CancelVerdict


def _placement_adjudication(response: object) -> _Adjudication:
    """Read a venue placement response: one order in, one status out of
    ``statuses`` (ADR-0015).

    Pure, and that is load-bearing rather than tidy — everything unreadable
    about a placement body is decided in here, so the caller's guard can cover
    exactly this and never the publishing that follows it (ADR-0048 §4).

    Any shape outside the three documented adjudications raises into
    ``UNREADABLE``. It used to fall out of the reporting silently, which is the
    one outcome inv 1 forbids outright: a venue that started adjudicating a
    fourth way would leave orders unreported with nothing recording that it had.
    """
    outcome = _action_outcome(response)
    if isinstance(outcome, _ActionError):
        return outcome
    (status,) = outcome
    if "resting" in status:
        return _Resting(oid=int(status["resting"]["oid"]))
    if "error" in status:
        return _Rejected(reason=str(status["error"]))
    if "filled" in status:
        return _Filled(oid=int(status["filled"]["oid"]))
    raise ValueError(f"unrecognized placement status: {status!r}")


def _cancel_adjudication(response: object) -> _CancelAdjudication:
    """Read a venue cancel response — the peer of ``_placement_adjudication``
    on the other write verb, pure for the same reason.

    The venue adjudicates a cancel two ways and both are matched, rather than
    one matched and everything else swept into the other. ``ALREADY_GONE`` is a
    *claim* — the venue says this order is filled, cancelled, or never landed —
    and reading it as "not ``success``" would let a status the venue has never
    sent make that claim on its behalf, going out silent as the ordinary
    cancel/fill race. Any third shape raises into ``UNREADABLE`` for the reason
    its placement twin does: a venue that started adjudicating a fourth way
    would leave orders unreported with nothing recording that it had (ADR-0048
    §4). The backstop is untouched — the durable ``cancel_requested`` marker
    leaves the order to reconciliation either way (ADR-0026).
    """
    outcome = _action_outcome(response)
    if isinstance(outcome, _ActionError):
        return outcome
    (status,) = outcome
    if status == "success":
        return _CancelVerdict.CANCELLED
    if isinstance(status, Mapping) and "error" in status:
        return _CancelVerdict.ALREADY_GONE
    raise ValueError(f"unrecognized cancel status: {status!r}")


def _fee_settled_in_usdc(entry: Mapping[str, Any]) -> Decimal:
    """One fill's reported fee, refusing a fee settled in any other token.

    The venue gives this grain a discriminator to read — ``feeToken`` — which is
    what the funding grain's counterpart does not have, so the detection is
    each one's own and the refusal behind it is shared
    (``reading.refuse_non_usdc``, which carries the ADR-0029/0048 reasoning for
    both).

    The assumption is guarded here rather than carried as a ``fee_currency``
    field nothing yet reads — perp fees are USDC-settled today, and spot is out
    of scope (ADR-0030).

    What is worth keeping *here* is what the alternative verdict would have cost
    this caller specifically: answered as ``UNREADABLE``, the refusal would skip
    the order for the whole ``unreadable_grace_seconds`` span, re-read it to the
    same refusal, and then fault on the cloid alone — naming strictly less than
    this refusal can, since only here is the offending token in hand (ADR-0036
    §4, ADR-0049 §4, and ADR-0048 §2's amended note for what the escalation no
    longer has to prevent).
    """
    token = entry["feeToken"]
    if token != "USDC":
        refuse_non_usdc(reported=f"fill fee settled in {token!r}, not USDC", row="fill")
    return figure(entry["fee"])


def _wire_decimal(value: Decimal) -> str:
    """Render a ``Decimal`` in the venue's wire format: plain notation, no
    exponent, no trailing zeros (the SDK's ``float_to_wire`` normalization,
    minus the float round-trip)."""
    return f"{value.normalize():f}"
