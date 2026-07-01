from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class SubnetSnapshot:
    netuid: int
    polled_at: datetime

    # Chain/price data (ChainCollector, every 15 min)
    alpha_price_tao: Optional[float] = None    # price.tao (TAO per alpha token)
    alpha_mcap_tao: Optional[float] = None     # (alpha_in + alpha_out) * price — true market cap in TAO
    alpha_mcap_usd: Optional[float] = None     # alpha_mcap_tao * tao_usd
    tao_in_tao: Optional[float] = None         # tao_in.tao — raw pool TAO reserve (used for flow calc)
    volume_24h_alpha: Optional[float] = None   # subnet_volume.tao (alpha tokens traded in 24h)
    buy_slippage_pct: Optional[float] = None   # reference TAO entry slippage (%)
    sell_slippage_pct: Optional[float] = None  # reference TAO exit slippage (%)
    tao_usd_price: Optional[float] = None      # from CoinGecko
    daily_emission_tao: Optional[float] = None  # tao_in_emission.tao * 7200
    emission_rank: Optional[int] = None        # rank by daily_emission_tao (1 = highest)
    net_tao_flow_tao: Optional[float] = None   # Δ(tao_in) − emission_accrual since prev poll
                                               # = pure net staking inflows (positive) or outflows (negative)
    n_neurons: Optional[int] = None            # SubnetInfo.subnetwork_n
    max_allowed_uids: Optional[int] = None     # SubnetInfo.max_n (capacity ceiling)
    reg_cost_tao: Optional[float] = None       # SubnetInfo.burn.tao
    owner_coldkey: Optional[str] = None

    # GitHub data (GitHubCollector, every 60 min)
    gh_last_push: Optional[datetime] = None
    gh_stars: Optional[int] = None
    gh_forks: Optional[int] = None
    gh_open_issues: Optional[int] = None

    # X/social data (XCollector, best-effort)
    x_last_tweet: Optional[datetime] = None
    x_followers: Optional[int] = None

    # Computed scores (set by scorer after collection)
    yield_score: Optional[float] = None
    health_score: Optional[float] = None
    momentum_score: Optional[float] = None
    hype_score: Optional[float] = None
    tradability_reference_tao: Optional[float] = None
    tradability_exit_slippage_pct: Optional[float] = None
    flow_score: Optional[float] = None
    relative_value_score: Optional[float] = None
    tradability_score: Optional[float] = None
    catalyst_score: Optional[float] = None
    risk_penalty: Optional[float] = None
    swing_score: Optional[float] = None
    composite_score: Optional[float] = None
    reg_demand_score: Optional[float] = None
    slot_fill_score: Optional[float] = None
    flow_accel_score: Optional[float] = None
    emergence_score: Optional[float] = None
    emergence_stage: Optional[str] = None
    price_ema_score: Optional[float] = None
    emission_value_score: Optional[float] = None
    protocol_context_score: Optional[float] = None
    spec421_score: Optional[float] = None


@dataclass
class AlertRecord:
    fired_at: datetime
    netuid: int
    subnet_name: str
    alert_type: str       # project-monitoring: 'emission_divergence' | 'dead_github' |
                          #   'emission_drop' | 'github_spike' | 'ownership_transfer' |
                          #   'new_entry'
                          # capital-protection: 'important_buy' | 'important_sell' |
                          #   'tao_outflow' | 'whale_inflow' | 'emission_near_zero' |
                          #   'liquidity_floor' | 'hyperparameter_change'
                          # watch/catalyst: 'emergence_watch' | 'analyst_mention' |
                          #   'milestone' | 'convergence'
    description: str
    current_value: Optional[float] = None
    threshold: Optional[float] = None
    notified: bool = False
    id: Optional[int] = None
