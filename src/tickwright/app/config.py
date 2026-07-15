"""The typed application config (ADR-0021/0032).

Two classes, one shape. ``AppConfig`` is the pure model ``build_engine`` takes,
composed of the per-adapter ``*Config``s that live in their own packages; it
reads nothing ambient. ``AppSettings`` subclasses it with the env/``.env``
reading the CLI needs, and ``__main__`` is its only builder — everything else,
tests included, builds the pure type (issue #71).

The ``Literal`` discriminants below are the whole selection surface: an unknown
value fails validation with a readable error before any wiring happens, and
adding an impl widens exactly one ``Literal`` plus one ``match`` arm in
``build.py``.
"""

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
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


class AppConfig(BaseModel):
    """Everything the composition root needs. Pure: reads nothing ambient.

    Deliberately *not* a ``BaseSettings``. Both ambient sources — an exported
    ``TICKWRIGHT_*`` var and a ``.env`` on the cwd — outrank a class default in
    the pydantic-settings chain, so a config class that reads them cannot also
    be the class tests build by hand: a developer ``.env`` selecting a live
    venue would silently wire one into a paper test (issue #71). Env-reading
    lives in ``AppSettings`` alone, and this class has no machinery to do it.
    """

    # Dropping BaseSettings drops the strictness it carried, and only the
    # env-reading was meant to go: a plain BaseModel ignores unknown fields,
    # which would silently swallow a mistyped keyword.
    model_config = ConfigDict(extra="forbid")

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


class AppSettings(AppConfig, BaseSettings):
    """The one env-reading skin, built only by ``__main__`` (the CLI entry).

    An ``AppConfig`` in every way that matters — ``build_engine`` takes the
    pure type and this satisfies it — plus the ability to resolve fields from
    the environment and ``.env``. Nested fields use ``__``, e.g.
    ``TICKWRIGHT_REPLAY__PATH=ticks.jsonl`` or
    ``TICKWRIGHT_ENGINE__SHUTDOWN_TIMEOUT_SECONDS=5``.

    This is the *only* legitimate place a config reads ambient state. It stays
    internal to ``tickwright.app`` and out of ``__all__`` on purpose: exporting
    it would advertise a second env-reading entry point, which is the bug class
    issue #71 closed.
    """

    model_config = SettingsConfigDict(
        env_prefix="TICKWRIGHT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
    )
