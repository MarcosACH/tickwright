"""``HyperliquidConfig`` — the venue package's config lives in its package
(ADR-0032); only the composition root reads them all."""

from pydantic import BaseModel, Field

_MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"
_TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"


class HyperliquidConfig(BaseModel):
    """Which network to speak to and which coins to subscribe (ADR-0021)."""

    testnet: bool = False
    symbols: list[str] = Field(default_factory=list)
    # Reconnect pacing (ADR-0021): doubling from initial, capped at max, always
    # slept on the injected Clock — a reconnect storm can never hammer the venue.
    reconnect_initial_backoff_seconds: float = Field(default=1.0, gt=0)
    reconnect_max_backoff_seconds: float = Field(default=60.0, gt=0)

    @property
    def ws_url(self) -> str:
        """The venue WS endpoint the ``testnet`` toggle selects."""
        return _TESTNET_WS_URL if self.testnet else _MAINNET_WS_URL
