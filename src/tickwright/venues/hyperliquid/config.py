"""``HyperliquidConfig`` — the venue package's config lives in its package
(ADR-0032); only the composition root reads them all."""

from decimal import Decimal

from pydantic import BaseModel, Field, SecretStr

_MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"
_TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"
_MAINNET_API_URL = "https://api.hyperliquid.xyz"
_TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"


class HyperliquidConfig(BaseModel):
    """Which network to speak to, which coins to subscribe, and how to sign
    (ADR-0021). The signing key is env-only and a ``SecretStr`` — never
    persisted, redacted from logs (ADR-0020); the feed and the default paper
    path need no key at all."""

    testnet: bool = False
    symbols: list[str] = Field(default_factory=list)
    signing_key: SecretStr | None = None
    # The account orders are placed for. Defaults to the signing key's own
    # address; set it when the key is an API/agent wallet acting for a master
    # account, whose address is what every /info query must ask about.
    account_address: str | None = None
    # MARKET → aggressive-IOC translation bound (ADR-0030): the limit price is
    # latest × (1 ± slippage_bound). The default mirrors the venue SDK's 5%.
    slippage_bound: Decimal = Field(default=Decimal("0.05"), ge=0)
    # Reconnect pacing (ADR-0021): doubling from initial, capped at max, always
    # slept on the injected Clock — a reconnect storm can never hammer the venue.
    reconnect_initial_backoff_seconds: float = Field(default=1.0, gt=0)
    reconnect_max_backoff_seconds: float = Field(default=60.0, gt=0)

    @property
    def ws_url(self) -> str:
        """The venue WS endpoint the ``testnet`` toggle selects."""
        return _TESTNET_WS_URL if self.testnet else _MAINNET_WS_URL

    @property
    def api_url(self) -> str:
        """The venue HTTP API base the ``testnet`` toggle selects."""
        return _TESTNET_API_URL if self.testnet else _MAINNET_API_URL
