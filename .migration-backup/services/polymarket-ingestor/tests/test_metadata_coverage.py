from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from marketsignalos_polymarket.metadata_coverage import (
    activity_conditions,
    coverage_for_conditions,
    market_coverage_index,
    refresh_hydration_metadata,
)
from marketsignalos_polymarket.models import PolymarketWalletHydration
from marketsignalos_polymarket.runner import (
    _build_stores,
    _load_market_index,
    _refresh_metadata_coverage,
    parse_activity_row,
    parse_market_row,
)
from marketsignalos_polymarket.skill_computation import (
    compute_enrichment_outputs,
    compute_wallet_enrichment,
)

WALLET = "0x" + "ab" * 20


def hydrated() -> PolymarketWalletHydration:
    return PolymarketWalletHydration(
        proxy_wallet=WALLET, activity_history_complete=True, positions_complete=True,
        closed_positions_complete=True, economic_all_time_complete=True, economic_month_complete=True,
        metadata_condition_count=99, metadata_covered_count=0, metadata_coverage=0,
        all_time_pnl_usdc=100, all_time_volume_usdc=1000, pnl_30d_usdc=10,
    )


def test_coverage_keeps_legacy_settlement_rule_and_distinguishes_missing() -> None:
    market = parse_market_row({"conditionId": "closed", "closed": True})
    open_market = replace(market, condition_id="open", closed=False, outcome_prices=[.6, .4])
    near_one = replace(open_market, condition_id="near", outcome_prices=[.99, .01])
    # An older covered observation still counts under the existing hydration rule.
    later_open = replace(market, closed=False, outcome_prices=[.5, .5])
    index = market_coverage_index([market, open_market, near_one, later_open])
    c = coverage_for_conditions({"closed", "open", "near", "missing"}, index)
    assert asdict(c) == {"conditions": 4, "covered": 2, "missing_market_rows": 1, "present_unsettled": 1}
    assert c.ratio == .5
    assert coverage_for_conditions(set(), index).ratio == 1


def test_denominator_includes_sell_only_and_ignores_duplicate_fills_nontrade_empty() -> None:
    trade = parse_activity_row({"proxyWallet": WALLET, "conditionId": "one", "type": "TRADE"})
    events = [trade, trade, replace(trade, condition_id="two", side="SELL"),
              replace(trade, condition_id="redeem", type="REDEEM"), replace(trade, condition_id="")]
    assert activity_conditions(events) == {"one", "two"}


def test_snapshot_refresh_is_immutable_and_never_repairs_other_trust_flags() -> None:
    saved = hydrated()
    saved.activity_history_complete = False
    before = asdict(saved)
    fresh = refresh_hydration_metadata(saved, {"one"}, {"one": True})
    assert fresh is not None
    assert (fresh.metadata_condition_count, fresh.metadata_covered_count, fresh.metadata_coverage) == (1, 1, 1)
    assert fresh.activity_history_complete is False
    assert fresh.all_time_pnl_usdc == saved.all_time_pnl_usdc
    assert asdict(saved) == before
    assert refresh_hydration_metadata(None, {"one"}, {"one": True}) is None


def test_scorers_refresh_stale_false_and_stale_true_without_changing_numeric_scores() -> None:
    activity = [parse_activity_row({
        "proxyWallet": WALLET, "conditionId": "one", "type": "TRADE", "side": "BUY",
        "timestamp": 1000, "size": 100, "price": .4, "usdcSize": 40, "outcomeIndex": 0,
    })]
    market = parse_market_row({
        "conditionId": "one", "closed": True, "outcomes": '["Yes","No"]',
        "outcomePrices": '["1","0"]',
    })
    saved = hydrated()
    before = asdict(saved)
    corrected = replace(saved, metadata_condition_count=1, metadata_covered_count=1, metadata_coverage=1)
    stale_rows, _ = compute_enrichment_outputs(activity=activity, markets=[market], leaderboard=[],
                                             hydration_by_wallet={WALLET: saved})
    fresh_rows, _ = compute_enrichment_outputs(activity=activity, markets=[market], leaderboard=[],
                                             hydration_by_wallet={WALLET: corrected})
    assert stale_rows[0].data_quality_status == "trusted"
    assert replace(stale_rows[0], computed_at="") == replace(fresh_rows[0], computed_at="")
    assert asdict(saved) == before
    single = compute_wallet_enrichment(WALLET, activity=activity, markets_by_condition={"one": market},
                                       hydration=saved)
    assert single.data_quality_status == "trusted"
    # A previously complete state must lose trust when a referenced market is absent.
    missing, _ = compute_enrichment_outputs(activity=activity, markets=[], leaderboard=[],
                                            hydration_by_wallet={WALLET: corrected})
    assert "incomplete market metadata" in missing[0].data_quality_reasons
    single_missing = compute_wallet_enrichment(WALLET, activity=activity, markets_by_condition={},
                                               hydration=corrected)
    assert "incomplete market metadata" in single_missing.data_quality_reasons


def test_shared_calculation_matches_existing_hydration_refresh(tmp_path: Path) -> None:
    stores = _build_stores(tmp_path)
    events = [parse_activity_row({"proxyWallet": WALLET, "conditionId": c, "type": "TRADE"})
              for c in ("closed", "open", "missing", "closed")]
    markets = [parse_market_row({"conditionId": "closed", "closed": True}),
               parse_market_row({"conditionId": "open", "closed": False,
                                 "outcomePrices": '["0.6","0.4"]'})]
    stores.activity.write_activity(events)
    stores.markets.write_markets(markets)
    saved = hydrated()
    stores.hydration.upsert_hydration([saved])
    _refresh_metadata_coverage(stores)
    refreshed = stores.hydration.load_hydration()[WALLET]
    pure = refresh_hydration_metadata(saved, activity_conditions(events), market_coverage_index(markets))
    assert pure is not None
    assert asdict(refreshed) == asdict(pure)
    raw_index = _load_market_index(stores.markets_path)
    assert market_coverage_index(markets) == {k: v["has_settlement"] for k, v in raw_index.items()}
