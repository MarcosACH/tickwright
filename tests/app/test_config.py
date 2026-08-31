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
from tickwright.domain import LeverageSpec, Side
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

    Duplicate-free and in declaration order: ``StrategyHost`` refuses two
    strategies over one symbol, but that fires at registration and this is read
    at config load, ahead of it.
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
            for strategy_id, symbol in (("eth", "ETH"), ("btc", "BTC"), ("btc-2", "BTC"))
        ],
    )

    assert config.traded_symbols == ("ETH", "BTC")
