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


def _seed_polymarket_dir(tmp_path: Path) -> Path:
    """Create the 5 files cross_exchange.py expects, return the dir."""
    d = tmp_path / "pmdata"
    d.mkdir(parents=True, exist_ok=True)

    # Skilled wallet with 30 resolved bets, high skill.
    _write_jsonl(
        d / "polymarket_wallet_enrichment.jsonl",
        [
            {
                "proxy_wallet": "0xskilled", "name": "AlphaWhale", "pseudonym": "AlphaWhale",
                "resolved_trades": 30, "wins": 24, "losses": 6,
                "win_rate": 0.8, "skill_likelihood": 0.95, "stddevs_above_expected": 3.3,
                "total_volume_usdc": 500000, "total_pnl_usdc": 75000,
                "avg_position_size_usdc": 16000, "trade_count": 120,
                "last_activity_at": 1731000000, "computed_at": "2026-05-11T00:00:00Z",
            },
            # Low-skill wallet — should be filtered out by min_skill.
            {
                "proxy_wallet": "0xnoob", "name": "Newbie", "pseudonym": "Newbie",
                "resolved_trades": 30, "wins": 12, "losses": 18,
                "win_rate": 0.4, "skill_likelihood": 0.1, "stddevs_above_expected": -2.2,
                "total_volume_usdc": 1000, "total_pnl_usdc": -500,
                "avg_position_size_usdc": 30, "trade_count": 40,
                "last_activity_at": 1731000000, "computed_at": "2026-05-11T00:00:00Z",
            },
        ],
    )

    # Skilled wallet holds a YES position on condition 0xcond1.
    # size=1000 shares, current_value=620 → implied YES price 0.62.
    _write_jsonl(
        d / "polymarket_positions.jsonl",
        [
            {
                "proxy_wallet": "0xskilled", "condition_id": "0xcond1",
                "outcome_index": 0, "outcome": "Yes",
                "size": 1000.0, "avg_price": 0.55, "current_value_usdc": 620.0,
                "slug": "fed-50bp-sep", "title": "Fed cuts 50bp in September",
                "event_slug": "fed-decision",
                "snapshot_at": "2026-05-11T00:00:00Z",
            },
            # Noob also holds a position — should be filtered by min_skill.
            {
                "proxy_wallet": "0xnoob", "condition_id": "0xcond1",
                "outcome_index": 0, "outcome": "Yes",
                "size": 10.0, "avg_price": 0.55, "current_value_usdc": 6.2,
                "slug": "fed-50bp-sep", "title": "Fed cuts 50bp in September",
                "event_slug": "fed-decision",
                "snapshot_at": "2026-05-11T00:00:00Z",
            },
        ],
    )

    # Approved link between Kalshi KXFED-26SEP-50BP and Polymarket 0xcond1.
    _write_jsonl(
        d / "market_links.jsonl",
        [
            {
                "kalshi_ticker": "KXFED-26SEP-50BP",
                "polymarket_condition_id": "0xcond1",
                "polymarket_slug": "fed-50bp-sep",
                "kalshi_title": "Fed cuts 50bp in September 2026?",
                "polymarket_title": "Fed cuts 50bp in September",
                "kalshi_end_date": "2026-09-18T00:00:00Z",
                "polymarket_end_date": "2026-09-18T00:00:00Z",
                "confidence": 0.92, "status": "approved", "matched_by": "auto",
                "matched_at": "2026-05-11T00:00:00Z",
            },
        ],
    )

    # Polymarket market with current YES price 0.62 (matches the position).
    _write_jsonl(
        d / "polymarket_markets.jsonl",
        [
            {
                "gamma_id": "1", "condition_id": "0xcond1", "slug": "fed-50bp-sep",
                "question": "Fed cuts 50bp in September", "category": "economics",
                "end_date": "2026-09-18T00:00:00Z",
                "outcomes": ["Yes", "No"], "outcome_prices": [0.62, 0.38],
                "volume_usdc": 100000, "liquidity_usdc": 5000,
                "closed": False, "active": True,
                "last_trade_price": 0.62, "best_bid": 0.61, "best_ask": 0.63,
                "fetched_at": "2026-05-11T00:00:00Z",
            },
        ],
    )

    # Kalshi market: YES priced at 55 cents (so 0.55) — 7 percentage points cheaper.
    _write_jsonl(
        d / "kalshi_markets.jsonl",
        [
            {
                "ticker": "KXFED-26SEP-50BP", "event_ticker": "KXFED-26SEP",
                "title": "Fed cuts 50bp in September 2026?",
                "subtitle": "", "yes_sub_title": "",
                "category": "Economics", "status": "open",
                "expiration_time": "2026-09-18T00:00:00Z",
                "close_time": "2026-09-18T00:00:00Z",
                "yes_bid": 54, "yes_ask": 56, "last_price": 55,
                "fetched_at": "2026-05-11T00:00:00Z",
            },
        ],
    )

    return d


def test_cross_exchange_surfaces_skilled_wallet_dislocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed_polymarket_dir(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/cross-exchange?min_skill=0.6&min_resolved=20&min_dislocation_pct=2")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1, f"Expected 1 signal, got {rows}"
    sig = rows[0]
    assert sig["proxy_wallet"] == "0xskilled"
    assert sig["wallet_name"] == "AlphaWhale"
    assert sig["kalshi_ticker"] == "KXFED-26SEP-50BP"
    # Kalshi YES at 0.55, Polymarket YES at 0.62 → dislocation = 0.55 - 0.62 = -0.07
    assert abs(sig["price_dislocation"] - (-0.07)) < 1e-3
    assert abs(sig["price_dislocation_pct"] - 7.0) < 1e-3
    # Kalshi YES is cheaper → buy YES on Kalshi.
    assert sig["cheaper_exchange"] == "kalshi"
    assert "buy yes on kalshi" in sig["recommended_action"].lower()


def test_cross_exchange_filters_low_skill_wallets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed_polymarket_dir(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    # Set bar above what AlphaWhale (0.95) clears — empty result.
    resp = client.get("/signals/cross-exchange?min_skill=0.99&min_resolved=20&min_dislocation_pct=2")
    assert resp.status_code == 200
    assert resp.json() == []


def test_cross_exchange_filters_small_dislocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed_polymarket_dir(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    # 7pp dislocation is below a 10pp threshold.
    resp = client.get("/signals/cross-exchange?min_dislocation_pct=10")
    assert resp.status_code == 200
    assert resp.json() == []


def test_cross_exchange_respects_only_approved_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed_polymarket_dir(tmp_path)
    # Downgrade the link to pending.
    _write_jsonl(
        pm_dir / "market_links.jsonl",
        [
            {
                "kalshi_ticker": "KXFED-26SEP-50BP",
                "polymarket_condition_id": "0xcond1",
                "polymarket_slug": "fed-50bp-sep",
                "kalshi_title": "Fed cuts 50bp in September 2026?",
                "polymarket_title": "Fed cuts 50bp in September",
                "kalshi_end_date": "2026-09-18T00:00:00Z",
                "polymarket_end_date": "2026-09-18T00:00:00Z",
                "confidence": 0.5, "status": "pending", "matched_by": "auto",
                "matched_at": "2026-05-11T00:00:00Z",
            },
        ],
    )
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    # Default lets pending through.
    resp = client.get("/signals/cross-exchange?min_skill=0.6&min_resolved=20&min_dislocation_pct=2")
    assert len(resp.json()) == 1
    # only_approved_links=true should drop it.
    resp = client.get("/signals/cross-exchange?min_skill=0.6&min_resolved=20&min_dislocation_pct=2&only_approved_links=true")
    assert resp.json() == []


def test_cross_exchange_rejected_links_never_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed_polymarket_dir(tmp_path)
    # Even with manual rejection, the API must skip these regardless of only_approved_links.
    _write_jsonl(
        pm_dir / "market_links.jsonl",
        [
            {
                "kalshi_ticker": "KXFED-26SEP-50BP",
                "polymarket_condition_id": "0xcond1",
                "polymarket_slug": "fed-50bp-sep",
                "kalshi_title": "Fed cuts 50bp in September 2026?",
                "polymarket_title": "Fed cuts 50bp in September",
                "kalshi_end_date": "2026-09-18T00:00:00Z",
                "polymarket_end_date": "2026-09-18T00:00:00Z",
                "confidence": 0.9, "status": "rejected", "matched_by": "manual",
                "matched_at": "2026-05-11T00:00:00Z",
            },
        ],
    )
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/cross-exchange?min_skill=0.6&min_resolved=20&min_dislocation_pct=2")
    assert resp.json() == []


def test_cross_exchange_no_position_no_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed_polymarket_dir(tmp_path)
    # Empty the positions file — wallet has skill but no exposure.
    _write_jsonl(pm_dir / "polymarket_positions.jsonl", [])
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/cross-exchange?min_skill=0.6&min_resolved=20")
    assert resp.json() == []


def test_cross_exchange_no_kalshi_price_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed_polymarket_dir(tmp_path)
    # Kalshi market exists but has no live price.
    _write_jsonl(
        pm_dir / "kalshi_markets.jsonl",
        [
            {
                "ticker": "KXFED-26SEP-50BP", "event_ticker": "KXFED-26SEP",
                "title": "Fed cuts 50bp in September 2026?",
                "subtitle": "", "yes_sub_title": "",
                "category": "Economics", "status": "open",
                "expiration_time": "2026-09-18T00:00:00Z",
                "close_time": "2026-09-18T00:00:00Z",
                "yes_bid": 0, "yes_ask": 0, "last_price": 0,  # no quotes
                "fetched_at": "2026-05-11T00:00:00Z",
            },
        ],
    )
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/cross-exchange?min_skill=0.6&min_resolved=20&min_dislocation_pct=2")
    assert resp.json() == []


def test_cross_exchange_no_position_outcome_uses_position_implied_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the polymarket market record is missing, fall back to the position's implied price."""
    pm_dir = _seed_polymarket_dir(tmp_path)
    _write_jsonl(pm_dir / "polymarket_markets.jsonl", [])
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/cross-exchange?min_skill=0.6&min_resolved=20&min_dislocation_pct=2")
    rows = resp.json()
    assert len(rows) == 1
    # Implied price from position: current_value_usdc / size = 620 / 1000 = 0.62
    assert abs(rows[0]["polymarket_yes_price"] - 0.62) < 1e-3


def test_cross_exchange_no_position_holder_returns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a wallet has skill but isn't in enrichment, no signal should fire."""
    pm_dir = _seed_polymarket_dir(tmp_path)
    _write_jsonl(pm_dir / "polymarket_wallet_enrichment.jsonl", [])
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/cross-exchange?min_skill=0.6&min_resolved=20")
    assert resp.json() == []


def test_cross_exchange_latest_position_snapshot_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Position file is append-only; the most recent snapshot should be used."""
    pm_dir = _seed_polymarket_dir(tmp_path)
    _write_jsonl(
        pm_dir / "polymarket_positions.jsonl",
        [
            # Older snapshot — wallet had 100 shares.
            {
                "proxy_wallet": "0xskilled", "condition_id": "0xcond1",
                "outcome_index": 0, "outcome": "Yes",
                "size": 100.0, "avg_price": 0.55, "current_value_usdc": 62.0,
                "slug": "fed-50bp-sep", "title": "Fed cuts 50bp in September",
                "event_slug": "fed-decision",
                "snapshot_at": "2026-05-10T00:00:00Z",
            },
            # Latest snapshot — wallet now has 1000 shares.
            {
                "proxy_wallet": "0xskilled", "condition_id": "0xcond1",
                "outcome_index": 0, "outcome": "Yes",
                "size": 1000.0, "avg_price": 0.55, "current_value_usdc": 620.0,
                "slug": "fed-50bp-sep", "title": "Fed cuts 50bp in September",
                "event_slug": "fed-decision",
                "snapshot_at": "2026-05-11T00:00:00Z",
            },
        ],
    )
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/cross-exchange?min_skill=0.6&min_resolved=20&min_dislocation_pct=2")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["polymarket_position_size"] == 1000.0
    assert rows[0]["polymarket_position_value_usdc"] == 620.0
