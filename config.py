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
# Snapshots needed to cover MOMENTUM_HISTORY_DAYS at the configured poll rate.
# 10% buffer absorbs missed polls without silently shrinking the window.
MOMENTUM_HISTORY_DAYS: int = 7
MOMENTUM_HISTORY_LIMIT: int = int(MOMENTUM_HISTORY_DAYS * 24 * 60 / POLL_INTERVAL_MINUTES * 1.1)
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8000"))
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DB_PATH: str = os.getenv("DB_PATH", "./data/monitor.db")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ── Scoring weights ───────────────────────────────────────────────────────────
# Yield is the primary dTAO alpha signal (emission rank ÷ mcap rank arbitrage).
# Health measures protocol-native subnet stability (ownership, reg cost, GitHub, liquidity).
# Momentum is net TAO inflow direction — the actual emission share driver.
# Hype (X followers/recency) is intentionally excluded from composite: it is
# gameable, protocol-external, and displayed as informational on the detail page.
YIELD_MIN_MCAP_USD: float = 50_000.0  # exclude micro-caps from yield scoring
YIELD_WEIGHT: float = 0.40
HEALTH_WEIGHT: float = 0.30
MOMENTUM_WEIGHT: float = 0.30

# ── Alert thresholds ─────────────────────────────────────────────────────────
# Project-monitoring alerts
EMISSION_DIVERGENCE_RATIO: float = 1.5      # mcap_rank / emission_rank > 1.5
DEAD_GITHUB_DAYS: int = 60                   # no commit in 60 days
DEAD_GITHUB_MIN_MCAP_USD: float = 500_000.0 # only flag dead GitHub if mcap > $500K
EMISSION_DROP_RANKS: int = 2                # lose >2 emission ranks in 24h
GITHUB_SPIKE_MULTIPLIER: float = 2.0        # stars or forks double in 24h
SOCIAL_SILENCE_DAYS: int = 14               # no tweet in 14 days
# Capital-protection alerts
WHALE_INFLOW_PCT: float = 0.05              # net TAO inflow > 5% of pool in one poll
NET_OUTFLOW_ALERT_PCT: float = 0.03         # net TAO outflow > 3% of pool in one poll
EMISSION_NEAR_ZERO_TAO: float = 5.0         # daily emission < 5 τ/day = near-zero risk
EMISSION_NEAR_ZERO_MIN_MCAP_USD: float = 100_000.0  # only alert above this mcap
LIQUIDITY_FLOOR_RATIO: float = 0.001        # < 0.1% daily turnover = effectively illiquid
LIQUIDITY_MIN_MCAP_USD: float = 200_000.0   # only alert above this mcap
REG_COST_CHANGE_PCT: float = 0.50           # reg cost moves ±50% = hyperparameter shift
# Shared
ALERT_COOLDOWN_HOURS: int = 6               # max 1 alert per subnet per type per 6h
HEALTH_CHECK_NONE_THRESHOLD: float = 0.50   # warn if >50% subnets have None emission

# ── Portfolio tracking ────────────────────────────────────────────────────────
WALLET_COLDKEYS: list[str] = [k.strip() for k in os.getenv("WALLET_COLDKEYS", "").split(",") if k.strip()]
WALLET_LABELS: list[str] = [l.strip() for l in os.getenv("WALLET_LABELS", "").split(",") if l.strip()]

# ── Bittensor ────────────────────────────────────────────────────────────────
BITTENSOR_NETWORK: str = "finney"
BLOCKS_PER_DAY: int = 7200
X_SCRAPE_MAX_PER_CYCLE: int = 30            # max subnets per XCollector run
X_SCRAPE_DELAY_SECONDS: float = 2.0         # delay between X scrapes

# ── Analyst tracking ──────────────────────────────────────────────────────────
ANALYST_HANDLES: list[str] = [
    h.strip() for h in os.getenv("ANALYST_HANDLES", "").split(",") if h.strip()
]
ANALYST_TWEET_LOOKBACK_HOURS: int = 25   # slightly > poll interval to avoid gaps
ANALYST_COVERAGE_DECAY_HOURS: int = 72   # coverage badge visible for 72h after mention
MAX_ANALYST_HANDLES: int = int(os.getenv("MAX_ANALYST_HANDLES", "20"))  # scrape cap

# ── AI Signal Interpreter ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
AI_INTERPRETER_MODEL: str = "claude-haiku-4-5-20251001"

# ── Signal Convergence ────────────────────────────────────────────────────────
CONVERGENCE_SIGNAL_WINDOW_HOURS: int = 24   # look back this many hours for signal grouping
CONVERGENCE_MIN_SIGNALS: int = 2            # distinct signal types needed to fire
CONVERGENCE_COOLDOWN_HOURS: int = 48        # separate cooldown from standard 6h

# ── Portfolio recommendations ────────────────────────────────────────────────
PORTFOLIO_RECOMMENDATION_WINDOW_HOURS: int = 168   # 1 week
PORTFOLIO_TRIM_MAX_ALLOC_PCT: float = 0.25         # >25% single-name concentration
PORTFOLIO_CATEGORY_MAX_ALLOC_PCT: float = 0.45     # >45% category concentration blocks new adds
PORTFOLIO_ADD_MIN_SCORE: float = 75.0
PORTFOLIO_NEW_BUY_MIN_SCORE: float = 78.0
PORTFOLIO_REPLACE_SCORE_MARGIN: float = 8.0
PORTFOLIO_HOLD_FLOOR_SCORE: float = 55.0

# ── Calibration gate ───────────────────────────────────────────────────────────
# The swing model has not been validated against forward returns yet (swing_score
# has no persisted history — see scripts/backtest_signals.py). Until it is, buy-side
# recommendations are flagged as unvalidated and confidence is capped. The first
# backtest of the legacy composite_score showed scores above ~80 mean-revert over
# 14d, so buy-side cards at/above SWING_EXTENDED_SCORE carry an explicit caution.
SWING_SIGNAL_VALIDATED: bool = False
SWING_EXTENDED_SCORE: float = 80.0

# ── Tradability scoring ──────────────────────────────────────────────────────
TRADABILITY_REFERENCE_TAO: float = 5.0           # reference swing trade size
TRADABILITY_MAX_SLIPPAGE_PCT: float = 8.0        # beyond this, new buys are blocked


def validate_config() -> None:
    """Fail fast at startup if required env vars are missing."""
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
               if not os.getenv(k)]
    if missing:
        print(f"[STARTUP ERROR] Missing required env vars: {', '.join(missing)}")
        sys.exit(1)
