"""Live funding ingest: the venue's own payments, transformed by nothing
(ADR-0037).

Paper **computes** funding off the `Clock` and this **ingests** it — the two
`Exchange` adapters are the seam's two implementations, which is why there is no
`FundingModel` object to swap. What lands on the bus is the same keyed
`FundingAccrual` either way, so the projection's apply path carries no `if
live:`.

`amount` is `usdc` verbatim, sign included. ADR-0037 §Sign fixes that the accrual
mirrors its venue source field rather than a house convention, so a later
reconcile is a direct field compare instead of an argument about which side
negated. Nothing here may normalise, `abs()`, or flip it — a transformation on
this path is a flip bug waiting for the first short position.
"""

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any, NoReturn

from tickwright.domain import Clock, EventBus, FundingAccrual, VenueFactUnsupported

from .config import HyperliquidConfig
from .reading import figure, rendered
from .session import WsSession
from .transport import Connect, WsConnection

_NS_PER_MS = 1_000_000


def accruals(
    fundings: Iterable[Mapping[str, Any]],
    *,
    account_id: str,
    ts_init_ns: int,
) -> tuple[FundingAccrual, ...]:
    """The venue's reported payments as keyed accruals, in venue-time order.

    `boundary_ts_ns` is the venue's own `time` — when the payment for that hour
    landed on the account — which is also `ts_event`, because the venue's
    settlement instant is the instant the fact occurred (ADR-0005). Paper's
    counterpart is its epoch-aligned boundary; the two never have to agree,
    since an account is either paper or live and the key only dedupes within one
    account's stream.

    **Sorted by venue time ascending, and that is this function's real content.**
    The venue documents no delivery order within a batch, while the ledger's
    watermark is monotonic: fed `t3, t1, t2` it applies `t3`, advances the mark
    past it, and then drops `t1` and `t2` as already-applied — real payments the
    account never sees again, because the snapshot is delivered once. Across
    deliveries the venue's documented behavior supplies that monotonicity
    (snapshot, then payments on the hour); within one batch nothing does but
    this line.

    The sort is **stable**, so records sharing a boundary keep the venue's
    delivery order. That is the common case rather than a corner — every symbol
    settles on the same hour — and there is nothing better to order them by:
    they are distinct payments keyed apart by `symbol`, so the gate sees one
    boundary either way and a tie-break would be inventing a sequence the venue
    did not report.
    """
    ordered = sorted(fundings, key=lambda record: int(record["time"]))
    return tuple(
        FundingAccrual(
            ts_event=int(record["time"]) * _NS_PER_MS,
            ts_init=ts_init_ns,
            account_id=account_id,
            symbol=record["coin"],
            boundary_ts_ns=int(record["time"]) * _NS_PER_MS,
            amount=_settled_in_usdc(record),
        )
        for record in ordered
    )


def _settled_in_usdc(record: Mapping[str, Any]) -> Decimal:
    """One payment's amount, refusing a payment denominated in anything else.

    A funding record carries no currency discriminator the way a fill carries
    `feeToken`: the amount field is *named* `usdc`, so the denomination is the
    key itself and a payment settled in another token arrives as a record with
    no `usdc` at all. This engine's money is a bare `Decimal` with USDC left
    implicit (ADR-0029), so such a payment has nowhere to go — accruing it would
    add a figure of one currency to a line of another with nothing in the ledger
    recording which token it came from.

    The failure worth guarding is the quiet one. Defaulting the read to `0` would
    accrue a zero, and a zero is indistinguishable from a boundary that genuinely
    owed nothing, so the account would under-count real money with no trace that
    it had. The refusal names the keys the record *did* carry, which is the one
    thing that tells an operator what the venue actually sent.

    `VenueFactUnsupported`, on the same reasoning as the fill fee's guard of the
    same assumption: a settled funding row never changes, so this is known
    permanent at the first read and escalates out of the seam rather than
    entering `UNREADABLE`, whose whole purpose is to find out — by waiting —
    whether a condition is durable (ADR-0048).
    """
    if "usdc" not in record:
        raise VenueFactUnsupported(
            f"funding payment on {record.get('coin')!r} reports no 'usdc' amount "
            f"(keys: {sorted(record)}): this engine's money is a bare Decimal with "
            "USDC implicit (ADR-0029), so a payment settled in another token has no "
            "home in the ledger. Retrying cannot change a settled funding row."
        )
    return figure(record["usdc"])


class FundingIngest:
    """The `userFundings` subscription, for as long as the exchange lives.

    A class rather than a bare coroutine because the loop has to be *stopped*
    from outside it: `Exchange.stop()` is what ends the run, and it ends it by
    closing the socket the reader is blocked on. It lives here rather than on
    `HyperliquidExchange` for the reason paper's generator lives outside
    `PaperExchange` — a non-terminating loop does not belong in a class whose
    other 800 lines are request-scoped HTTP.

    Reconnect is the feed's own loop and the same object (`WsSession`), with one
    difference that costs nothing: resubscribing re-delivers the snapshot, so
    every payment missed while the socket was down arrives again. The ledger's
    watermark drops the ones already applied, which makes a reconnect *heal* the
    gap rather than leave one — the same durable gate that makes live's
    reconcile re-ingest a no-op (ADR-0043 §5.2).
    """

    def __init__(
        self,
        *,
        config: HyperliquidConfig,
        bus: EventBus,
        clock: Clock,
        account_id: str,
        address: str,
        connect: Connect,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._account_id = account_id
        self._address = address
        self._session = WsSession(
            config=config,
            clock=clock,
            connect=connect,
            subscribe=self._subscribe,
            consume=self._read_frames,
        )

    async def run(self) -> None:
        await self._session.run()

    async def stop(self) -> None:
        await self._session.stop()

    async def _subscribe(self, connection: WsConnection) -> None:
        """The one authenticated-by-address channel this adapter reads.

        Keyed by the *trading* account, which is the signing key's own address
        only when the key is not an agent wallet acting for a master account —
        the same address every `/info` query about this account uses.
        """
        message = {
            "method": "subscribe",
            "subscription": {"type": "userFundings", "user": self._address},
        }
        await connection.send(json.dumps(message))

    async def _read_frames(self, connection: WsConnection) -> None:
        """Publish every payment the venue delivers, batch by batch.

        **No `ConflatingIngress` here, deliberately.** The feed conflates under
        backpressure because a stale tick is worthless next to a fresh one
        (ADR-0023); every funding payment is a distinct movement of cash and
        dropping one loses money. A slow subscriber must therefore back the
        socket up rather than have its payments collapsed — which it can, because
        this stream is hourly, not per-trade.
        """
        async for frame in connection:
            for accrual in self._parse(frame):
                await self._bus.publish(accrual)

    def _parse(self, frame: str) -> tuple[FundingAccrual, ...]:
        """One frame's payments, for a frame this sources; nothing for one it
        does not; a refusal for one it cannot tell apart.

        **Two answers, and which frame gets which is the whole rule.** A frame
        naming a channel that is not this one is *ignored*: the venue's own
        `subscriptionResponse` and `pong` ride this socket as constant
        housekeeping, they are not deliveries of payments, and naming them would
        drown the log in traffic nothing is wrong with. Everything else
        **refuses** — a frame on `userFundings` whose body is not a batch of
        payments, and a frame that names no channel at all because it is not a
        JSON object.

        That second one is where this parts company with `feed.py`, which drops
        such a frame and names it. The tick stream is lossy by contract
        (ADR-0023) and another tick is along in a moment; a payment has no next
        delivery, so a frame we cannot read is an *unknown number of payments*
        rather than none — and returning `()` books that unknown as zero, which
        is `_settled_in_usdc`'s silent-zero failure one grain up. The account
        would under-count real money with nothing recording that it had.

        Nothing here can be healed by waiting either, which is what makes the
        refusal permanent rather than a freeze (ADR-0048): a websocket message
        arrives whole or not at all — fragments are the client's to reassemble
        (RFC 6455 §5.4) and a connection that dies mid-message ends iteration
        instead — so an unreadable body is the venue's contract having changed,
        not a truncation a re-read could fix.

        `isSnapshot` is read past. The first message is the historical snapshot
        and later ones are the payments on the hour, but a payment is a payment
        however it was delivered — and the watermark, not this flag, is what
        decides which have already been applied.
        """
        try:
            message = json.loads(frame)
        except json.JSONDecodeError:
            _refuse_delivery(frame, "is not JSON")
        if not isinstance(message, dict):
            _refuse_delivery(message, "is JSON but not an object, so it names no channel")
        if message.get("channel") != "userFundings":
            return ()
        data = message.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("fundings"), list):
            _refuse_delivery(message, "carries no 'fundings' list")
        return accruals(
            data["fundings"],
            account_id=self._account_id,
            ts_init_ns=self._clock.timestamp_ns(),
        )


def _refuse_delivery(delivered: object, why: str) -> NoReturn:
    """Refuse a frame off the funding socket, quoting what actually arrived.

    Quoting is not garnish: an unreadable delivery is how a venue contract
    change presents, and the key set is what identifies one — so this borrows
    `reading.rendered`, the same bounded shape-first rendering a failed venue
    read carries, rather than inventing a second way to say it.

    `VenueFactUnsupported` on the same reasoning as the record-grain refusal
    below it, and for the same reason it is not a member of `UNREADABLE`
    (ADR-0048): the condition is already known permanent at the first read, so
    answering it as something a retry could fix would spend a wait to learn what
    the frame already showed.
    """
    raise VenueFactUnsupported(
        f"funding delivery {why}: {rendered(delivered)}. Every message on this "
        "channel is cash, and a body this process cannot read is an unknown "
        "number of payments rather than none — banking it as zero would "
        "under-count real money with nothing recording that it had. A websocket "
        "message arrives whole or not at all (RFC 6455 §5.4), so retrying cannot "
        "change what this frame says."
    )
