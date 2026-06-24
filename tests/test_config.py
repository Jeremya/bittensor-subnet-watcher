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


def test_dashboard_host_defaults_to_localhost():
    import importlib
    import config

    importlib.reload(config)
    assert config.DASHBOARD_HOST == "127.0.0.1"


def test_emergence_weight_config_sums_to_one():
    import config

    total = (
        config.EMERGENCE_REG_DEMAND_WEIGHT
        + config.EMERGENCE_SLOT_FILL_WEIGHT
        + config.EMERGENCE_FLOW_ACCEL_WEIGHT
    )
    assert abs(total - 1.0) < 1e-9
    assert config.EMERGENCE_WATCH_SCORE > 0


def test_config_has_no_duplicate_module_level_definitions():
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parent.parent / "config.py"
    tree = ast.parse(source.read_text())

    names: list[str] = []
    for node in tree.body:  # module level only
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)

    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == [], f"duplicate config definitions: {duplicates}"
