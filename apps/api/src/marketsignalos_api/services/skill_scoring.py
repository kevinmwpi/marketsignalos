from __future__ import annotations

import math
from datetime import UTC

from marketsignalos_api.services.models import AccountEnrichment, SkilledAccountResult, SurfaceAccountStats


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clamp_01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def rank_accounts_by_skill(
    *,
    surface_stats: list[SurfaceAccountStats],
    enrichment_by_account: dict[str, AccountEnrichment],
    limit: int,
) -> list[SkilledAccountResult]:
    if not surface_stats:
        return []

    reference_now = max(stat.last_seen for stat in surface_stats)
    results: list[SkilledAccountResult] = []

    for stat in surface_stats:
        enrich = enrichment_by_account.get(stat.account_id)

        skill_score = _clamp_01(_normal_cdf(stat.z_score))
        base_insider_signal = (
            0.55 * skill_score
            + 0.25 * min(1.0, stat.resolved_calls / 80.0)
            + 0.20 * min(1.0, max(0.0, stat.z_score) / 5.0)
        )

        if enrich is not None:
            insider_like_score = _clamp_01(
                base_insider_signal
                + 0.10 * _clamp_01(enrich.cross_market_coordination)
                + 0.07 * _clamp_01(enrich.pre_resolution_accuracy)
                + 0.03 * _clamp_01(enrich.fill_intensity)
            )
        else:
            insider_like_score = base_insider_signal

        anomaly_probability = _clamp_01(1.0 - (1.0 - skill_score) ** 2)
        account_age_days = max(0, int((reference_now - stat.first_seen).total_seconds() // 86400))

        results.append(
            SkilledAccountResult(
                account_id=stat.account_id,
                account_first_seen_at=stat.first_seen.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                account_age_days=account_age_days,
                resolved_calls=stat.resolved_calls,
                wins=stat.wins,
                losses=stat.losses,
                expected_wins=round(stat.expected_wins, 4),
                excess_wins=round(stat.wins - stat.expected_wins, 4),
                win_rate=round(stat.win_rate, 4),
                stddevs_above_expected=round(stat.z_score, 4),
                skill_score=round(skill_score, 6),
                insider_like_score=round(insider_like_score, 6),
                anomaly_probability=round(anomaly_probability, 6),
                last_activity_at=stat.last_seen.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            )
        )

    results.sort(
        key=lambda row: (
            -row.insider_like_score,
            -row.skill_score,
            -row.resolved_calls,
            row.account_id,
        )
    )
    return results[:limit]
