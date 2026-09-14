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


def _seed(tmp_path: Path) -> Path:
    d = tmp_path / "pmdata"
    d.mkdir(parents=True, exist_ok=True)

    _write_jsonl(
        d / "polymarket_wallet_enrichment.jsonl",
        [
            {
                "proxy_wallet": "0xalpha", "name": "AlphaWhale", "pseudonym": "AlphaWhale",
                "resolved_trades": 50, "wins": 38, "losses": 12,
                "win_rate": 0.76, "skill_likelihood": 0.99, "stddevs_above_expected": 3.7,
                "forecast_skill_likelihood": 0.99, "forecast_edge_mean": 0.5,
                "forecast_edge_lower_bound": 0.2, "independent_settled_events": 50.0,
                "recent_skill_likelihood": 0.95, "recent_edge_mean": 0.4,
                "recent_edge_lower_bound": 0.1, "recent_independent_events": 18.0,
                "clv_mean": 0.041, "clv_lower_bound": 0.012, "clv_sample_size": 32.5,
                "all_time_pnl_usdc": 250_000, "all_time_roi": 0.25, "pnl_30d_usdc": 1_000,
                "data_quality_status": "trusted", "tailability_status": "tailable",
                "score_version": "forecast-v3",
                "total_volume_usdc": 1_000_000, "total_pnl_usdc": 250_000,
                "avg_position_size_usdc": 20_000, "trade_count": 200,
                "last_activity_at": 1731000000, "computed_at": "2026-06-10T00:00:00Z",
            },
        ],
    )
    _write_jsonl(
        d / "polymarket_wallet_bets.jsonl",
        [
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xc_won", "outcome_index": 0,
                "outcome": "Yes", "event_slug": "event-won", "category": "politics",
                "title": "Won market", "status": "won", "entry_price": 0.40,
                "cost_usdc": 400.0, "net_size": 1000.0, "realized_pnl_usdc": 0.0,
                "total_pnl_usdc": 600.0, "clv": 0.12, "last_trade_ts": 1_700_000_000,
                "computed_at": "2026-06-10T00:00:00Z",
            },
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xc_lost", "outcome_index": 0,
                "outcome": "Yes", "event_slug": "event-lost", "category": "politics",
                "title": "Lost market", "status": "lost", "entry_price": 0.50,
                "cost_usdc": 200.0, "net_size": 400.0, "realized_pnl_usdc": 0.0,
                "total_pnl_usdc": -200.0, "clv": -0.05, "last_trade_ts": 1_700_100_000,
                "computed_at": "2026-06-10T00:00:00Z",
            },
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xc_open", "outcome_index": 0,
                "outcome": "Yes", "event_slug": "event-open", "category": "crypto",
                "title": "Open market", "status": "open", "entry_price": 0.30,
                "cost_usdc": 300.0, "net_size": 1000.0, "realized_pnl_usdc": 0.0,
                "total_pnl_usdc": 0.0, "clv": None, "last_trade_ts": 1_700_200_000,
                "computed_at": "2026-06-10T00:00:00Z",
            },
            # Different wallet — must not leak into alpha's dossier.
            {
                "proxy_wallet": "0xother", "condition_id": "0xc_other", "outcome_index": 0,
                "outcome": "Yes", "event_slug": "event-other", "category": "sports",
                "title": "Other wallet market", "status": "won", "entry_price": 0.60,
                "cost_usdc": 100.0, "net_size": 100.0, "realized_pnl_usdc": 0.0,
                "total_pnl_usdc": 40.0, "clv": 0.02, "last_trade_ts": 1_700_300_000,
                "computed_at": "2026-06-10T00:00:00Z",
            },
        ],
    )
    _write_jsonl(
        d / "polymarket_wallet_values.jsonl",
        [
            {"proxy_wallet": "0xalpha", "value_usdc": 18_000.0,
             "snapshot_at": "2026-06-09T00:00:00Z"},
            {"proxy_wallet": "0xalpha", "value_usdc": 21_500.0,
             "snapshot_at": "2026-06-10T00:00:00Z"},
        ],
    )
    return d


def test_wallet_detail_assembles_dossier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    body = TestClient(app).get("/signals/wallets/0xALPHA").json()  # case-insensitive
    assert body["proxy_wallet"] == "0xalpha"
    assert body["polymarket_profile_url"] == "https://polymarket.com/profile/0xalpha"
    assert body["wallet_value_usdc"] == 21_500.0  # latest snapshot wins
    assert body["enrichment"]["clv_mean"] == 0.041
    assert body["enrichment"]["recent_independent_events"] == 18.0

    # Bet history: alpha's rows only, newest last_trade_ts first.
    conds = [b["condition_id"] for b in body["bet_history"]]
    assert conds == ["0xc_open", "0xc_lost", "0xc_won"]
    assert body["bet_history_total"] == 3
    open_bet = body["bet_history"][0]
    assert open_bet["clv"] is None

    # Equity curve: settled bets only (won, lost), oldest first, cumulative.
    curve = body["equity_curve"]
    assert [p["cumulative_pnl_usdc"] for p in curve] == [600.0, 400.0]
    assert curve[0]["ts"] == 1_700_000_000

    # Category breakdown aggregates and sorts by bet count.
    cats = {c["category"]: c for c in body["category_breakdown"]}
    assert cats["politics"]["bets"] == 2
    assert cats["politics"]["wins"] == 1
    assert cats["politics"]["losses"] == 1
    assert cats["politics"]["pnl_usdc"] == 400.0
    assert cats["politics"]["win_rate"] == 0.5
    assert cats["crypto"]["open"] == 1


def test_wallet_detail_unknown_wallet_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    resp = TestClient(app).get("/signals/wallets/0xnobody")
    assert resp.status_code == 404


def test_wallet_detail_respects_bet_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    body = TestClient(app).get("/signals/wallets/0xalpha?bet_limit=1").json()
    assert len(body["bet_history"]) == 1
    assert body["bet_history"][0]["condition_id"] == "0xc_open"  # newest
    assert body["bet_history_total"] == 3


def test_leaderboard_carries_clv_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    rows = TestClient(app).get(
        "/signals/polymarket-leaderboard?min_resolved=1"
    ).json()
    alpha = next(r for r in rows if r["proxy_wallet"] == "0xalpha")
    assert alpha["clv_mean"] == 0.041
    assert alpha["clv_lower_bound"] == 0.012
    assert alpha["clv_sample_size"] == 32.5
