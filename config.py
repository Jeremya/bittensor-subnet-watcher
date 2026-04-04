import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ── Required (validated at startup) ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Optional with defaults ───────────────────────────────────────────────────
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
POLL_INTERVAL_MINUTES: int = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8000"))
DB_PATH: str = os.getenv("DB_PATH", "./data/monitor.db")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ── Scoring weights ───────────────────────────────────────────────────────────
# Yield is the primary dTAO alpha signal (emission rank ÷ mcap rank arbitrage).
# Quality gates out dead subnets. Momentum confirms entry timing.
# Hype captures social traction (X followers + tweet recency).
YIELD_WEIGHT: float = 0.35
QUALITY_WEIGHT: float = 0.25
MOMENTUM_WEIGHT: float = 0.25
HYPE_WEIGHT: float = 0.15

# ── Alert thresholds ─────────────────────────────────────────────────────────
EMISSION_DIVERGENCE_RATIO: float = 1.5      # mcap_rank / emission_rank > 1.5
DEAD_GITHUB_DAYS: int = 60                   # no commit in 60 days
DEAD_GITHUB_MIN_MCAP_USD: float = 500_000.0 # only flag if mcap > $500K
WHALE_INFLOW_PCT: float = 0.05              # >5% of alpha supply staked in one poll
EMISSION_DROP_RANKS: int = 2                # lose >2 emission ranks in 24h
GITHUB_SPIKE_MULTIPLIER: float = 2.0        # stars or forks double in 24h
SOCIAL_SILENCE_DAYS: int = 14               # no tweet in 14 days
ALERT_COOLDOWN_HOURS: int = 6               # max 1 alert per subnet per type per 6h
HEALTH_CHECK_NONE_THRESHOLD: float = 0.50   # warn if >50% subnets have None emission

# ── Bittensor ────────────────────────────────────────────────────────────────
BITTENSOR_NETWORK: str = "finney"
BLOCKS_PER_DAY: int = 7200
X_SCRAPE_MAX_PER_CYCLE: int = 30            # max subnets per XCollector run
X_SCRAPE_DELAY_SECONDS: float = 2.0         # delay between X scrapes


def validate_config() -> None:
    """Fail fast at startup if required env vars are missing."""
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
               if not os.getenv(k)]
    if missing:
        print(f"[STARTUP ERROR] Missing required env vars: {', '.join(missing)}")
        sys.exit(1)
