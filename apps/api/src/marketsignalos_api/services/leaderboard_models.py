from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SurfaceAccountStats:
    account_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_calls: int
    wins: int
    expected_wins: float
    z_score: float


@dataclass(frozen=True, slots=True)
class AccountEnrichment:
    account_id: str
    cross_market_coordination: float
    pre_resolution_accuracy: float
    fill_intensity: float


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    account_id: str
    account_first_seen_at: str
    account_age_days: int
    resolved_calls: int
    wins: int
    losses: int
    win_rate: float
    expected_wins: float
    excess_wins: float
    stddevs_above_expected: float
    skill_likelihood: float
    insider_like_score: float
    anomaly_probability: float
    last_activity_at: str

