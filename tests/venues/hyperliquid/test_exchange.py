"""``HyperliquidExchange`` venue translation (issue #23): mocked-HTTP tests.

The POST transport is the only fake; signing is real (SDK utilities over a
throwaway, publicly-known test key), so every asserted wire is one the venue
could verify — the recovery check proves the signature binds the exact action
sent. Venue quirk translation lives in the adapter, never the engine
(ADR-0030): MARKET becomes an aggressive IOC limit at
``latest × (1 ± slippage_bound)``, quantized per the ADR-0017 price rule.
"""

import asyncio
from decimal import Decimal

from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
from hyperliquid_fakes import FakeExchangeApi, resting_response

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import (
    AggressorSide,
    InstrumentSpec,
    MarketTick,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
)
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)

# Anvil's account #0 — a publicly-known throwaway key, safe in a test file.
TEST_SIGNING_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
CLOID = "0x" + "ab" * 16

BTC_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=5,
    max_decimals=6,
    min_notional=Decimal("10"),
    max_sig_figs=5,
)
UNIVERSE = HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3})

_NS_PER_MS = 1_000_000


def make_exchange(
    post: FakeExchangeApi, *, bus: InMemoryBus, clock: ManualClock
) -> HyperliquidExchange:
    config = HyperliquidConfig(
        testnet=True,
        symbols=["BTC"],
        signing_key=TEST_SIGNING_KEY,
        slippage_bound=Decimal("0.05"),
    )
    return HyperliquidExchange(config=config, bus=bus, clock=clock, universe=UNIVERSE, post=post)


def tick(symbol: str, price: str) -> MarketTick:
    return MarketTick(
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id="1",
        seq=0,
        venue_trade_id=True,
        ts_event=1_700_000_000_000 * _NS_PER_MS,
        ts_init=1_700_000_000_000 * _NS_PER_MS,
    )


def market_order(side: Side, quantity: str) -> PlaceOrder:
    return PlaceOrder(
        cloid=CLOID,
        symbol="BTC",
        side=side,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def test_market_buy_places_an_aggressive_ioc_limit_at_the_bounded_price() -> None:
    async def main() -> tuple[FakeExchangeApi, ManualClock]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=1_700_000_001_000 * _NS_PER_MS)
        post = FakeExchangeApi([resting_response(oid=77)])
        exchange = make_exchange(post, bus=bus, clock=clock)
        await bus.publish(tick("BTC", "43250.5"))
        await exchange.place(market_order(Side.BUY, "0.5"))
        return post, clock

    post, clock = asyncio.run(main())

    url, payload = post.requests[0]
    assert url == "https://api.hyperliquid-testnet.xyz/exchange"
    action = payload["action"]
    # 43250.5 × 1.05 = 45413.025, quantized to the 5-sig-fig ∧ 1-decimal grid
    # (buy rounds down, so the bound is never exceeded) → 45413. IOC, never a
    # native market type; cloid rides along for reconcile-by-cloid.
    assert action == {
        "type": "order",
        "orders": [
            {
                "a": 3,
                "b": True,
                "p": "45413",
                "s": "0.5",
                "r": False,
                "t": {"limit": {"tif": "Ioc"}},
                "c": CLOID,
            }
        ],
        "grouping": "na",
    }
    # The nonce is the injected clock's ms timestamp (ADR-0005), and the
    # signature recovers to the signing key's address over exactly this action
    # on testnet — a venue-verifiable wire, not just a plausible-looking one.
    assert payload["nonce"] == clock.timestamp_ns() // _NS_PER_MS
    recovered = recover_agent_or_user_from_l1_action(
        action, payload["signature"], None, payload["nonce"], None, False
    )
    assert recovered == Account.from_key(TEST_SIGNING_KEY).address
