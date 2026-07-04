import asyncio
import logging
from telegram import Bot
from telegram.error import RetryAfter, NetworkError, Forbidden
from models import AlertRecord

logger = logging.getLogger(__name__)

ALERT_TYPE_EMOJI = {
    "emission_divergence": "🔔",
    "dead_github": "💀",
    "ownership_transfer": "🔄",
    "important_buy": "🟢",
    "important_sell": "🔴",
    "whale_inflow": "🐋",
    "emission_drop": "📉",
    "github_spike": "🚀",
    "social_silence": "🤫",
    "new_entry": "✨",
    "analyst_mention": "📡",
    "milestone":       "🔬",
    "convergence":     "🚨",
    "collector_stale": "🩺",
    "pump_ignition":   "🔥",
    "regime_flip":     "🌊",
}


def format_alert_message(alert: AlertRecord) -> str:
    emoji = ALERT_TYPE_EMOJI.get(alert.alert_type, "⚠️")
    type_label = alert.alert_type.replace("_", " ").title()
    lines = [
        f"{emoji} [SN{alert.netuid} — {alert.subnet_name}] {type_label}",
        alert.description,
    ]
    if alert.current_value is not None and alert.threshold is not None:
        lines.append(f"Value: {alert.current_value} / Threshold: {alert.threshold}")
    lines.append(alert.fired_at.strftime("%Y-%m-%d %H:%M UTC"))
    return "\n".join(lines)


class _BotProxy:
    """
    Thin proxy around telegram.Bot that exposes send_message and get_me
    as plain, patchable attributes so tests can use patch.object().
    """

    def __init__(self, token: str) -> None:
        self._inner = Bot(token=token)
        self.send_message = self._inner.send_message
        self.get_me = self._inner.get_me


class TelegramBot:
    def __init__(self, token: str, chat_id: str) -> None:
        self._bot = _BotProxy(token=token)
        self._chat_id = chat_id

    async def validate_token(self) -> None:
        """Raise Forbidden at startup if token is invalid."""
        await self._bot.get_me()

    async def send_alerts(self,
                           alerts: list[AlertRecord],
                           alert_ids: list[int]) -> list[int]:
        """
        Send each alert as a Telegram message.
        Returns list of alert IDs that were successfully sent.
        On RetryAfter: sleep and retry once.
        On NetworkError: skip (leave notified=0 for next poll).
        """
        sent_ids: list[int] = []
        for alert, alert_id in zip(alerts, alert_ids):
            msg = format_alert_message(alert)
            sent = await self._try_send(msg)
            if sent:
                sent_ids.append(alert_id)
            await asyncio.sleep(0.1)  # flood control
        return sent_ids

    async def _try_send(self, text: str) -> bool:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=text, parse_mode=None
            )
            return True
        except RetryAfter as exc:
            logger.warning("[TELEGRAM] rate_limited retry_after=%ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id, text=text, parse_mode=None
                )
                return True
            except Exception as e2:
                logger.error("[TELEGRAM] retry_failed error=%s", e2)
                return False
        except NetworkError as exc:
            logger.warning("[TELEGRAM] network_error error=%s", exc)
            return False
        except Exception as exc:
            logger.error("[TELEGRAM] send_failed error=%s", exc)
            return False

    async def send_health_warning(self, message: str) -> None:
        """Send an operational health warning (not an alert)."""
        await self._try_send(f"⚠️ Health Warning\n{message}")

    async def send_digest(self, text: str) -> bool:
        """Send the daily digest as a single message."""
        return await self._try_send(text)
