"""The typed application config (ADR-0021/0032).

``AppConfig`` is the one settings object the CLI reads — from the environment
and ``.env`` — composed of the per-adapter ``*Config``s that live in their own
packages. The ``Literal`` discriminants below are the whole selection surface:
an unknown value fails validation with a readable error before any wiring
happens, and adding an impl widens exactly one ``Literal`` plus one ``match``
arm in ``build.py``.
"""

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tickwright.adapters.bus import KafkaBusConfig
from tickwright.adapters.feed import ReplayFeedConfig
from tickwright.adapters.paper import PaperExchangeConfig
from tickwright.adapters.store import PostgresStoreConfig, SQLiteStoreConfig
from tickwright.domain import Side
from tickwright.engine.runner import EngineConfig
from tickwright.venues.hyperliquid import HyperliquidConfig


class StrategyConfig(BaseModel):
    """One hosted strategy: which reference impl, its identity, and its order."""

    kind: Literal["single_shot_market", "single_shot_limit"]
    strategy_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal | None = None

    @model_validator(mode="after")
    def _limit_needs_a_price(self) -> Self:
        if self.kind == "single_shot_limit" and self.price is None:
            raise ValueError("a single_shot_limit strategy needs a price")
        return self


class AppConfig(BaseSettings):
    """Everything the composition root needs, from env vars or ``.env``.

    Nested fields use ``__`` in the environment, e.g.
    ``TICKWRIGHT_REPLAY__PATH=ticks.jsonl`` or
    ``TICKWRIGHT_ENGINE__SHUTDOWN_TIMEOUT_SECONDS=5``.
    """

    model_config = SettingsConfigDict(
        env_prefix="TICKWRIGHT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # The seam discriminants (ADR-0032): each names the impl for one Protocol.
    bus: Literal["in_memory", "kafka"] = "in_memory"
    store: Literal["sqlite", "postgres"] = "sqlite"
    exchange: Literal["paper", "hyperliquid"] = "paper"
    feed: Literal["replay", "hyperliquid"] = "replay"
    guard: Literal["real", "noop"] = "real"

    # The per-adapter configs (each defined in its adapter's package).
    sqlite: SQLiteStoreConfig = SQLiteStoreConfig()
    postgres: PostgresStoreConfig = PostgresStoreConfig()
    replay: ReplayFeedConfig | None = None
    hyperliquid: HyperliquidConfig = HyperliquidConfig()
    paper: PaperExchangeConfig = PaperExchangeConfig()
    kafka: KafkaBusConfig = KafkaBusConfig()

    strategies: list[StrategyConfig] = Field(default_factory=list)
    engine: EngineConfig = EngineConfig()

    @model_validator(mode="after")
    def _the_selected_feed_needs_its_config(self) -> Self:
        if self.feed == "replay" and self.replay is None:
            raise ValueError("feed='replay' needs a tick file: set TICKWRIGHT_REPLAY__PATH")
        if self.feed == "hyperliquid" and not self.hyperliquid.symbols:
            raise ValueError(
                "feed='hyperliquid' needs at least one symbol: set TICKWRIGHT_HYPERLIQUID__SYMBOLS"
            )
        if self.exchange == "hyperliquid" and self.hyperliquid.signing_key is None:
            # The paper default needs no key at all (ADR-0021); only the live
            # write path signs.
            raise ValueError(
                "exchange='hyperliquid' needs a signing key: "
                "set TICKWRIGHT_HYPERLIQUID__SIGNING_KEY"
            )
        return self

    def secrets(self) -> tuple[str, ...]:
        """Every secret value this config carries, for log redaction (ADR-0020).

        The config is the one place that knows which of its fields are key
        material, so it owns the inventory the CLI hands ``configure_logging``.
        The default paper path carries none.
        """
        key = self.hyperliquid.signing_key
        return (key.get_secret_value(),) if key is not None else ()
