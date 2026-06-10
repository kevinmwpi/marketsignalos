from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.main import app


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )


def _enrichment(wallet: str, name: str) -> dict[str, object]:
    return {
        "proxy_wallet": wallet, "name": name, "pseudonym": name,
        "resolved_trades": 50, "wins": 38, "losses": 12,
        "win_rate": 0.76, "skill_likelihood": 0.99, "stddevs_above_expected": 3.7,
        "forecast_skill_likelihood": 0.99, "forecast_edge_mean": 0.5,
        "forecast_edge_lower_bound": 0.2, "independent_settled_events": 50.0,
        "all_time_pnl_usdc": 250_000, "all_time_roi": 0.25, "pnl_30d_usdc": 1_000,
        "data_quality_status": "trusted", "tailability_status": "tailable",
        "score_version": "forecast-v3",
        "total_volume_usdc": 1_000_000, "total_pnl_usdc": 250_000,
        "avg_position_size_usdc": 20_000, "trade_count": 200,
        "last_activity_at": 1731000000, "computed_at": "2026-06-09T00:00:00Z",
    }


def _position(
    wallet: str, cond: str, outcome_index: int, *, value: float, slug: str,
) -> dict[str, object]:
    return {
        "proxy_wallet": wallet, "condition_id": cond,
        "outcome_index": outcome_index,
        "outcome": "Yes" if outcome_index == 0 else "No",
        "size": 1000.0, "avg_price": 0.5, "current_value_usdc": value,
        "slug": slug, "title": f"Market {slug}", "event_slug": f"event-{slug}",
        "current_outcome_price": 0.58, "snapshot_id": f"snapshot-{wallet}",
        "snapshot_at": "2026-06-09T08:00:00Z",
    }


def _buy(
    wallet: str, cond: str, outcome_index: int, *, ts: int, price: float,
    usdc: float, slug: str,
) -> dict[str, object]:
    return {
        "proxy_wallet": wallet, "timestamp": ts, "condition_id": cond,
        "type": "TRADE", "side": "BUY",
        "size": usdc / price, "usdc_size": usdc, "price": price,
        "outcome_index": outcome_index,
        "outcome": "Yes" if outcome_index == 0 else "No",
        "slug": slug, "title": f"Market {slug}", "event_slug": f"event-{slug}",
        "transaction_hash": f"0xtx-{wallet}-{cond}-{outcome_index}",
    }


def _seed(tmp_path: Path) -> Path:
    """alpha + beta both hold YES on the fed market; alpha alone holds btc."""
    d = tmp_path / "pmdata"
    d.mkdir(parents=True, exist_ok=True)

    _write_jsonl(
        d / "polymarket_wallet_enrichment.jsonl",
        [_enrichment("0xalpha", "AlphaWhale"), _enrichment("0xbeta", "BetaShark"),
         _enrichment("0xgamma", "GammaFox")],
    )
    _write_jsonl(
        d / "polymarket_positions.jsonl",
        [
            _position("0xalpha", "0xcond_fed", 0, value=2900.0, slug="fed"),
            _position("0xbeta", "0xcond_fed", 0, value=1200.0, slug="fed"),
            _position("0xalpha", "0xcond_btc", 0, value=800.0, slug="btc"),
        ],
    )
    _write_jsonl(
        d / "polymarket_position_snapshots.jsonl",
        [
            {"proxy_wallet": "0xalpha", "snapshot_id": "snapshot-0xalpha", "complete": True},
            {"proxy_wallet": "0xbeta", "snapshot_id": "snapshot-0xbeta", "complete": True},
            {"proxy_wallet": "0xgamma", "snapshot_id": "snapshot-0xgamma", "complete": True},
        ],
    )
    _write_jsonl(
        d / "polymarket_activity.jsonl",
        [
            _buy("0xalpha", "0xcond_fed", 0, ts=1731500000, price=0.45, usdc=2250.0, slug="fed"),
            _buy("0xbeta", "0xcond_fed", 0, ts=1731400000, price=0.55, usdc=1100.0, slug="fed"),
            _buy("0xalpha", "0xcond_btc", 0, ts=1730000000, price=0.30, usdc=600.0, slug="btc"),
        ],
    )
    _write_jsonl(
        d / "polymarket_markets.jsonl",
        [
            {
                "gamma_id": "1", "condition_id": "0xcond_fed", "slug": "fed",
                "question": "Fed cuts?", "category": "economics",
                "end_date": "2026-09-18T00:00:00Z",
                "outcomes": ["Yes", "No"], "outcome_prices": [0.58, 0.42],
                "volume_usdc": 100000, "liquidity_usdc": 5000,
                "closed": False, "active": True,
                "last_trade_price": 0.58, "best_bid": 0.57, "best_ask": 0.59,
                "fetched_at": "2026-06-09T08:00:00Z",
            },
            {
                "gamma_id": "2", "condition_id": "0xcond_btc", "slug": "btc",
                "question": "BTC 100k?", "category": "crypto",
                "end_date": "2026-12-31T00:00:00Z",
                "outcomes": ["Yes", "No"], "outcome_prices": [0.40, 0.60],
                "volume_usdc": 50000, "liquidity_usdc": 2500,
                "closed": False, "active": True,
                "last_trade_price": 0.40, "best_bid": 0.39, "best_ask": 0.41,
                "fetched_at": "2026-06-09T08:00:00Z",
            },
        ],
    )
    return d


def _add_contested_side(pm_dir: Path) -> None:
    """gamma takes the NO side of the fed market."""
    positions = pm_dir / "polymarket_positions.jsonl"
    rows = [json.loads(line) for line in positions.read_text("utf-8").splitlines() if line]
    rows.append(
        {
            **_position("0xgamma", "0xcond_fed", 1, value=500.0, slug="fed"),
            "snapshot_id": "snapshot-0xgamma",
        }
    )
    _write_jsonl(positions, rows)
    activity = pm_dir / "polymarket_activity.jsonl"
    arows = [json.loads(line) for line in activity.read_text("utf-8").splitlines() if line]
    arows.append(
        _buy("0xgamma", "0xcond_fed", 1, ts=1731450000, price=0.50, usdc=500.0, slug="fed")
    )
    _write_jsonl(activity, arows)


def test_feed_rows_carry_consensus_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows = TestClient(app).get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20"
    ).json()
    fed_rows = [r for r in rows if r["condition_id"] == "0xcond_fed"]
    assert len(fed_rows) == 2
    assert all(r["consensus_wallets"] == 2 for r in fed_rows)
    assert all(r["consensus_contested"] is False for r in fed_rows)
    btc = next(r for r in rows if r["condition_id"] == "0xcond_btc")
    assert btc["consensus_wallets"] == 1


def test_feed_consensus_ignores_per_bet_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """beta's $1200 fed position is filtered out by the value floor, but
    alpha's surviving row must still count beta toward consensus."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows = TestClient(app).get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20"
        "&min_position_value_usdc=2000"
    ).json()
    assert [r["proxy_wallet"] for r in rows] == ["0xalpha"]
    assert rows[0]["consensus_wallets"] == 2


def test_feed_marks_contested_markets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    _add_contested_side(pm_dir)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows = TestClient(app).get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20"
    ).json()
    fed_yes = [r for r in rows if r["condition_id"] == "0xcond_fed" and r["outcome_index"] == 0]
    fed_no = [r for r in rows if r["condition_id"] == "0xcond_fed" and r["outcome_index"] == 1]
    assert all(r["consensus_contested"] is True for r in fed_yes + fed_no)
    assert all(r["consensus_wallets"] == 2 for r in fed_yes)
    assert all(r["consensus_wallets"] == 1 for r in fed_no)


def test_consensus_endpoint_groups_sides_and_weights_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows = TestClient(app).get("/signals/market-consensus").json()
    # Only the fed market clears min_wallets=2.
    assert len(rows) == 1
    fed = rows[0]
    assert fed["condition_id"] == "0xcond_fed"
    assert fed["total_wallets"] == 2
    assert fed["net_direction"] == "Yes"
    assert fed["polymarket_market_url"] == "https://polymarket.com/event/event-fed"
    assert len(fed["sides"]) == 1
    side = fed["sides"][0]
    assert side["wallets"] == 2
    assert side["wallet_names"] == ["AlphaWhale", "BetaShark"]
    assert side["total_position_value_usdc"] == 4100.0
    # Capital-weighted: (0.45*2250 + 0.55*1100) / 3350 = 0.4828
    assert side["avg_entry_price"] == 0.4828
    assert side["latest_entry_at"] == 1731500000


def test_consensus_endpoint_flags_contested_markets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    _add_contested_side(pm_dir)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows = TestClient(app).get("/signals/market-consensus").json()
    fed = next(r for r in rows if r["condition_id"] == "0xcond_fed")
    assert fed["net_direction"] == "contested"
    assert fed["total_wallets"] == 3
    assert len(fed["sides"]) == 2
    # Sides sorted by wallet count: YES (2) before NO (1).
    assert fed["sides"][0]["outcome_index"] == 0
    assert fed["sides"][1]["outcome_index"] == 1
    assert fed["sides"][1]["wallet_names"] == ["GammaFox"]


def test_consensus_endpoint_min_wallets_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows = TestClient(app).get("/signals/market-consensus?min_wallets=1").json()
    conds = [r["condition_id"] for r in rows]
    # Sorted by wallet count: fed (2) before btc (1).
    assert conds == ["0xcond_fed", "0xcond_btc"]
