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

from collections import Counter
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tickwright.adapters.bus import KafkaBusConfig
from tickwright.adapters.feed import ReplayFeedConfig
from tickwright.adapters.paper import PaperExchangeConfig
from tickwright.adapters.store import PostgresStoreConfig, SQLiteStoreConfig
from tickwright.domain import UNATTRIBUTED, LeverageSpec, Side, SymbolOwnership
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

    @model_validator(mode="after")
    def _id_may_not_be_the_reserved_partition(self) -> Self:
        # ADR-0043 §2: the sentinel is made uncollidable rather than merely
        # conventional. Refused here and again at ``StrategyHost.register`` —
        # this is the earliest the id exists, that is the last point before it
        # keys a ledger row, and a strategy built without a config only meets
        # the second.
        if self.strategy_id == UNATTRIBUTED:
            raise ValueError(f"{UNATTRIBUTED} is the reserved unattributed partition, not an id")
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

    leverage: dict[str, LeverageSpec] = Field(default_factory=dict)
    """Per-symbol leverage & margin mode — venue-agnostic, top-level (ADR-0044 §2).

    A peer of ``strategies`` and ``engine``, deliberately **not** nested under
    ``paper`` or ``hyperliquid``: its consumer is the venue-agnostic margin
    model, which needs it on both paths, and no live run may read a paper block
    (ADR-0042 §1).

    **Sparse.** It carries only the symbols the operator named; the composition
    root resolves it against the strategy-declared set into the *complete* map
    both consumers receive, so neither can invent its own reading of an
    unconfigured symbol."""

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
        if self.exchange == "paper" and self.paper.genesis_collateral is None:
            # The paper account has no venue to ask for its opening balance, so
            # the operator states it or the run does not start (ADR-0042 §1).
            # The demand lives here, keyed on the discriminant, because this
            # validator is the only scope where ``exchange`` and the paper block
            # are both visible — a required field on ``PaperExchangeConfig``
            # would fire first and force a paper number onto a live run.
            raise ValueError(
                "exchange='paper' needs a starting collateral: "
                "set TICKWRIGHT_PAPER__GENESIS_COLLATERAL"
            )
        return self

    @model_validator(mode="after")
    def _no_two_strategies_may_share_one_id(self) -> Self:
        """ADR-0018's uniqueness gate, refused at load rather than at wiring.

        The third of this model's cross-strategy identity rules and the oldest:
        ids key seqs, snapshots, ledger partitions and ``OrderEvent`` routing,
        so two strategies sharing one silently corrupt each other's state.
        ``StrategyHost.register`` has refused it since it existed and stays the
        gate for a strategy the composition root never saw — this is the mirror
        the other two already have, and it is here for the same reason they are:
        a ``StrategyConfig`` alone cannot see a collision, this is the smallest
        scope that can, and ``build_engine`` opens the store, resolves the
        leverage book and constructs the venue before its registration loop.

        Ordered ahead of the disjointness check below because a duplicate id is
        the more fundamental fault and reads badly through the other's wording:
        two ``alpha`` entries on one symbol would otherwise be refused as
        ``alpha`` colliding with ``alpha``. Every duplicated id at once, in
        declaration order, for the reason each check here reports in full.
        """
        counts = Counter(strategy.strategy_id for strategy in self.strategies)
        duplicates = [strategy_id for strategy_id, count in counts.items() if count > 1]
        if duplicates:
            raise ValueError("duplicate strategy_id declared: " + ", ".join(duplicates))
        return self

    @model_validator(mode="after")
    def _no_two_strategies_may_declare_one_symbol(self) -> Self:
        """ADR-0034's disjointness rule, refused at load rather than at wiring.

        ``StrategyHost.register`` is the gate for a strategy the composition
        root never saw, and stays it. But a *configured* overlap is visible
        right here, and ``build_engine`` opens the store, resolves the leverage
        book and constructs the venue before it reaches the registration loop —
        so leaving this to the host alone means a typo'd ``TICKWRIGHT_STRATEGIES``
        creates a database file, or a live signing exchange, on its way to being
        refused. Two gates for one rule, the placement the reserved
        ``__unattributed__`` id already has: earliest where the value exists,
        last before it keys a ledger row.

        Every offending declaration at once, like the dead-entry check below.
        The book keeps folding past a refusal rather than stopping at the first,
        so each offender is measured against the strategies that legitimately
        own their symbols and never against another offender.

        The rule and its wording are ``SymbolOwnership``'s, not this
        validator's: the exception type is all the two gates may differ in.
        """
        ownership = SymbolOwnership()
        refusals: list[str] = []
        for strategy in self.strategies:
            refusal = ownership.refusal(strategy.strategy_id, symbols=(strategy.symbol,))
            if refusal is None:
                ownership = ownership.claim(strategy.strategy_id, symbols=(strategy.symbol,))
            else:
                refusals.append(refusal)
        if refusals:
            raise ValueError("\n".join(refusals))
        return self

    @property
    def traded_symbols(self) -> tuple[str, ...]:
        """What this process can place orders on: the strategy-declared symbols.

        Named once because two things ask it and must not answer differently
        (ADR-0044 §3): the dead-entry validator below, which rejects a
        ``leverage`` key outside this set, and the composition root's
        resolution, which completes ``leverage`` *over* it. Two comprehensions
        in two modules is how those two come to disagree about the set one
        validates and the other fills.

        Deliberately not the feed's subscription list (which may carry context
        symbols nothing trades) and not the venue's instrument universe (which
        says nothing about what is traded). Ordered by declaration, and
        duplicate-free by the validator above rather than by this comprehension:
        the dedupe is unreachable from a valid config and stays only so that the
        set is well-defined for a model built field-by-field mid-validation.
        """
        return tuple(dict.fromkeys(strategy.symbol for strategy in self.strategies))

    @model_validator(mode="after")
    def _every_leverage_entry_must_name_a_traded_symbol(self) -> Self:
        """Reject config that cannot take effect (ADR-0044 §3).

        An entry reaches the model and the venue only through a strategy that
        trades its symbol, so one naming a symbol no strategy declares is dead —
        nearly always a typo, and the dangerous kind, since the operator
        believes they raised the leverage on a symbol still running at the
        ``1x`` default. ``extra="forbid"`` above already settles that
        silently-ignored configuration is a bug here, not a convenience.

        Fires at load, before any component is built, so the root's resolution
        never meets a dead entry and neither path reaches ``start()`` carrying
        one. Every offending key at once: an operator who typo'd two learns both
        on the first run rather than one per run.
        """
        dead = sorted(set(self.leverage) - set(self.traded_symbols))
        if dead:
            raise ValueError(
                "leverage names symbols no configured strategy trades: "
                f"{', '.join(dead)} — set TICKWRIGHT_LEVERAGE to traded symbols only"
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
