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


def _append_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def _position(
    cond: str, *, size: float, value: float, slug: str, snapshot_id: str,
    snapshot_at: str,
) -> dict[str, object]:
    return {
        "proxy_wallet": "0xalpha", "condition_id": cond, "outcome_index": 0,
        "outcome": "Yes", "size": size, "avg_price": 0.45,
        "current_value_usdc": value, "slug": slug, "title": f"Market {slug}",
        "event_slug": f"event-{slug}", "current_outcome_price": 0.58,
        "snapshot_id": snapshot_id, "snapshot_at": snapshot_at,
    }


def _market(cond: str, slug: str, *, closed: bool = False) -> dict[str, object]:
    return {
        "gamma_id": cond, "condition_id": cond, "slug": slug,
        "question": f"{slug}?", "category": "economics",
        "end_date": "2026-09-18T00:00:00Z",
        "outcomes": ["Yes", "No"],
        "outcome_prices": [1.0, 0.0] if closed else [0.58, 0.42],
        "volume_usdc": 100000, "liquidity_usdc": 5000,
        "closed": closed, "active": not closed,
        "last_trade_price": 0.58, "best_bid": 0.57, "best_ask": 0.59,
        "fetched_at": "2026-06-10T08:00:00Z" if closed else "2026-06-09T08:00:00Z",
    }


def _seed(tmp_path: Path) -> Path:
    """Skilled wallet alpha holding fed + btc in complete snapshot A."""
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
                "all_time_pnl_usdc": 250_000, "all_time_roi": 0.25, "pnl_30d_usdc": 1_000,
                "data_quality_status": "trusted", "tailability_status": "tailable",
                "score_version": "forecast-v3",
                "total_volume_usdc": 1_000_000, "total_pnl_usdc": 250_000,
                "avg_position_size_usdc": 20_000, "trade_count": 200,
                "last_activity_at": 1731000000, "computed_at": "2026-06-09T00:00:00Z",
            },
        ],
    )
    _write_jsonl(
        d / "polymarket_positions.jsonl",
        [
            _position("0xcond_fed", size=5000.0, value=2900.0, slug="fed",
                      snapshot_id="snapA", snapshot_at="2026-06-09T08:00:00Z"),
            _position("0xcond_btc", size=2000.0, value=800.0, slug="btc",
                      snapshot_id="snapA", snapshot_at="2026-06-09T08:00:00Z"),
        ],
    )
    _write_jsonl(
        d / "polymarket_position_snapshots.jsonl",
        [{"proxy_wallet": "0xalpha", "snapshot_id": "snapA", "complete": True,
          "snapshot_at": "2026-06-09T08:00:00Z"}],
    )
    _write_jsonl(
        d / "polymarket_activity.jsonl",
        [
            {
                "proxy_wallet": "0xalpha", "timestamp": 1731500000,
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "BUY",
                "size": 5000.0, "usdc_size": 2250.0, "price": 0.45,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed",
                "title": "Market fed", "event_slug": "event-fed",
                "transaction_hash": "0xtx_fed",
            },
            {
                "proxy_wallet": "0xalpha", "timestamp": 1730000000,
                "condition_id": "0xcond_btc", "type": "TRADE", "side": "BUY",
                "size": 2000.0, "usdc_size": 600.0, "price": 0.30,
                "outcome_index": 0, "outcome": "Yes", "slug": "btc",
                "title": "Market btc", "event_slug": "event-btc",
                "transaction_hash": "0xtx_btc",
            },
        ],
    )
    _write_jsonl(
        d / "polymarket_markets.jsonl",
        [_market("0xcond_fed", "fed"), _market("0xcond_btc", "btc")],
    )
    return d


def _advance_to_snapshot_b(
    pm_dir: Path, *, fed_size: float | None, btc_size: float
) -> None:
    """Append snapshot B rows and advance the manifest. fed_size=None means
    the fed position is absent from snapshot B entirely."""
    rows: list[dict[str, object]] = []
    if fed_size is not None:
        rows.append(
            _position("0xcond_fed", size=fed_size, value=fed_size * 0.58,
                      slug="fed", snapshot_id="snapB",
                      snapshot_at="2026-06-10T08:00:00Z")
        )
    rows.append(
        _position("0xcond_btc", size=btc_size, value=btc_size * 0.4, slug="btc",
                  snapshot_id="snapB", snapshot_at="2026-06-10T08:00:00Z")
    )
    _append_jsonl(pm_dir / "polymarket_positions.jsonl", rows)
    _write_jsonl(
        pm_dir / "polymarket_position_snapshots.jsonl",
        [{"proxy_wallet": "0xalpha", "snapshot_id": "snapB", "complete": True,
          "snapshot_at": "2026-06-10T08:00:00Z"}],
    )


def test_first_pass_initializes_without_emitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)

    body = client.post("/signals/exits/refresh").json()
    assert body["initialized_wallets"] == 1
    assert body["detected_closed"] == 0
    assert body["detected_trimmed"] == 0
    assert client.get("/signals/exits").json() == []


def test_detects_closed_and_trimmed_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    client.post("/signals/exits/refresh")  # initialize at snapA

    # fed gone entirely; btc trimmed 2000 → 900 (55%).
    _advance_to_snapshot_b(pm_dir, fed_size=None, btc_size=900.0)
    body = client.post("/signals/exits/refresh").json()
    assert body["detected_closed"] == 1
    assert body["detected_trimmed"] == 1
    assert body["diffed_wallets"] == 1

    rows = client.get("/signals/exits").json()
    assert len(rows) == 2
    fed = next(r for r in rows if r["condition_id"] == "0xcond_fed")
    assert fed["exit_type"] == "closed"
    assert fed["previous_size"] == 5000.0
    assert fed["current_size"] == 0.0
    assert fed["reduction_pct"] == 1.0
    assert fed["previous_value_usdc"] == 2900.0
    assert fed["wallet_name"] == "AlphaWhale"
    assert fed["polymarket_market_url"] == "https://polymarket.com/event/event-fed"
    btc = next(r for r in rows if r["condition_id"] == "0xcond_btc")
    assert btc["exit_type"] == "trimmed"
    assert btc["reduction_pct"] == 0.55

    # Replay: same snapshots → nothing new.
    body = client.post("/signals/exits/refresh").json()
    assert body["detected_closed"] == 0
    assert body["detected_trimmed"] == 0
    assert len(client.get("/signals/exits").json()) == 2


def test_small_trims_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    client.post("/signals/exits/refresh")

    # fed unchanged; btc trimmed only 20% (2000 → 1600) — below threshold.
    _advance_to_snapshot_b(pm_dir, fed_size=5000.0, btc_size=1600.0)
    body = client.post("/signals/exits/refresh").json()
    assert body["detected_closed"] == 0
    assert body["detected_trimmed"] == 0


def test_resolved_market_redemption_is_not_an_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the market closed between snapshots, the vanished position is
    settlement (redemption), not a sell — no exit signal."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    client.post("/signals/exits/refresh")

    _advance_to_snapshot_b(pm_dir, fed_size=None, btc_size=900.0)
    # fed market resolved between snapshots.
    _write_jsonl(
        pm_dir / "polymarket_markets.jsonl",
        [_market("0xcond_fed", "fed", closed=True), _market("0xcond_btc", "btc")],
    )
    body = client.post("/signals/exits/refresh").json()
    assert body["detected_closed"] == 0  # fed suppressed
    assert body["detected_trimmed"] == 1  # btc still real
    rows = client.get("/signals/exits").json()
    assert [r["condition_id"] for r in rows] == ["0xcond_btc"]


def test_exit_marks_previously_surfaced_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)

    # Record the held positions in the paper-trade ledger FIRST, then
    # initialize the exit watermark.
    ledger = client.post("/signals/ledger/refresh").json()
    assert ledger["recorded"] == 2
    client.post("/signals/exits/refresh")

    _advance_to_snapshot_b(pm_dir, fed_size=None, btc_size=900.0)
    client.post("/signals/exits/refresh")

    fed = next(
        r
        for r in client.get("/signals/exits").json()
        if r["condition_id"] == "0xcond_fed"
    )
    assert fed["was_surfaced"] is True
    assert fed["surfaced_recorded_at"]


def test_exit_type_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    client.post("/signals/exits/refresh")
    _advance_to_snapshot_b(pm_dir, fed_size=None, btc_size=900.0)
    client.post("/signals/exits/refresh")

    closed = client.get("/signals/exits?exit_type=closed").json()
    assert [r["condition_id"] for r in closed] == ["0xcond_fed"]
    trimmed = client.get("/signals/exits?exit_type=trimmed").json()
    assert [r["condition_id"] for r in trimmed] == ["0xcond_btc"]
