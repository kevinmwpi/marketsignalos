from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from marketsignalos_api.services.account_enrichment_ingestion import collect_account_enrichment
from marketsignalos_api.services.account_surface_ingestion import collect_surface_account_stats
from marketsignalos_api.services.leaderboard_models import LeaderboardEntry
from marketsignalos_api.services.orderflow_analytics import OrderflowAnomaly, detect_orderflow_anomalies
from marketsignalos_api.services.skill_scoring import rank_accounts_by_skill


SignalType = Literal["skilled_account", "orderflow_dislocation"]
DriverTone = Literal["positive", "neutral", "risk"]


class OpportunityDriver(BaseModel):
    label: str
    value: str
    contribution: float = Field(ge=0.0, le=1.0)
    tone: DriverTone


class OpportunitySignal(BaseModel):
    signal_id: str
    signal_type: SignalType
    source: str
    entity: str
    market_ticker: str | None
    direction: str | None
    title: str
    thesis: str
    probability: float = Field(ge=0.0, le=1.0)
    opportunity_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    expected_value_basis: str
    observed_at: str
    drivers: list[OpportunityDriver]
    next_step: str


class OpportunitySummary(BaseModel):
    candidate_count: int
    skilled_account_count: int
    orderflow_event_count: int
    top_probability: float
    top_opportunity_score: float
    freshness_window_days: int


class OpportunityDashboard(BaseModel):
    generated_at: str
    summary: OpportunitySummary
    opportunities: list[OpportunitySignal]


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _clamp_01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_score(value: float) -> str:
    return f"{value * 100:.1f}%"


def _format_points(value: float) -> str:
    return f"{value:.2f} pts"


def _account_opportunity(entry: LeaderboardEntry) -> OpportunitySignal:
    resolved_calls = entry.resolved_calls
    z_score = entry.stddevs_above_expected
    skill_likelihood = entry.skill_likelihood
    insider_like_score = entry.insider_like_score
    excess_wins = entry.excess_wins
    account_id = entry.account_id
    observed_at = entry.last_activity_at

    sample_strength = _clamp_01(resolved_calls / 75.0)
    z_strength = _clamp_01(max(z_score, 0.0) / 4.0)
    confidence = _clamp_01(
        0.35 + (0.25 * sample_strength) + (0.25 * z_strength) + (0.15 * insider_like_score)
    )
    opportunity_score = _clamp_01(
        (0.58 * skill_likelihood) + (0.24 * insider_like_score) + (0.18 * confidence)
    )

    return OpportunitySignal(
        signal_id=f"account:{account_id}",
        signal_type="skilled_account",
        source="kalshi",
        entity=account_id,
        market_ticker=None,
        direction=None,
        title=f"Statistically advantaged account {account_id}",
        thesis=(
            f"{account_id} is {z_score:.2f} standard deviations above expected outcomes "
            f"across {resolved_calls} resolved calls, with {excess_wins:.1f} excess wins."
        ),
        probability=round(skill_likelihood, 6),
        opportunity_score=round(opportunity_score, 6),
        confidence=round(confidence, 6),
        expected_value_basis=(
            "Skill-likelihood edge plus enrichment confluence; validate future positions "
            "against liquidity, timing, and independent market context."
        ),
        observed_at=observed_at,
        drivers=[
            OpportunityDriver(
                label="skill likelihood",
                value=_format_score(skill_likelihood),
                contribution=round(skill_likelihood, 6),
                tone="positive",
            ),
            OpportunityDriver(
                label="z-score",
                value=f"{z_score:.2f} sigma",
                contribution=round(z_strength, 6),
                tone="positive" if z_score > 0 else "neutral",
            ),
            OpportunityDriver(
                label="sample",
                value=f"{resolved_calls} resolved",
                contribution=round(sample_strength, 6),
                tone="neutral",
            ),
            OpportunityDriver(
                label="enrichment",
                value=_format_score(insider_like_score),
                contribution=round(insider_like_score, 6),
                tone="positive",
            ),
        ],
        next_step="Monitor this account's next fills and compare entry price with current fair value.",
    )


def _anomaly_probability(anomaly: OrderflowAnomaly) -> float:
    if anomaly.anomaly_type == "size_outlier":
        return _clamp_01(_normal_cdf(anomaly.anomaly_score))
    if anomaly.anomaly_type == "odds_jump":
        return _clamp_01(0.55 + (min(anomaly.anomaly_score, 45.0) / 90.0))
    if anomaly.anomaly_type == "large_bet":
        return _clamp_01(0.5 + (0.45 * (1.0 - math.exp(-anomaly.anomaly_score / 500.0))))
    return 0.5


def _anomaly_driver(anomaly: OrderflowAnomaly) -> OpportunityDriver:
    probability = _anomaly_probability(anomaly)
    if anomaly.anomaly_type == "size_outlier":
        return OpportunityDriver(
            label="volume z-score",
            value=f"{anomaly.anomaly_score:.2f} sigma",
            contribution=round(probability, 6),
            tone="positive",
        )
    if anomaly.anomaly_type == "odds_jump":
        return OpportunityDriver(
            label="price displacement",
            value=_format_points(anomaly.anomaly_score),
            contribution=round(probability, 6),
            tone="positive",
        )
    if anomaly.anomaly_type == "large_bet":
        return OpportunityDriver(
            label="large quantity",
            value=f"{int(anomaly.anomaly_score)} contracts",
            contribution=round(probability, 6),
            tone="neutral",
        )
    return OpportunityDriver(
        label=anomaly.anomaly_type.replace("_", " "),
        value=f"{anomaly.anomaly_score:.2f}",
        contribution=round(probability, 6),
        tone="neutral",
    )


def _orderflow_opportunities(anomalies: list[OrderflowAnomaly]) -> list[OpportunitySignal]:
    grouped: dict[tuple[str, str], list[OrderflowAnomaly]] = defaultdict(list)
    for anomaly in anomalies:
        grouped[(anomaly.market_ticker, anomaly.trade_id)].append(anomaly)

    opportunities: list[OpportunitySignal] = []
    for (market_ticker, trade_id), event_anomalies in grouped.items():
        event_anomalies.sort(key=lambda row: row.anomaly_type)
        lead = event_anomalies[0]
        probabilities = [_anomaly_probability(anomaly) for anomaly in event_anomalies]
        max_probability = max(probabilities)
        confluence = _clamp_01(len(event_anomalies) / 3.0)
        average_probability = sum(probabilities) / len(probabilities)
        confidence = _clamp_01(0.40 + (0.35 * confluence) + (0.25 * max_probability))
        opportunity_score = _clamp_01(
            (0.55 * max_probability) + (0.25 * average_probability) + (0.20 * confidence)
        )
        observed_at = max(_parse_utc(anomaly.traded_at) for anomaly in event_anomalies)
        driver_names = ", ".join(
            anomaly.anomaly_type.replace("_", " ") for anomaly in event_anomalies
        )

        opportunities.append(
            OpportunitySignal(
                signal_id=f"flow:{market_ticker}:{trade_id}",
                signal_type="orderflow_dislocation",
                source=lead.source,
                entity=market_ticker,
                market_ticker=market_ticker,
                direction=lead.side,
                title=f"Irregular {lead.side} flow in {market_ticker}",
                thesis=(
                    f"{market_ticker} printed a {lead.side} trade at {lead.price:.2f} with "
                    f"{lead.quantity} contracts and {driver_names} evidence."
                ),
                probability=round(max_probability, 6),
                opportunity_score=round(opportunity_score, 6),
                confidence=round(confidence, 6),
                expected_value_basis=(
                    "Abnormal flow can expose stale pricing or information imbalance; confirm "
                    "persistence and executable spread before treating it as expected value."
                ),
                observed_at=_iso_z(observed_at),
                drivers=[_anomaly_driver(anomaly) for anomaly in event_anomalies],
                next_step="Check current book depth, spread, and whether follow-through volume persists.",
            )
        )

    return opportunities


def build_opportunity_dashboard(
    *,
    fresh_days: int,
    min_resolved: int,
    limit: int,
    max_rows: int,
    min_odds_jump: float,
    min_size_zscore: float,
    min_large_quantity: int,
) -> OpportunityDashboard:
    surface = collect_surface_account_stats(fresh_days=fresh_days, min_resolved=min_resolved)
    enrichment = collect_account_enrichment({row.account_id for row in surface})
    ranked_accounts = rank_accounts_by_skill(
        surface=surface,
        enrichment=enrichment,
        limit=max(limit, 1),
    )
    account_opportunities = [_account_opportunity(entry) for entry in ranked_accounts]

    anomalies = detect_orderflow_anomalies(
        max_rows=max_rows,
        limit=max(limit * 3, 1),
        min_odds_jump=min_odds_jump,
        min_size_zscore=min_size_zscore,
        min_large_quantity=min_large_quantity,
    )
    orderflow_opportunities = _orderflow_opportunities(anomalies)

    opportunities = account_opportunities + orderflow_opportunities
    opportunities.sort(
        key=lambda row: (
            -row.opportunity_score,
            -row.probability,
            row.observed_at,
            row.signal_id,
        )
    )
    opportunities = opportunities[:limit]

    observed_dates = [_parse_utc(row.observed_at) for row in opportunities]
    generated_at = _iso_z(max(observed_dates)) if observed_dates else _iso_z(datetime.now(timezone.utc))
    top_probability = max((row.probability for row in opportunities), default=0.0)
    top_opportunity_score = max((row.opportunity_score for row in opportunities), default=0.0)

    return OpportunityDashboard(
        generated_at=generated_at,
        summary=OpportunitySummary(
            candidate_count=len(opportunities),
            skilled_account_count=len(account_opportunities),
            orderflow_event_count=len(orderflow_opportunities),
            top_probability=round(top_probability, 6),
            top_opportunity_score=round(top_opportunity_score, 6),
            freshness_window_days=fresh_days,
        ),
        opportunities=opportunities,
    )
