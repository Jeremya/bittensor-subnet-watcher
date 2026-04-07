import os
import sys
import pytest


def test_validate_config_exits_if_token_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    # Call validate_config() directly — don't reload config, which would re-run
    # load_dotenv() and restore the vars from .env before we can test.
    import config
    with pytest.raises(SystemExit) as exc_info:
        config.validate_config()
    assert exc_info.value.code == 1


def test_validate_config_passes_with_both_vars(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    import importlib
    import config
    importlib.reload(config)
    config.validate_config()  # should not raise


def test_scoring_weights_sum_to_one():
    import config
    total = config.YIELD_WEIGHT + config.HEALTH_WEIGHT + config.MOMENTUM_WEIGHT
    assert abs(total - 1.0) < 1e-9
