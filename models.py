from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SubnetSnapshot:
    netuid: int
    polled_at: datetime

    # Chain/price data (ChainCollector, every 15 min)
    alpha_price_tao: Optional[float] = None    # price.tao
    alpha_mcap_tao: Optional[float] = None     # tao_in.tao (TAO in pool)
    alpha_mcap_usd: Optional[float] = None     # tao_in.tao * tao_usd
    volume_24h_alpha: Optional[float] = None   # subnet_volume.tao
    tao_usd_price: Optional[float] = None      # from CoinGecko
    daily_emission_tao: Optional[float] = None  # tao_in_emission.tao * 7200
    emission_rank: Optional[int] = None        # rank by daily_emission_tao (1 = highest)
    n_neurons: Optional[int] = None            # SubnetInfo.subnetwork_n
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
    quality_score: Optional[float] = None
    momentum_score: Optional[float] = None
    hype_score: Optional[float] = None
    composite_score: Optional[float] = None


@dataclass
class AlertRecord:
    fired_at: datetime
    netuid: int
    subnet_name: str
    alert_type: str       # 'emission_divergence' | 'dead_github' | 'ownership_transfer' |
                          # 'whale_inflow' | 'emission_drop' | 'github_spike' |
                          # 'social_silence' | 'new_entry'
    description: str
    current_value: Optional[float] = None
    threshold: Optional[float] = None
    notified: bool = False
    id: Optional[int] = None
