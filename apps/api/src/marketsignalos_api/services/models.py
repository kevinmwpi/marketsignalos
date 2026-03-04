from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class FillRecord:
    source: str
    account_id: str
    market_ticker: str
    fill_id: str
    trade_id: str
    side: str
    price: float
    quantity: int
    traded_at: datetime


@dataclass(frozen=True, slots=True)
class ResolutionRecord:
    source: str
    market_ticker: str
    resolved_at: datetime
    settlement_side: str


@dataclass(frozen=True, slots=True)
class SurfaceAccountStats:
    account_id: str
    first_seen: datetime
    last_seen: datetime
    resolved_calls: int
    wins: int
    losses: int
    expected_wins: float
    win_rate: float
    z_score: float


@dataclass(frozen=True, slots=True)
class AccountEnrichment:
    account_id: str
    as_of: datetime
    profile_confidence: float
    cross_market_coordination: float
    pre_resolution_accuracy: float
    fill_intensity: float


@dataclass(frozen=True, slots=True)
class SkilledAccountResult:
    account_id: str
    account_first_seen_at: str
    account_age_days: int
    resolved_calls: int
    wins: int
    losses: int
    expected_wins: float
    excess_wins: float
    win_rate: float
    stddevs_above_expected: float
    skill_score: float
    insider_like_score: float
    anomaly_probability: float
    last_activity_at: str
