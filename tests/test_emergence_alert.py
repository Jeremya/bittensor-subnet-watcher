from datetime import datetime, timezone

import config
from engine.alerts import check_emergence_watch
from models import SubnetSnapshot


def _snap(score, stage, mcap_usd=150_000.0):
    return SubnetSnapshot(
        netuid=42,
        polled_at=datetime.now(timezone.utc),
        emergence_score=score,
        emergence_stage=stage,
        alpha_mcap_usd=mcap_usd,
    )


def test_emergence_watch_fires_above_threshold_for_candidate():
    alert = check_emergence_watch(_snap(config.EMERGENCE_WATCH_SCORE + 5, "nascent"))
    assert alert is not None
    assert alert.alert_type == "emergence_watch"


def test_emergence_watch_silent_below_threshold():
    assert check_emergence_watch(_snap(config.EMERGENCE_WATCH_SCORE - 5, "nascent")) is None


def test_emergence_watch_skips_established():
    high = check_emergence_watch(_snap(config.EMERGENCE_WATCH_SCORE + 20, "established"))
    assert high is None


def test_emergence_watch_handles_none_score():
    assert check_emergence_watch(_snap(None, "nascent")) is None
