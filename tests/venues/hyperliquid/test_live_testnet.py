"""The opt-in live testnet suite (issue #23, ADR-0022): real venue, real key.

Runs only when opted in with ``TICKWRIGHT_LIVE_TESTNET`` (issue #73) and given
``TICKWRIGHT_HYPERLIQUID__SIGNING_KEY`` (a funded testnet API wallet), never in
the CI gate — ``uv run pytest -m live`` is the manual/nightly invocation. The
opt-in flag is a dedicated run-gate that maps onto no config field, so CI can
run the whole suite under a hostile config without enrolling this one. One
round trip proves the whole
write path against the real venue: place a deep out-of-the-money resting
LIMIT → reconcile-visible via ``fetch_order`` → cancel by cloid →
``CANCELLED``, both as a pushed report and as venue truth.
"""

import asyncio
import os
from decimal import Decimal

import pytest
from live_gate import LIVE_TESTNET_ENV, live_testnet_enabled
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import LiveClock
from tickwright.domain import (
    ExecutionReport,
    LeverageBook,
    LeverageSpec,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueOrderView,
    derive_cloid,
    quantize_price,
    quantize_size,
)
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    fetch_instrument_specs,
)
from tickwright.venues.hyperliquid.account import held_leverage
from tickwright.venues.hyperliquid.transport import post_json

SIGNING_KEY_ENV = "TICKWRIGHT_HYPERLIQUID__SIGNING_KEY"
ACCOUNT_ADDRESS_ENV = "TICKWRIGHT_HYPERLIQUID__ACCOUNT_ADDRESS"
SYMBOL = "BTC"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not live_testnet_enabled(),
        reason=f"no {LIVE_TESTNET_ENV}: live testnet suite not opted in (ADR-0022)",
    ),
]


def test_place_reconcile_cancel_round_trip_on_testnet() -> None:
    async def main() -> None:
        config = HyperliquidConfig(
            testnet=True,
            symbols=[SYMBOL],
            signing_key=SecretStr(os.environ[SIGNING_KEY_ENV]),
            account_address=os.environ.get(ACCOUNT_ADDRESS_ENV),
        )
        universe = await fetch_instrument_specs(config)
        spec = universe.specs[SYMBOL]

        bus = InMemoryBus()
        clock = LiveClock()
        exchange = HyperliquidExchange(
            config=config,
            bus=bus,
            clock=clock,
            universe=universe,
            startup_timeout_seconds=60.0,
        )
        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)

        # A deep out-of-the-money buy — half the mid — rests and cannot fill;
        # the size clears the venue's $10 minimum with margin at that price.
        mids = await post_json(f"{config.api_url}/info", {"type": "allMids"})
        assert isinstance(mids, dict)
        mid = Decimal(str(mids[SYMBOL]))
        price = quantize_price(mid / 2, Side.BUY, spec)
        quantity = quantize_size(Decimal("12") / price, spec)

        # A fresh cloid per run: the venue remembers old ones, and this saga
        # identity is exactly what reconcile-by-cloid keys on (ADR-0006).
        cloid = derive_cloid(f"live-test:{SYMBOL}:{clock.timestamp_ns()}")

        await exchange.place(
            PlaceOrder(
                cloid=cloid,
                symbol=SYMBOL,
                side=Side.BUY,
                quantity=quantity,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                price=price,
            )
        )
        (live,) = [r for r in reports if isinstance(r, OrderStatusReport)]
        assert live.status is OrderState.LIVE, f"expected a resting order, got {live!r}"
        assert live.cloid == cloid

        # Reconcile-visible: the fetch path sees the resting order as venue truth.
        view = await exchange.fetch_order(cloid)
        assert isinstance(view, VenueOrderView) and view.status is not None
        assert view.status.status is OrderState.LIVE
        assert view.fills == ()

        await exchange.cancel(cloid)
        cancelled = [
            r
            for r in reports
            if isinstance(r, OrderStatusReport) and r.status is OrderState.CANCELLED
        ]
        assert cancelled, "the venue-accepted cancel must report CANCELLED"

        # And the venue agrees: the order record itself reads canceled.
        after = await exchange.fetch_order(cloid)
        assert isinstance(after, VenueOrderView) and after.status is not None
        assert after.status.status is OrderState.CANCELLED

    asyncio.run(main())


async def _venue_leverage(config: HyperliquidConfig, *, address: str) -> LeverageSpec:
    """The account's stored setting for ``SYMBOL``, read through ``activeAssetData``.

    Deliberately **not** the endpoint the push itself reads. ADR-0044 §4 declined
    ``activeAssetData`` for the boot because it is one request *per symbol* where
    ``clearinghouseState`` answers a whole boot in one; that cost is irrelevant to
    a single-symbol assertion, and paying it buys the two things an oracle needs.
    It is a different code path from the one under test, so it cannot agree with
    the push by construction — and it reports the setting for a **flat** symbol,
    which ``clearinghouseState`` does not (it carries leverage only on positions
    the account holds). Reading the push's own source would therefore have left
    the only branch this arm can safely exercise unobservable.
    """
    body = await post_json(
        f"{config.api_url}/info",
        {"type": "activeAssetData", "user": address, "coin": SYMBOL},
    )
    assert isinstance(body, dict), f"unreadable activeAssetData: {body!r}"
    leverage = body["leverage"]
    return LeverageSpec(mode=leverage["type"], leverage=int(leverage["value"]))


def test_the_boot_pushes_configured_leverage_to_testnet() -> None:
    """ADR-0044 §7's ``updateLeverage`` push against the real venue (#180).

    The one signed write the boot makes, and the half of it no recorded response
    can prove: that the action we sign is the action Hyperliquid accepts, and
    that accepting it actually moves the account. The unit arm drives a fake
    transport, so an asset index off by one, a misnamed field or a rejected
    signature would all pass it.

    Scoped to the branch that is safe to run for real: ``SYMBOL`` **flat**, so
    the push writes blind. The other two branches are deliberately not here —
    ``aligned`` is unobservable (the venue returns the identical envelope either
    way, which is why §6 makes it a success rather than a detection), and the
    held disagreement would need an open position to refuse against, i.e. this
    suite opening a real one and leaving it if the refusal it is testing fires.
    A held symbol is skipped rather than flattened: liquidating whatever an
    operator's testnet account is carrying is not a test's business.

    Both halves of the single action are moved, and moved *away* from where the
    account already was, so a push that silently no-ops cannot pass: the mode is
    flipped and the value changed. The setting is restored on the way out
    through a second boot, which keeps the arm re-runnable and leaves the
    account as the suite found it.

    Calling ``start()`` also puts ADR-0046's account-mode gate on a real venue
    for the first time — the push runs behind it, so a testnet account in a
    pooled mode fails here with ``VenueAccountModeUnsupported`` rather than
    reaching the write at all.
    """

    async def main() -> None:
        config = HyperliquidConfig(
            testnet=True,
            symbols=[SYMBOL],
            signing_key=SecretStr(os.environ[SIGNING_KEY_ENV]),
            account_address=os.environ.get(ACCOUNT_ADDRESS_ENV),
        )
        from eth_account import Account

        address = config.account_address or str(
            Account.from_key(os.environ[SIGNING_KEY_ENV]).address
        )
        universe = await fetch_instrument_specs(config)

        state = await post_json(
            f"{config.api_url}/info", {"type": "clearinghouseState", "user": address}
        )
        if SYMBOL in held_leverage(state):
            pytest.skip(
                f"the testnet account holds a {SYMBOL} position; flatten it to run this arm"
            )

        before = await _venue_leverage(config, address=address)
        cap = universe.specs[SYMBOL].max_leverage
        assert cap >= 3, f"{SYMBOL} caps at {cap}x, too low to move the setting twice"
        target = LeverageSpec(
            mode="cross" if before.mode == "isolated" else "isolated",
            leverage=3 if before.leverage != 3 else 2,
        )

        def _boot(book: LeverageSpec) -> HyperliquidExchange:
            return HyperliquidExchange(
                config=config,
                bus=InMemoryBus(),
                clock=LiveClock(),
                universe=universe,
                startup_timeout_seconds=60.0,
                leverage=LeverageBook(entries={SYMBOL: book}),
            )

        try:
            await _boot(target).start()
            assert await _venue_leverage(config, address=address) == target
        finally:
            await _boot(before).start()

        assert await _venue_leverage(config, address=address) == before

    asyncio.run(main())
