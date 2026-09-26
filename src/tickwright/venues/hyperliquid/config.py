"""``HyperliquidConfig`` — the venue package's config lives in its package
(ADR-0032); only the composition root reads them all."""

from decimal import Decimal
from typing import Self

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from tickwright.domain import duration_ns

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
    reconnect_initial_backoff_seconds: float = 1.0
    reconnect_max_backoff_seconds: float = 60.0

    @field_validator("reconnect_initial_backoff_seconds", "reconnect_max_backoff_seconds")
    @classmethod
    def _usable_seconds(cls, value: float, info: ValidationInfo) -> float:
        # Infinity loads as a float, and the reconnect loop would sleep on it
        # forever. The feed then stops with no error, so refuse it at boot.
        assert info.field_name is not None
        duration_ns(value, name=info.field_name)
        return value

    @model_validator(mode="after")
    def _initial_within_max(self) -> Self:
        # The max caps only the doubling, so a larger initial would still be
        # slept once in full, past the most the operator allowed.
        if self.reconnect_initial_backoff_seconds > self.reconnect_max_backoff_seconds:
            raise ValueError(
                "reconnect_initial_backoff_seconds must be at most "
                "reconnect_max_backoff_seconds, got "
                f"{self.reconnect_initial_backoff_seconds} > {self.reconnect_max_backoff_seconds}"
            )
        return self

    @property
    def ws_url(self) -> str:
        """The venue WS endpoint the ``testnet`` toggle selects."""
        return _TESTNET_WS_URL if self.testnet else _MAINNET_WS_URL

    @property
    def api_url(self) -> str:
        """The venue HTTP API base the ``testnet`` toggle selects."""
        return _TESTNET_API_URL if self.testnet else _MAINNET_API_URL
