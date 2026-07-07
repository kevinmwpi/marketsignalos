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
    """
    Fixture covers four scenarios in one DB so each test only needs to
    assert what it's checking:
      - alpha:    skilled, holds an OPEN position, has a recent BUY  → INCLUDED
      - alpha:    a SECOND held position with an older BUY           → INCLUDED (older)
      - alpha:    a position that has been fully sold (size=0)        → EXCLUDED
      - noob:     not skilled — also has held position + buy         → EXCLUDED
      - ghost:    skilled and has a snapshot but no matching BUY      → EXCLUDED
    """
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
                "score_version": "forecast-v2", "price_lead_score": 0.75,
                "total_volume_usdc": 1_000_000, "total_pnl_usdc": 250_000,
                "avg_position_size_usdc": 20_000, "trade_count": 200,
                "last_activity_at": 1731000000, "computed_at": "2026-05-12T00:00:00Z",
            },
            # noob — should fail min_skill.
            {
                "proxy_wallet": "0xnoob", "name": "Newbie", "pseudonym": "Newbie",
                "resolved_trades": 50, "wins": 20, "losses": 30,
                "win_rate": 0.4, "skill_likelihood": 0.05, "stddevs_above_expected": -2.8,
                "data_quality_status": "trusted", "tailability_status": "blocked",
                "score_version": "forecast-v2",
                "total_volume_usdc": 1000, "total_pnl_usdc": -500,
                "avg_position_size_usdc": 30, "trade_count": 60,
                "last_activity_at": 1731000000, "computed_at": "2026-05-12T00:00:00Z",
            },
            # ghost — skilled but no matching BUY event.
            {
                "proxy_wallet": "0xghost", "name": "GhostHolder", "pseudonym": "GhostHolder",
                "resolved_trades": 40, "wins": 30, "losses": 10,
                "win_rate": 0.75, "skill_likelihood": 0.95, "stddevs_above_expected": 3.0,
                "forecast_skill_likelihood": 0.95, "forecast_edge_mean": 0.4,
                "forecast_edge_lower_bound": 0.1, "independent_settled_events": 40.0,
                "all_time_pnl_usdc": 100_000, "all_time_roi": 0.2, "pnl_30d_usdc": 1_000,
                "data_quality_status": "trusted", "tailability_status": "tailable",
                "score_version": "forecast-v2",
                "total_volume_usdc": 500_000, "total_pnl_usdc": 100_000,
                "avg_position_size_usdc": 10_000, "trade_count": 100,
                "last_activity_at": 1731000000, "computed_at": "2026-05-12T00:00:00Z",
            },
        ],
    )

    _write_jsonl(
        d / "polymarket_positions.jsonl",
        [
            # alpha recent buy — still held, biggest current size.
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xcond_fed",
                "outcome_index": 0, "outcome": "Yes",
                "size": 5000.0, "avg_price": 0.45, "current_value_usdc": 2900.0,
                "slug": "fed-50bp-sep", "title": "Fed cuts 50bp in September",
                "event_slug": "fed-decision",
                "current_outcome_price": 0.58, "snapshot_id": "snapshot-alpha",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
            # alpha older buy — still held but bought longer ago.
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xcond_btc",
                "outcome_index": 0, "outcome": "Yes",
                "size": 2000.0, "avg_price": 0.30, "current_value_usdc": 800.0,
                "slug": "btc-100k-eoy", "title": "Bitcoin closes 2026 above $100k",
                "event_slug": "btc-eoy-2026",
                "current_outcome_price": 0.40, "snapshot_id": "snapshot-alpha",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
            # alpha fully exited — must NOT appear in the feed.
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xcond_exit",
                "outcome_index": 0, "outcome": "Yes",
                "size": 0.0, "avg_price": 0.55, "current_value_usdc": 0.0,
                "slug": "exited", "title": "Already exited",
                "event_slug": "exited-event",
                "current_outcome_price": 0.50, "snapshot_id": "snapshot-alpha",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
            # noob's held position — must NOT appear (low skill).
            {
                "proxy_wallet": "0xnoob", "condition_id": "0xcond_fed",
                "outcome_index": 0, "outcome": "Yes",
                "size": 10.0, "avg_price": 0.45, "current_value_usdc": 5.8,
                "slug": "fed-50bp-sep", "title": "Fed cuts 50bp in September",
                "event_slug": "fed-decision",
                "current_outcome_price": 0.58, "snapshot_id": "snapshot-noob",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
            # ghost holds a position but we have NO matching buy event for it.
            {
                "proxy_wallet": "0xghost", "condition_id": "0xcond_btc",
                "outcome_index": 0, "outcome": "Yes",
                "size": 100.0, "avg_price": 0.32, "current_value_usdc": 40.0,
                "slug": "btc-100k-eoy", "title": "Bitcoin closes 2026 above $100k",
                "event_slug": "btc-eoy-2026",
                "current_outcome_price": 0.40, "snapshot_id": "snapshot-ghost",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
        ],
    )
    _write_jsonl(
        d / "polymarket_position_snapshots.jsonl",
        [
            {"proxy_wallet": "0xalpha", "snapshot_id": "snapshot-alpha", "complete": True},
            {"proxy_wallet": "0xnoob", "snapshot_id": "snapshot-noob", "complete": True},
            {"proxy_wallet": "0xghost", "snapshot_id": "snapshot-ghost", "complete": True},
        ],
    )

    _write_jsonl(
        d / "polymarket_activity.jsonl",
        [
            # alpha bought fed-50bp twice; latest should be the entry shown.
            {
                "proxy_wallet": "0xalpha", "timestamp": 1731000000,
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "BUY",
                "size": 3000.0, "usdc_size": 1350.0, "price": 0.45,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed-50bp-sep",
                "title": "Fed cuts 50bp in September", "event_slug": "fed-decision",
                "transaction_hash": "0xtx_old",
            },
            {
                "proxy_wallet": "0xalpha", "timestamp": 1731500000,  # NEWER
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "BUY",
                "size": 2000.0, "usdc_size": 900.0, "price": 0.45,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed-50bp-sep",
                "title": "Fed cuts 50bp in September", "event_slug": "fed-decision",
                "transaction_hash": "0xtx_new",
            },
            # alpha BTC buy — older entry, still held.
            {
                "proxy_wallet": "0xalpha", "timestamp": 1730000000,
                "condition_id": "0xcond_btc", "type": "TRADE", "side": "BUY",
                "size": 2000.0, "usdc_size": 600.0, "price": 0.30,
                "outcome_index": 0, "outcome": "Yes", "slug": "btc-100k-eoy",
                "title": "Bitcoin closes 2026 above $100k", "event_slug": "btc-eoy-2026",
                "transaction_hash": "0xtx_btc",
            },
            # alpha's exited position — has a buy event but position is closed.
            {
                "proxy_wallet": "0xalpha", "timestamp": 1729000000,
                "condition_id": "0xcond_exit", "type": "TRADE", "side": "BUY",
                "size": 100.0, "usdc_size": 55.0, "price": 0.55,
                "outcome_index": 0, "outcome": "Yes", "slug": "exited",
                "title": "Already exited", "event_slug": "exited-event",
                "transaction_hash": "0xtx_exit",
            },
            # alpha SELL event on fed — must be ignored (we only count BUYs).
            {
                "proxy_wallet": "0xalpha", "timestamp": 1731600000,
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "SELL",
                "size": 500.0, "usdc_size": 250.0, "price": 0.50,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed-50bp-sep",
                "title": "Fed cuts 50bp in September", "event_slug": "fed-decision",
                "transaction_hash": "0xtx_sell",
            },
            # noob's BUY — wallet excluded earlier so this never reaches output.
            {
                "proxy_wallet": "0xnoob", "timestamp": 1731000000,
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "BUY",
                "size": 10.0, "usdc_size": 4.5, "price": 0.45,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed-50bp-sep",
                "title": "Fed cuts 50bp in September", "event_slug": "fed-decision",
                "transaction_hash": "0xtx_noob",
            },
        ],
    )

    # Live market prices so the feed can carry current_market_yes_price.
    _write_jsonl(
        d / "polymarket_markets.jsonl",
        [
            {
                "gamma_id": "1", "condition_id": "0xcond_fed", "slug": "fed-50bp-sep",
                "question": "Fed cuts 50bp in September", "category": "economics",
                "end_date": "2026-09-18T00:00:00Z",
                "outcomes": ["Yes", "No"], "outcome_prices": [0.58, 0.42],
                "volume_usdc": 100000, "liquidity_usdc": 5000,
                "closed": False, "active": True,
                "last_trade_price": 0.58, "best_bid": 0.57, "best_ask": 0.59,
                "fetched_at": "2026-05-12T08:00:00Z",
            },
            {
                "gamma_id": "2", "condition_id": "0xcond_btc", "slug": "btc-100k-eoy",
                "question": "Bitcoin closes 2026 above $100k", "category": "crypto",
                "end_date": "2026-12-31T00:00:00Z",
                "outcomes": ["Yes", "No"], "outcome_prices": [0.40, 0.60],
                "volume_usdc": 50000, "liquidity_usdc": 2500,
                "closed": False, "active": True,
                "last_trade_price": 0.40, "best_bid": 0.39, "best_ask": 0.41,
                "fetched_at": "2026-05-12T08:00:00Z",
            },
        ],
    )

    return d


def test_skilled_bets_orders_best_tail_ev_first_within_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feed semantics: within the same remaining-edge tier, the entry with
    the best tail EV at today's price ranks first; the latest of multiple
    buys on the same position is what gets shown."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20")
    assert resp.status_code == 200
    rows = resp.json()
    # alpha has TWO held positions with matching buys; both should appear.
    assert len(rows) == 2

    # Both entries are "fresh"; btc has less of its move priced in relative
    # to the wallet's implied fair price, so its tail EV (−5.6¢) beats
    # fed's (−8.0¢) and it ranks first despite the older BUY timestamp.
    assert rows[0]["condition_id"] == "0xcond_btc"
    assert rows[0]["bought_at"] == 1730000000
    assert rows[1]["condition_id"] == "0xcond_fed"
    assert rows[1]["bought_at"] == 1731500000
    assert rows[1]["transaction_hash"] == "0xtx_new", \
        "Should reflect the LATEST buy, not the older one"


def test_skilled_bets_carries_tail_ev_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row must carry the wallet-edge-implied fair price and the EV of
    tailing at TODAY's price. Alpha's conservative edge bound is 0.2
    log-odds; fed entry 0.45 → fair sigmoid(logit(0.45)+0.2) ≈ 0.4998,
    now 0.58 → EV ≈ −8.0¢ (thesis already priced past fair)."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    rows = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20").json()
    fed = next(r for r in rows if r["condition_id"] == "0xcond_fed")
    assert fed["tail_edge_used"] == 0.2
    assert fed["tail_fair_price"] == 0.4998
    assert fed["tail_ev"] == -0.0802
    assert fed["tail_ev_status"] == "negative"

    btc = next(r for r in rows if r["condition_id"] == "0xcond_btc")
    assert btc["tail_fair_price"] == 0.3436
    assert btc["tail_ev"] == -0.0564
    assert btc["tail_ev_status"] == "negative"


def test_skilled_bets_min_tail_ev_filters_and_drops_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    # Both fixture rows carry negative EV — a floor of 0 empties the feed.
    rows = client.get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20&min_tail_ev=0"
    ).json()
    assert rows == []

    # A floor between the two EVs (−5.6¢ and −8.0¢) keeps only btc.
    rows = client.get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20&min_tail_ev=-0.06"
    ).json()
    assert [r["condition_id"] for r in rows] == ["0xcond_btc"]


def test_skilled_bets_category_fit_caps_tail_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proven category fit (>=5 independent events) replaces the wallet
    edge in the EV math when it is more conservative; a thin category falls
    back to the wallet-level score. Category matching is case-insensitive."""
    pm_dir = _seed(tmp_path)
    enrichment_path = pm_dir / "polymarket_wallet_enrichment.jsonl"
    rows = [
        json.loads(line)
        for line in enrichment_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    for row in rows:
        if row["proxy_wallet"] == "0xalpha":
            row["category_edges"] = [
                # Proven but weaker than the wallet's 0.2 lifetime bound.
                {"category": "Economics", "skill_likelihood": 0.97,
                 "edge_mean": 0.3, "edge_lower_bound": 0.1,
                 "independent_events": 25.0},
                # Too thin to trust despite a huge bound.
                {"category": "crypto", "skill_likelihood": 0.9,
                 "edge_mean": 0.5, "edge_lower_bound": 0.4,
                 "independent_events": 2.0},
            ]
    _write_jsonl(enrichment_path, rows)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    feed = TestClient(app).get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20"
    ).json()

    fed = next(r for r in feed if r["condition_id"] == "0xcond_fed")
    assert fed["category_skill_source"] == "category"
    assert fed["category_skill_likelihood"] == 0.97
    assert fed["category_edge_lower_bound"] == 0.1
    assert fed["category_independent_events"] == 25.0
    # min(lifetime 0.2, economics 0.1) → fair sigmoid(logit(0.45)+0.1) ≈ 0.4749.
    assert fed["tail_edge_used"] == 0.1
    assert fed["tail_fair_price"] == 0.4749
    assert fed["tail_ev"] == -0.1051

    btc = next(r for r in feed if r["condition_id"] == "0xcond_btc")
    assert btc["category_skill_source"] == "wallet_fallback"
    # Echoes the wallet-level score; reports the thin category's sample size.
    assert btc["category_skill_likelihood"] == btc["skill_likelihood"]
    assert btc["category_independent_events"] == 2.0
    assert btc["tail_edge_used"] == 0.2  # crypto fit too thin to cap


def test_tail_ev_math_statuses() -> None:
    from marketsignalos_api.services.skilled_bets import _tail_ev

    # Positive: fair well above the live price.
    fair, ev, status = _tail_ev(0.30, 0.30, 0.2)
    assert fair == 0.3436
    assert ev == 0.0436
    assert status == "positive"

    # Marginal: inside the 2¢ spread/fee buffer.
    fair, ev, status = _tail_ev(0.30, 0.33, 0.2)
    assert ev == 0.0136
    assert status == "marginal"

    # Negative: priced past the wallet-implied fair price.
    _, ev, status = _tail_ev(0.30, 0.40, 0.2)
    assert ev == -0.0564
    assert status == "negative"

    # Unknown when either price is unobserved.
    assert _tail_ev(0.0, 0.40, 0.2)[2] == "unknown"
    assert _tail_ev(0.30, 0.0, 0.2)[2] == "unknown"


def test_skilled_bets_excludes_low_skill_and_exited_and_unknown_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20")
    rows = resp.json()
    wallets = {r["proxy_wallet"] for r in rows}
    # Noob fails min_skill; ghost has no matching BUY event; alpha's fully
    # exited position has size=0 in the latest snapshot.
    assert wallets == {"0xalpha"}
    conditions = {r["condition_id"] for r in rows}
    assert "0xcond_exit" not in conditions, "fully exited position must not surface"


def test_skilled_bets_carries_deep_link_urls_and_current_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row must carry working Polymarket URLs and the live market price
    so the consumer can decide whether the entry is still attractive."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20")
    fed_row = next(r for r in resp.json() if r["condition_id"] == "0xcond_fed")

    assert fed_row["polymarket_profile_url"] == "https://polymarket.com/profile/0xalpha"
    assert fed_row["polymarket_market_url"] == "https://polymarket.com/event/fed-decision"
    assert fed_row["wallet_price_lead_score"] == 0.75
    assert fed_row["entry_price"] == 0.45
    # Live market has moved from 0.45 (entry) to 0.58 — caller can compute drift.
    assert fed_row["current_market_yes_price"] == 0.58
    assert fed_row["current_position_size"] == 5000.0
    assert fed_row["tradability"] == "poly_direct"


def test_skilled_bets_hides_closed_markets_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    markets_path = pm_dir / "polymarket_markets.jsonl"
    rows = json.loads(markets_path.read_text(encoding="utf-8").strip().splitlines()[1])
    rows["closed"] = True
    rows["active"] = False
    markets_path.write_text(
        markets_path.read_text(encoding="utf-8").splitlines()[0]
        + "\n"
        + json.dumps(rows, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    default_rows = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20").json()
    assert all(row["condition_id"] != "0xcond_btc" for row in default_rows)

    all_rows = client.get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20&include_untradable=true"
    ).json()
    closed_row = next(row for row in all_rows if row["condition_id"] == "0xcond_btc")
    assert closed_row["tradability"] == "closed"


def test_skilled_bets_pending_kalshi_mirror_has_no_tail_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    _write_jsonl(
        pm_dir / "market_links.jsonl",
        [
            {
                "kalshi_ticker": "KXFED-PENDING",
                "polymarket_condition_id": "0xcond_fed",
                "polymarket_slug": "fed-50bp-sep",
                "kalshi_title": "Pending mirror",
                "polymarket_title": "Fed cuts 50bp in September",
                "kalshi_end_date": "2026-09-18T00:00:00Z",
                "polymarket_end_date": "2026-09-18T00:00:00Z",
                "confidence": 0.55,
                "status": "pending",
                "matched_by": "auto",
                "matched_at": "2026-05-12T08:00:00Z",
            },
        ],
    )
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    fed = next(
        row
        for row in TestClient(app)
        .get("/signals/skilled-bets?min_skill=0.9&min_resolved=20")
        .json()
        if row["condition_id"] == "0xcond_fed"
    )
    assert fed["tradability"] == "poly_direct"
    assert fed["kalshi_match_status"] == "pending"
    assert fed["kalshi_market_url"] == ""


def test_skilled_bets_min_position_value_filters_dust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    # Alpha's BTC position is worth $800; raise the floor above it.
    resp = client.get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20&min_position_value_usdc=1000"
    )
    rows = resp.json()
    # Only the fed position ($2900) clears the bar.
    assert len(rows) == 1
    assert rows[0]["condition_id"] == "0xcond_fed"


def test_skilled_bets_surfaces_kalshi_mirror_when_match_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Central product behavior: when the Polymarket → Kalshi matcher has
    found an equivalent Kalshi market for the Polymarket condition_id the
    skilled wallet is holding, the bet must carry the Kalshi ticker,
    title, deep link, and current YES price so the user can tail it."""
    pm_dir = _seed(tmp_path)

    # Two candidate matches for the same condition_id — the approved /
    # higher-confidence one should win, and a rejected link must be
    # ignored entirely.
    _write_jsonl(
        pm_dir / "market_links.jsonl",
        [
            {
                "kalshi_ticker": "KXFED-26SEP-50BP",
                "polymarket_condition_id": "0xcond_fed",
                "polymarket_slug": "fed-50bp-sep",
                "kalshi_title": "Fed September 50bp cut",
                "polymarket_title": "Fed cuts 50bp in September",
                "kalshi_end_date": "2026-09-18T00:00:00Z",
                "polymarket_end_date": "2026-09-18T00:00:00Z",
                "confidence": 0.92,
                "status": "approved",
                "matched_by": "auto",
                "matched_at": "2026-05-12T08:00:00Z",
            },
            {
                "kalshi_ticker": "KXFED-26SEP-OLD",
                "polymarket_condition_id": "0xcond_fed",
                "polymarket_slug": "fed-50bp-sep",
                "kalshi_title": "Older candidate",
                "polymarket_title": "Fed cuts 50bp in September",
                "kalshi_end_date": "2026-09-18T00:00:00Z",
                "polymarket_end_date": "2026-09-18T00:00:00Z",
                "confidence": 0.99,
                "status": "rejected",
                "matched_by": "manual",
                "matched_at": "2026-05-12T07:00:00Z",
            },
        ],
    )
    _write_jsonl(
        pm_dir / "kalshi_markets.jsonl",
        [
            {
                "ticker": "KXFED-26SEP-50BP",
                "event_ticker": "KXFED-26SEP",
                "title": "Fed cuts 50 bps at the September meeting",
                "subtitle": "",
                "yes_sub_title": "",
                "category": "Economics",
                "status": "open",
                "expiration_time": "2026-09-18T00:00:00Z",
                "close_time": "2026-09-18T00:00:00Z",
                "yes_bid": 55,
                "yes_ask": 57,
                "last_price": 56,
                "fetched_at": "2026-05-12T08:00:00Z",
            },
        ],
    )

    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    resp = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20")
    rows = resp.json()
    fed = next(r for r in rows if r["condition_id"] == "0xcond_fed")

    assert fed["kalshi_ticker"] == "KXFED-26SEP-50BP"
    assert fed["kalshi_event_ticker"] == "KXFED-26SEP"
    # Live title from kalshi_markets.jsonl (not the snapshot in market_links).
    assert fed["kalshi_title"] == "Fed cuts 50 bps at the September meeting"
    assert fed["kalshi_market_url"] == "https://kalshi.com/markets/kxfed-26sep"
    # Mid of 55/57 cents -> 0.56.
    assert fed["kalshi_yes_price"] == 0.56
    assert fed["kalshi_match_confidence"] == 0.92
    assert fed["kalshi_match_status"] == "approved"

    # The btc condition has no link → mirror fields must be empty so the UI
    # can hide the Kalshi CTA cleanly.
    btc = next(r for r in rows if r["condition_id"] == "0xcond_btc")
    assert btc["kalshi_ticker"] == ""
    assert btc["kalshi_market_url"] == ""
    assert btc["kalshi_yes_price"] == 0.0
    assert btc["kalshi_match_status"] == ""


def test_skilled_bets_empty_when_no_skilled_wallets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    # Set bar above alpha's 0.99 → no skilled wallets → empty feed.
    resp = client.get("/signals/skilled-bets?min_skill=0.999&min_resolved=20")
    assert resp.json() == []


def test_skilled_bets_summary_reports_quarantine_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    body = TestClient(app).get("/signals/skilled-bets/summary").json()
    assert body["trusted"] == 3
    assert body["rebuilding"] == 0
    assert body["blocked"] == 1
    assert body["tailable"] == 2
    assert body["blocked_reasons"] == {"legacy or incomplete score": 1}
    assert body["feed_poly_direct"] == 2
    assert body["feed_actionable"] == 2
    assert body["latest_signal_at"] == 1731500000


def test_research_leaderboard_keeps_blocked_wallets_with_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    rows = TestClient(app).get(
        "/signals/polymarket-leaderboard?min_resolved=1&tailability=blocked"
    ).json()
    assert [row["proxy_wallet"] for row in rows] == ["0xnoob"]
    assert rows[0]["tailability_status"] == "blocked"


def test_skilled_bets_drops_positions_absent_from_latest_complete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    _write_jsonl(
        pm_dir / "polymarket_position_snapshots.jsonl",
        [{"proxy_wallet": "0xalpha", "snapshot_id": "snapshot-empty", "complete": True}],
    )
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    rows = TestClient(app).get("/signals/skilled-bets?min_skill=0.9&min_resolved=20").json()
    assert rows == []


def test_skilled_bets_carries_remaining_edge_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row reports how much of the entry→$1 move the market has already
    made, so a tailing user can tell fresh entries from priced-in ones."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows = TestClient(app).get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20"
    ).json()
    fed = next(r for r in rows if r["condition_id"] == "0xcond_fed")
    # entry 0.45 → now 0.58: (0.58 - 0.45) / (1 - 0.45) = 0.2364 → fresh.
    assert fed["move_captured_pct"] == 0.2364
    assert fed["remaining_edge_status"] == "fresh"
    btc = next(r for r in rows if r["condition_id"] == "0xcond_btc")
    # entry 0.30 → now 0.40: 0.10 / 0.70 = 0.1429 → fresh.
    assert btc["move_captured_pct"] == 0.1429
    assert btc["remaining_edge_status"] == "fresh"


def _make_fed_position_late(pm_dir: Path) -> None:
    """Rewrite the fed position so its picked outcome now trades at 0.95 —
    91% of the 0.45→$1 move already captured."""
    positions_path = pm_dir / "polymarket_positions.jsonl"
    rows = [
        json.loads(line)
        for line in positions_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    for row in rows:
        if row["condition_id"] == "0xcond_fed" and row["proxy_wallet"] == "0xalpha":
            row["current_outcome_price"] = 0.95
    _write_jsonl(positions_path, rows)


def test_skilled_bets_down_ranks_late_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fed entry is NEWER than btc, but once its move is >75% captured it
    must sort below the still-fresh btc signal."""
    pm_dir = _seed(tmp_path)
    _make_fed_position_late(pm_dir)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows = TestClient(app).get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20"
    ).json()
    assert [r["condition_id"] for r in rows] == ["0xcond_btc", "0xcond_fed"]
    fed = rows[1]
    assert fed["remaining_edge_status"] == "late"
    # (0.95 - 0.45) / 0.55 = 0.9091
    assert fed["move_captured_pct"] == 0.9091


def test_skilled_bets_max_move_captured_filters_late_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    _make_fed_position_late(pm_dir)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows = TestClient(app).get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20&max_move_captured=0.75"
    ).json()
    assert [r["condition_id"] for r in rows] == ["0xcond_btc"]


def test_skilled_bets_flags_high_conviction_and_boosts_rank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BUY that is >=2 standard deviations above the wallet's own historical
    sizing is "high" conviction and sorts above NEWER normal-sized entries
    within the same remaining-edge rank. Wallets with <5 prior buys stay
    "unknown" (alpha's seed history has only 4 buys)."""
    pm_dir = _seed(tmp_path)
    activity_path = pm_dir / "polymarket_activity.jsonl"
    rows = [
        json.loads(line)
        for line in activity_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    # Six small historical buys on an unrelated market plus one huge buy on
    # the held BTC position: mean≈757, std≈1732 → z≈2.45 for the $5000 buy.
    for i in range(6):
        rows.append(
            {
                "proxy_wallet": "0xalpha", "timestamp": 1729000000 + i,
                "condition_id": "0xcond_hist", "type": "TRADE", "side": "BUY",
                "size": 100.0, "usdc_size": 50.0, "price": 0.5,
                "outcome_index": 0, "outcome": "Yes", "slug": "hist",
                "title": "History", "event_slug": f"hist-{i}",
                "transaction_hash": f"0xtx_hist_{i}",
            }
        )
    rows.append(
        {
            "proxy_wallet": "0xalpha", "timestamp": 1731000001,  # OLDER than fed buy
            "condition_id": "0xcond_btc", "type": "TRADE", "side": "BUY",
            "size": 10000.0, "usdc_size": 5000.0, "price": 0.50,
            "outcome_index": 0, "outcome": "Yes", "slug": "btc-100k-eoy",
            "title": "Bitcoin closes 2026 above $100k", "event_slug": "btc-eoy-2026",
            "transaction_hash": "0xtx_btc_huge",
        }
    )
    _write_jsonl(activity_path, rows)
    _write_jsonl(
        pm_dir / "polymarket_wallet_values.jsonl",
        [{"proxy_wallet": "0xalpha", "value_usdc": 20000.0,
          "snapshot_at": "2026-05-12T08:00:00Z"}],
    )
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    rows_out = TestClient(app).get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20"
    ).json()
    btc = next(r for r in rows_out if r["condition_id"] == "0xcond_btc")
    fed = next(r for r in rows_out if r["condition_id"] == "0xcond_fed")

    assert btc["conviction"] == "high"
    assert btc["conviction_z"] >= 2.0
    # $5000 entry against a $20k bankroll.
    assert btc["entry_pct_of_bankroll"] == 0.25
    # Both rows are "fresh", and the fed BUY is newer — but high conviction
    # outranks recency within the same remaining-edge rank.
    assert [r["condition_id"] for r in rows_out].index("0xcond_btc") < [
        r["condition_id"] for r in rows_out
    ].index("0xcond_fed")
    # Fed entry is normal-sized for this wallet (now 11 observed buys).
    assert fed["conviction"] in ("normal", "low")


def test_skilled_bets_conviction_unknown_with_thin_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alpha's base seed has only 4 BUYs — too thin to standardize against."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    rows = TestClient(app).get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20"
    ).json()
    assert all(r["conviction"] == "unknown" for r in rows)
    assert all(r["conviction_z"] == 0.0 for r in rows)


def test_legacy_enrichment_is_quarantined_from_active_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    enrichment = pm_dir / "polymarket_wallet_enrichment.jsonl"
    rows = [
        json.loads(line)
        for line in enrichment.read_text(encoding="utf-8").splitlines()
        if line
    ]
    rows[0].pop("score_version")
    _write_jsonl(enrichment, rows)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    signals = TestClient(app).get("/signals/skilled-bets?min_skill=0.9&min_resolved=20").json()
    assert signals == []


def _set_alpha_style(pm_dir: Path) -> None:
    """Mark alpha's enrichment row as a systematic wallet."""
    enrichment = pm_dir / "polymarket_wallet_enrichment.jsonl"
    rows = [
        json.loads(line)
        for line in enrichment.read_text(encoding="utf-8").splitlines()
        if line
    ]
    for row in rows:
        if row["proxy_wallet"] == "0xalpha":
            row["style_archetype"] = "systematic"
            row["automation_score"] = 0.91
            row["style_drivers"] = ["112.0 trades per active day"]
    _write_jsonl(enrichment, rows)


def test_skilled_bets_carries_wallet_archetype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feed rows carry the wallet's trading-style archetype; enrichment rows
    written before the field existed default to 'unclassified'."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)

    rows = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20").json()
    assert all(r["wallet_archetype"] == "unclassified" for r in rows)
    assert all(r["wallet_automation_score"] == 0.0 for r in rows)

    _set_alpha_style(pm_dir)
    rows = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20").json()
    assert rows and all(r["wallet_archetype"] == "systematic" for r in rows)
    assert all(r["wallet_automation_score"] == 0.91 for r in rows)


def test_leaderboard_filters_by_archetype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    _set_alpha_style(pm_dir)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)

    systematic = client.get(
        "/signals/polymarket-leaderboard?min_resolved=1&archetype=systematic"
    ).json()
    assert [row["proxy_wallet"] for row in systematic] == ["0xalpha"]
    assert systematic[0]["automation_score"] == 0.91
    assert systematic[0]["style_drivers"] == ["112.0 trades per active day"]

    unclassified = client.get(
        "/signals/polymarket-leaderboard?min_resolved=1&archetype=unclassified"
    ).json()
    assert "0xalpha" not in {row["proxy_wallet"] for row in unclassified}
    assert {row["style_archetype"] for row in unclassified} == {"unclassified"}
