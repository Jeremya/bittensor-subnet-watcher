import re
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from db.database import insert_analyst_mention, insert_alert
from engine.alerts import _registry_name
from models import AlertRecord

_TWEET_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/\d+",
    re.IGNORECASE,
)


def parse_tweet_handle(url: str) -> Optional[str]:
    """Extract the author handle from an x.com/twitter.com status URL, else None."""
    m = _TWEET_URL_RE.match(url.strip())
    return m.group(1) if m else None


async def add_manual_mention(db: aiosqlite.Connection,
                             registry: dict,
                             netuid: int,
                             tweet_url: str,
                             tweet_text: str) -> bool:
    """Store a hand-curated tweet as an analyst mention.

    Both the mention and its alert row are inserted pre-notified: the user just
    typed this, so Telegram must not echo it back — but convergence and catalyst
    scoring read these rows and should see them.
    """
    handle = parse_tweet_handle(tweet_url)
    if handle is None:
        return False
    url = tweet_url.strip()
    now = datetime.now(timezone.utc)
    inserted = await insert_analyst_mention(
        db, handle, netuid, url, tweet_text.strip(), now, notified=True
    )
    if not inserted:
        return False
    text_preview = tweet_text.strip()[:120]
    await insert_alert(db, AlertRecord(
        fired_at=now,
        netuid=netuid,
        subnet_name=_registry_name(registry, netuid),
        alert_type="analyst_mention",
        description=f"@{handle} (curated): \"{text_preview}\"\n→ {url}",
        current_value=None,
        threshold=None,
        notified=True,
    ))
    return True
