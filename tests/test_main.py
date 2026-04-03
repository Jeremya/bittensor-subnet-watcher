import pytest
import sys
from unittest.mock import patch, AsyncMock


def test_validate_config_called_at_startup(monkeypatch):
    """main.py must call validate_config() before anything else."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    with pytest.raises(SystemExit) as exc_info:
        import importlib
        import main
        importlib.reload(main)
    # sys.exit(1) called by validate_config
    assert exc_info.value.code == 1
