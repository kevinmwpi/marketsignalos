from __future__ import annotations

import math
from datetime import UTC

from marketsignalos_api.services.leaderboard_models import (
    AccountEnrichment,
    LeaderboardEntry,
    SurfaceAccountStats,
)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _clamp_01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def rank_accounts_by_skill(
    *,
    surface: list[SurfaceAccountStats],
    enrichment: dict[str, AccountEnrichment],
    limit: int,
) -> list[LeaderboardEntry]:
    if not surface:
        return []

    reference_now = max(row.last_seen_at for row in surface)
    rows: list[LeaderboardEntry] = []

    for account in surface:
        details = enrichment.get(account.account_id)
        skill_likelihood = _clamp_01(_normal_cdf(account.z_score))

        enrichment_score = 0.0
        if details:
            enrichment_score = _clamp_01(
                (details.cross_market_coordination + details.pre_resolution_accuracy + details.fill_intensity)
                / 3.0
            )

        insider_like_score = _clamp_01((0.8 * skill_likelihood) + (0.2 * enrichment_score))
        anomaly_probability = _clamp_01(skill_likelihood)
        losses = account.resolved_calls - account.wins
        account_age_days = max(0, int((reference_now - account.first_seen_at).total_seconds() // 86400))

        rows.append(
            LeaderboardEntry(
                account_id=account.account_id,
                account_first_seen_at=account.first_seen_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                account_age_days=account_age_days,
                resolved_calls=account.resolved_calls,
                wins=account.wins,
                losses=losses,
                win_rate=round(account.wins / account.resolved_calls, 4),
                expected_wins=round(account.expected_wins, 4),
                excess_wins=round(account.wins - account.expected_wins, 4),
                stddevs_above_expected=round(account.z_score, 4),
                skill_likelihood=round(skill_likelihood, 6),
                insider_like_score=round(insider_like_score, 6),
                anomaly_probability=round(anomaly_probability, 6),
                last_activity_at=account.last_seen_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            )
        )

    rows.sort(
        key=lambda row: (
            -row.insider_like_score,
            -row.skill_likelihood,
            -row.resolved_calls,
            row.account_id,
        )
    )
    return rows[:limit]

