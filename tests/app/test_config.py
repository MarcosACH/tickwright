"""The config split (issue #71): a pure model, and one env-reading skin.

``AppConfig`` is what everything except the composition root builds, and it
reads nothing ambient — neither a ``.env`` on the cwd nor an exported
``TICKWRIGHT_*`` var can reach it. ``AppSettings`` is the one class that does
read them, built only by ``__main__``, and it resolves the pydantic-settings
precedence chain: kwargs > environment > ``.env`` > class default.
"""

from pathlib import Path

import pytest

from tickwright.app.config import AppConfig

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
