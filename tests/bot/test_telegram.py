import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from models import AlertRecord
from bot.telegram import TelegramBot, format_alert_message


def make_alert(alert_type: str = "emission_divergence") -> AlertRecord:
    return AlertRecord(
        fired_at=datetime.now(timezone.utc),
        netuid=42,
        subnet_name="Chutes",
        alert_type=alert_type,
        description="Emission rank #3 / MCap rank #18 → ratio 6.0x",
        current_value=6.0,
        threshold=1.5,
    )


def test_format_alert_message():
    alert = make_alert()
    msg = format_alert_message(alert)
    assert "SN42" in msg
    assert "Chutes" in msg
    assert "6.0" in msg
    assert "1.5" in msg


async def test_send_alerts_happy_path():
    bot = TelegramBot(token="fake_token", chat_id="123")
    alerts = [make_alert(), make_alert("dead_github")]

    with patch.object(bot._bot, "send_message", AsyncMock(return_value=MagicMock())):
        sent_ids = await bot.send_alerts(alerts, alert_ids=[1, 2])

    assert sent_ids == [1, 2]


async def test_send_alerts_retry_after():
    from telegram.error import RetryAfter
    bot = TelegramBot(token="fake_token", chat_id="123")
    alerts = [make_alert()]

    call_count = 0
    async def flaky_send(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RetryAfter(1)
        return MagicMock()

    with patch.object(bot._bot, "send_message", flaky_send):
        with patch("bot.telegram.asyncio.sleep", AsyncMock()):
            sent_ids = await bot.send_alerts(alerts, alert_ids=[10])

    assert sent_ids == [10]
    assert call_count == 2


async def test_send_alerts_network_error_skips():
    from telegram.error import NetworkError
    bot = TelegramBot(token="fake_token", chat_id="123")
    alerts = [make_alert()]

    with patch.object(bot._bot, "send_message",
                      AsyncMock(side_effect=NetworkError("unreachable"))):
        sent_ids = await bot.send_alerts(alerts, alert_ids=[5])

    assert sent_ids == []  # not sent, notified=0 stays for next poll
