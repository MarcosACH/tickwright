"""The config split (issue #71): a pure model, and one env-reading skin.

``AppConfig`` is what everything except the composition root builds, and it
reads nothing ambient — neither a ``.env`` on the cwd nor an exported
``TICKWRIGHT_*`` var can reach it. ``AppSettings`` is the one class that does
read them, built only by ``__main__``, and it resolves the pydantic-settings
precedence chain: kwargs > environment > ``.env`` > class default.
"""

import os
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from tickwright.adapters.feed import ReplayFeedConfig
from tickwright.adapters.paper import PaperExchangeConfig
from tickwright.app.config import AppConfig, AppSettings, StrategyConfig
from tickwright.domain import UNATTRIBUTED, LeverageSpec, Side
from tickwright.venues.hyperliquid import HyperliquidConfig

_HOSTILE_ENV_FILE = (
    "TICKWRIGHT_FEED=hyperliquid\n"
    "TICKWRIGHT_EXCHANGE=hyperliquid\n"
    'TICKWRIGHT_HYPERLIQUID__SYMBOLS=["BTC"]\n'
    "TICKWRIGHT_HYPERLIQUID__SIGNING_KEY=0xdeadbeef\n"
)


def test_app_config_ignores_a_hostile_dotenv_and_exported_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pure model takes its class defaults, whatever the environment says.

    Both ambient sources outrank a class default in pydantic-settings, so this
    is the whole hermeticity contract for ``tests/app/``: a developer ``.env``
    selecting a live venue must not wire one into a test's paper engine.
    """
    (tmp_path / "ticks.jsonl").touch()
    (tmp_path / ".env").write_text(_HOSTILE_ENV_FILE)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TICKWRIGHT_FEED", "hyperliquid")
    monkeypatch.setenv("TICKWRIGHT_EXCHANGE", "hyperliquid")

    config = AppConfig(
        replay=ReplayFeedConfig(path=tmp_path / "ticks.jsonl"),
        paper=PaperExchangeConfig(genesis_collateral=Decimal("100000")),
    )

    assert config.feed == "replay"
    assert config.exchange == "paper"
    assert config.secrets() == ()


def test_app_config_rejects_an_unknown_field() -> None:
    """A mistyped field is a loud error, not a silently dropped keyword.

    ``AppConfig`` inherited ``extra="forbid"`` from ``BaseSettings`` and must
    not lose it on the way to being a pure ``BaseModel``: the helpers here
    build a config by keyword to poke one field at a time
    (``test_build_engine._config``), so a typo would otherwise be dropped and
    leave the test asserting against a default it never chose — the same
    silently-accepted config issue #71 is about, one layer down.

    ``replay`` is set so the typo is the only thing wrong with this config.
    Without it the model validator rejects the missing tick file anyway, and
    the assertion below passes on that error instead — ``ValidationError``
    renders the input dict, so even a match on the field name finds it there.
    """
    with pytest.raises(ValidationError, match=r"exchagne[\s\S]*Extra inputs are not permitted"):
        AppConfig(
            replay=ReplayFeedConfig(path=Path("ticks.jsonl")),
            exchagne="hyperliquid",  # type: ignore[call-arg]  # the subject
        )


def test_app_settings_resolves_the_documented_precedence_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kwargs > environment > ``.env`` > class default, one field per rung.

    ``__main__`` builds this class and nothing else does, so its resolution
    order is worth pinning where it can be read.

    This is the one test in the suite that reads ambient state on purpose, so
    it must own *all* of it: every ``TICKWRIGHT_*`` var goes, and the test puts
    back only the rungs it is asserting on. Controlling just the vars it sets
    would leave the rest — ``feed`` and ``store`` below — reading whatever the
    developer or CI exported.
    """
    for name in [k for k in os.environ if k.startswith("TICKWRIGHT_")]:
        monkeypatch.delenv(name)
    (tmp_path / "ticks.jsonl").touch()
    (tmp_path / ".env").write_text(
        "TICKWRIGHT_REPLAY__PATH=ticks.jsonl\n"  # nested __ resolution
        "TICKWRIGHT_STORE=postgres\n"  # only here: beats the default
        "TICKWRIGHT_BUS=in_memory\n"  # also exported: loses to it
        "TICKWRIGHT_GUARD=noop\n"  # also a kwarg: loses to it
        "TICKWRIGHT_PAPER__GENESIS_COLLATERAL=100000\n"  # the paper venue demands it
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TICKWRIGHT_BUS", "kafka")
    monkeypatch.setenv("TICKWRIGHT_GUARD", "noop")

    settings = AppSettings(guard="real")

    assert settings.guard == "real"  # kwarg beats the environment
    assert settings.bus == "kafka"  # environment beats the .env file
    assert settings.store == "postgres"  # .env file beats the class default
    assert settings.feed == "replay"  # class default, unmentioned by either
    assert settings.replay is not None and settings.replay.path == Path("ticks.jsonl")


def test_a_paper_run_without_a_genesis_collateral_is_rejected_at_load(tmp_path: Path) -> None:
    """The engine supplies no collateral of its own: a paper run reads the
    number an operator stated, or it does not start (ADR-0042 §1)."""
    (tmp_path / "ticks.jsonl").touch()

    with pytest.raises(ValidationError, match="GENESIS_COLLATERAL"):
        AppConfig(replay=ReplayFeedConfig(path=tmp_path / "ticks.jsonl"))


def test_a_live_run_is_never_asked_for_the_paper_genesis_value(tmp_path: Path) -> None:
    """The demand is keyed on the ``exchange`` discriminant, which is why it
    cannot be a required *field* on the paper block: a partial ``paper`` section
    is the ordinary case, and a required field would fire during that block's
    own validation, before any validator can see the venue (ADR-0042 §1)."""
    config = AppConfig(
        exchange="hyperliquid",
        feed="hyperliquid",
        hyperliquid=HyperliquidConfig(symbols=["BTC"], signing_key=SecretStr("0xdeadbeef")),
        # One paper variable is enough to construct the block — the shape that
        # would trip a required field on a run that has no paper venue at all.
        paper=PaperExchangeConfig(fill_model="immediate"),
    )

    assert config.paper.genesis_collateral is None


def test_a_non_positive_paper_genesis_is_a_typo_not_a_scenario() -> None:
    """An account cannot be *created* owing money (ADR-0042 §1). This is input
    validation, not margin enforcement — derived free margin still goes
    negative freely at runtime."""
    with pytest.raises(ValidationError):
        PaperExchangeConfig(genesis_collateral=Decimal("0"))


@pytest.mark.parametrize("label", ["Main", "paper-main", "a" * 33, ""])
def test_the_paper_account_label_is_slug_constrained(label: str) -> None:
    """No hyphen, so ``paper-<label>`` stays unambiguously two segments against
    a live id's three (ADR-0042 §5)."""
    with pytest.raises(ValidationError):
        PaperExchangeConfig(genesis_collateral=Decimal("1000"), account_label=label)


def test_a_leverage_entry_for_an_untraded_symbol_is_rejected_at_load(tmp_path: Path) -> None:
    """Dead config must not silently misrepresent the book (ADR-0044 §3).

    An entry only takes effect through a strategy that trades its symbol, so one
    naming a symbol no configured strategy declares is nearly always a typo —
    and the dangerous kind: the operator believes they set ``5x`` while the
    symbol they meant trades at the ``1x`` default. ``extra="forbid"`` already
    establishes that silently-ignored configuration is a bug here rather than a
    convenience.

    Rejected at *load*, before any component is built, so the composition root's
    resolution never meets a dead entry and neither path reaches ``start()``
    carrying one. **Every** offending key is named at once, so an operator who
    typo'd two symbols learns both on the first run rather than one per run.
    """
    (tmp_path / "ticks.jsonl").touch()

    with pytest.raises(ValidationError, match="ETH.*SOL|SOL.*ETH"):
        AppConfig(
            replay=ReplayFeedConfig(path=tmp_path / "ticks.jsonl"),
            paper=PaperExchangeConfig(genesis_collateral=Decimal("100000")),
            strategies=[
                StrategyConfig(
                    kind="single_shot_market",
                    strategy_id="demo",
                    symbol="BTC",
                    side=Side.BUY,
                    quantity=Decimal("0.5"),
                )
            ],
            leverage={
                "BTC": LeverageSpec(mode="cross", leverage=5),
                "ETH": LeverageSpec(leverage=3),
                "SOL": LeverageSpec(leverage=2),
            },
        )


def test_the_traded_symbol_set_is_derived_once_for_both_readers(tmp_path: Path) -> None:
    """Two things ask "what does this process trade" and must not disagree.

    The dead-entry validator rejects a ``leverage`` key *outside* this set while
    the composition root's ``resolve_leverage`` completes ``leverage`` *over* it
    — the same set read for opposite purposes, so two comprehensions in two
    modules is exactly how one comes to admit a key the other drops. It is the
    strategy-declared set (ADR-0044 §3), deliberately not the feed's
    subscription list, which may carry context symbols nothing trades.

    In declaration order, and over distinct symbols because the model refuses
    anything else: ADR-0034's disjointness rule is a load-time validator, so a
    duplicate is unreachable here rather than deduped here.
    """
    (tmp_path / "ticks.jsonl").touch()

    config = AppConfig(
        replay=ReplayFeedConfig(path=tmp_path / "ticks.jsonl"),
        paper=PaperExchangeConfig(genesis_collateral=Decimal("100000")),
        hyperliquid=HyperliquidConfig(symbols=["BTC", "ETH", "SOL"]),
        strategies=[
            StrategyConfig(
                kind="single_shot_market",
                strategy_id=strategy_id,
                symbol=symbol,
                side=Side.BUY,
                quantity=Decimal("0.5"),
            )
            for strategy_id, symbol in (("eth", "ETH"), ("btc", "BTC"), ("sol", "SOL"))
        ],
    )

    assert config.traded_symbols == ("ETH", "BTC", "SOL")


def test_a_strategy_may_not_claim_the_reserved_unattributed_id() -> None:
    """``__unattributed__`` is the ledger's foreign-flow partition, not a name.

    The sentinel is what the store writes where the in-memory partition key is
    ``None`` (ADR-0043 §2), so a strategy legitimately called that would have
    its book merged with flow the engine never placed — silently, and on the
    key every Σ is folded over. Refused at config load, which is the earliest
    the id exists; ``StrategyHost.register`` refuses it again for the strategies
    that never come through a config.
    """
    with pytest.raises(ValidationError, match=UNATTRIBUTED):
        StrategyConfig(
            kind="single_shot_market",
            strategy_id=UNATTRIBUTED,
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("0.5"),
        )


def test_two_strategies_may_not_declare_one_symbol(tmp_path: Path) -> None:
    """ADR-0034's disjointness rule, refused before anything is built.

    ``StrategyHost.register`` is where the rule is enforced for a strategy the
    composition root never saw, but a *configured* overlap is visible here — and
    ``build_engine`` opens the store, resolves leverage and constructs the venue
    before it reaches the registration loop, so leaving it to the host means a
    typo'd `TICKWRIGHT_STRATEGIES` creates a database file (or a live signing
    exchange) on its way to being refused. The same two-gate placement the
    reserved ``__unattributed__`` id already has: earliest where the value
    exists, last before it keys a ledger row.

    Every offender at once, like the leverage dead-entry check one validator
    over: an operator who overlapped two pairs learns both on this run.
    """
    (tmp_path / "ticks.jsonl").touch()

    def _strategy(strategy_id: str, symbol: str) -> StrategyConfig:
        return StrategyConfig(
            kind="single_shot_market",
            strategy_id=strategy_id,
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("0.5"),
        )

    with pytest.raises(ValidationError) as refused:
        AppConfig(
            replay=ReplayFeedConfig(path=tmp_path / "ticks.jsonl"),
            paper=PaperExchangeConfig(genesis_collateral=Decimal("100000")),
            strategies=[
                _strategy("alpha", "BTC"),
                _strategy("beta", "BTC"),
                _strategy("gamma", "ETH"),
                _strategy("delta", "ETH"),
            ],
        )

    message = str(refused.value)
    assert "beta declares" in message and "BTC (owned by alpha)" in message
    assert "delta declares" in message and "ETH (owned by gamma)" in message
    assert "separate account" in message


def test_two_strategies_may_not_share_one_id(tmp_path: Path) -> None:
    """``StrategyHost``'s oldest fail-fast, mirrored where the ids are all visible.

    A duplicate ``strategy_id`` silently corrupts both strategies: ids key seqs,
    snapshots, ledger partitions and ``OrderEvent`` routing (ADR-0018). The
    registry has refused it since it existed, but only at registration — and
    ``build_engine`` opens the store, resolves leverage and constructs the venue
    before its registration loop, so the same typo that ADR-0034's disjointness
    rule now catches at load was still paying for a database file (or a live
    signing exchange) when the collision was in the id rather than the symbol.
    A ``StrategyConfig`` cannot see it alone; this is the smallest scope that
    can.

    Distinct symbols on purpose, so the refusal can only be about the id.
    """
    (tmp_path / "ticks.jsonl").touch()

    def _strategy(strategy_id: str, symbol: str) -> StrategyConfig:
        return StrategyConfig(
            kind="single_shot_market",
            strategy_id=strategy_id,
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("0.5"),
        )

    with pytest.raises(ValidationError) as refused:
        AppConfig(
            replay=ReplayFeedConfig(path=tmp_path / "ticks.jsonl"),
            paper=PaperExchangeConfig(genesis_collateral=Decimal("100000")),
            strategies=[
                _strategy("alpha", "BTC"),
                _strategy("alpha", "ETH"),
                _strategy("beta", "SOL"),
                _strategy("beta", "DOGE"),
            ],
        )

    message = str(refused.value)
    assert "alpha" in message and "beta" in message
    assert "duplicate" in message


def test_a_duplicate_id_on_one_symbol_is_refused_as_a_duplicate_id(tmp_path: Path) -> None:
    """The doubly-faulty declaration is reported as the fault that explains it.

    ``[alpha/BTC, alpha/BTC]`` breaks both cross-strategy rules at once, and
    which one it is refused as is a decision rather than an accident: pydantic
    runs ``mode="after"`` validators in definition order, so the id check
    sitting above the disjointness check in ``config.py`` is the whole of the
    guarantee. Read through the disjointness wording the same config refuses as
    ``alpha`` colliding with ``alpha`` and prescribes a separate account for it,
    which is nonsense — there is one strategy, declared twice.

    The negative assertion is what actually pins the ordering; the positive one
    passes under either order, because a duplicate id is refused either way.
    Reordering the two validators, or landing a third between them, fails here.
    """
    (tmp_path / "ticks.jsonl").touch()

    def _strategy(strategy_id: str, symbol: str) -> StrategyConfig:
        return StrategyConfig(
            kind="single_shot_market",
            strategy_id=strategy_id,
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("0.5"),
        )

    with pytest.raises(ValidationError) as refused:
        AppConfig(
            replay=ReplayFeedConfig(path=tmp_path / "ticks.jsonl"),
            paper=PaperExchangeConfig(genesis_collateral=Decimal("100000")),
            strategies=[_strategy("alpha", "BTC"), _strategy("alpha", "BTC")],
        )

    message = str(refused.value)
    assert "duplicate strategy_id declared: alpha" in message
    assert "owned by alpha" not in message
    assert "separate account" not in message
