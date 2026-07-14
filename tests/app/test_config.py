"""The config split (issue #71): a pure model, and one env-reading skin.

``AppConfig`` is what everything except the composition root builds, and it
reads nothing ambient — neither a ``.env`` on the cwd nor an exported
``TICKWRIGHT_*`` var can reach it. ``AppSettings`` is the one class that does
read them, built only by ``__main__``, and it resolves the pydantic-settings
precedence chain: kwargs > environment > ``.env`` > class default.
"""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from tickwright.app.config import AppConfig, AppSettings

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

    config = AppConfig(replay={"path": tmp_path / "ticks.jsonl"})  # type: ignore[arg-type]

    assert config.feed == "replay"
    assert config.exchange == "paper"
    assert config.secrets() == ()


def test_app_config_rejects_an_unknown_field(tmp_path: Path) -> None:
    """A mistyped field is a loud error, not a silently dropped keyword.

    ``AppConfig`` inherited ``extra="forbid"`` from ``BaseSettings`` and must
    not lose it on the way to being a pure ``BaseModel``: the helpers here
    build a config by keyword to poke one field at a time
    (``test_build_engine._config``), so a typo would otherwise be dropped and
    leave the test asserting against a default it never chose — the same
    silently-accepted config issue #71 is about, one layer down.
    """
    (tmp_path / "ticks.jsonl").touch()

    with pytest.raises(ValidationError, match="exchagne"):
        AppConfig(  # type: ignore[call-arg]
            replay={"path": tmp_path / "ticks.jsonl"},  # type: ignore[arg-type]
            exchagne="hyperliquid",
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
