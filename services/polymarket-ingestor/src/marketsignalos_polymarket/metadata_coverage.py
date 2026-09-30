"""Snapshot-derived coverage using the existing forecast-v4 settlement rule.

This deliberately preserves the rule: a stored closed row or an outcome price
>=0.99 counts as covered. Open, unsettled metadata is reported separately but
does not newly pass the gate. Coverage is not proof of a valid resolution label.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from .models import PolymarketActivity, PolymarketMarket, PolymarketWalletHydration


@dataclass(frozen=True)
class MetadataCoverage:
    conditions: int
    covered: int
    missing_market_rows: int
    present_unsettled: int

    @property
    def ratio(self) -> float:
        return self.covered / self.conditions if self.conditions else 1.0


def market_coverage_index(markets: Iterable[PolymarketMarket]) -> dict[str, bool]:
    """Match runner's any-historical-row coverage aggregation, not its latest fit row."""
    index: dict[str, bool] = {}
    for market in markets:
        if market.condition_id:
            covered = market.closed or any(price >= .99 for price in market.outcome_prices)
            index[market.condition_id] = index.get(market.condition_id, False) or covered
    return index


def activity_conditions(activity: Iterable[PolymarketActivity]) -> set[str]:
    # Include SELL-only conditions and ignore duplicate fills, matching hydration.
    return {event.condition_id for event in activity
            if event.type == "TRADE" and event.condition_id}


def coverage_for_conditions(conditions: set[str], index: Mapping[str, bool]) -> MetadataCoverage:
    covered = sum(index.get(condition, False) for condition in conditions)
    missing = sum(condition not in index for condition in conditions)
    return MetadataCoverage(len(conditions), covered, missing, len(conditions) - covered - missing)


def refresh_hydration_metadata(
    hydration: PolymarketWalletHydration | None,
    conditions: set[str],
    index: Mapping[str, bool],
) -> PolymarketWalletHydration | None:
    """Use the score's inputs without modifying persisted hydration or other trust flags."""
    if hydration is None:
        return None
    coverage = coverage_for_conditions(conditions, index)
    return replace(hydration, metadata_condition_count=coverage.conditions,
                   metadata_covered_count=coverage.covered, metadata_coverage=coverage.ratio)
