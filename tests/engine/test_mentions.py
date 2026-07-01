import pytest

from db.database import init_db
from engine.mentions import add_manual_mention, parse_tweet_handle


def test_parse_tweet_handle_x_com():
    assert parse_tweet_handle("https://x.com/taoanalyst/status/1234567890") == "taoanalyst"


def test_parse_tweet_handle_twitter_com_and_www():
    assert parse_tweet_handle("https://www.twitter.com/Some_Handle/status/99?s=20") == "Some_Handle"


def test_parse_tweet_handle_rejects_non_status_urls():
    assert parse_tweet_handle("https://x.com/taoanalyst") is None
    assert parse_tweet_handle("https://example.com/x.com/a/status/1") is None
    assert parse_tweet_handle("not a url") is None


@pytest.mark.asyncio
async def test_add_manual_mention_inserts_mention_and_silent_alert(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        ok = await add_manual_mention(
            db, registry={9: {"name": "IOTA"}}, netuid=9,
            tweet_url="https://x.com/analyst/status/123", tweet_text="IOTA looks strong",
        )
        assert ok is True
        cur = await db.execute("SELECT analyst_handle, notified FROM analyst_mentions")
        handle, notified = await cur.fetchone()
        assert handle == "analyst" and notified == 1
        cur = await db.execute("SELECT alert_type, netuid, notified FROM alerts")
        atype, netuid, alert_notified = await cur.fetchone()
        assert atype == "analyst_mention" and netuid == 9 and alert_notified == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_add_manual_mention_dedups_and_rejects_bad_url(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        url = "https://x.com/analyst/status/123"
        assert await add_manual_mention(db, {}, 9, url, "text") is True
        assert await add_manual_mention(db, {}, 9, url, "text") is False   # dedup
        assert await add_manual_mention(db, {}, 9, "https://x.com/analyst", "t") is False
        cur = await db.execute("SELECT COUNT(*) FROM alerts")
        assert (await cur.fetchone())[0] == 1   # no duplicate alert either
    finally:
        await db.close()
