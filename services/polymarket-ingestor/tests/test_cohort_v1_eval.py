from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import cohort_v1, cohort_v1_eval
from marketsignalos_polymarket.cohort_v1 import config_hash
from marketsignalos_polymarket.cohort_v1_eval import evaluate, price_at, summarize

OPENS = datetime(2026, 10, 22, tzinfo=UTC)
EVAL = OPENS + timedelta(days=30)
T2 = "0x" + "2" * 40
COMP = "0x" + "c" * 40
HOUR = 3600


def _config(**window: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "cohort_id": "cohort-v1",
        "score_generation": {"run_id": "gen1"},
        "tiers": {"T1": [], "T2": [T2], "T3": [T2, COMP, "0x" + "3" * 40]},
        "comparison": {"pairs": {T2: COMP}},
        "ablations": {"rules": {}, "clv_only": [COMP]},
        "window": {"opens": OPENS.isoformat(), "evaluation_date": EVAL.isoformat(),
                   "under_powered": False, **window},
    }
    config["config_hash"] = config_hash(config)
    return config


def _signal(sid: str, wallet: str, event: str, *, day: int = 1, status: str = "captured",
            all_in: float = 0.50, vwap: float = 0.49, **extra: Any) -> dict[str, Any]:
    detected = OPENS + timedelta(days=day)
    row: dict[str, Any] = {
        "signal_id": sid, "wallet": wallet, "condition_id": f"0x{event}",
        "outcome_index": 0, "event_slug": event, "detected_at": detected.isoformat(),
        "membership_mode": "frozen", "status": status, "wallet_usdc": 40.0,
        "wallet_shares": 100.0, "book": {"best_ask": 0.50, "best_bid": 0.48},
        "exclusion_reasons": [] if status == "captured" else ["no book: no asks"]}
    if status == "captured":
        row["clip"] = {"all_in_price": all_in, "vwap": vwap}
    return row | extra


def _write(data: Path, config: dict[str, Any], signals: list[dict[str, Any]],
           prices: dict[str, list[tuple[int, float]]]) -> None:
    stage = data / "cohort-v1"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "signals.jsonl").write_text("".join(
        json.dumps({"config_hash": config["config_hash"]} | s) + "\n" for s in signals))
    (stage / "price_observations.jsonl").write_text("".join(
        json.dumps({"signal_id": sid, "price": p,
                    "event_time": datetime.fromtimestamp(t, UTC).isoformat()}) + "\n"
        for sid, points in prices.items() for t, p in points))
    gen = data / "score-snapshots" / "gen1"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "polymarket_wallet_enrichment.jsonl").write_text(
        json.dumps({"proxy_wallet": T2, "edge_mean": 0.4}) + "\n")
    (data / "polymarket_markets.jsonl").write_text(
        json.dumps({"condition_id": "0xe1", "closed": True, "outcome_prices": [1.0, 0.0],
                    "fetched_at": "2026-11-20T00:00:00Z"}) + "\n")


def _at(signal: dict[str, Any], price: float, offset: int = HOUR) -> tuple[int, float]:
    detected = datetime.fromisoformat(signal["detected_at"])
    return (int(detected.timestamp()) + offset - 60, price)


@pytest.fixture
def data(tmp_path: Path) -> Path:
    config = _config()
    a = _signal("a", T2, "e1")
    b = _signal("b", T2, "e1", day=2)
    c = _signal("c", T2, "e2", day=3)
    d = _signal("d", COMP, "e3", day=4)
    signals = [
        a, b, c, d,
        _signal("x", T2, "e4", day=5, status="excluded"),
        _signal("early", T2, "e1", day=-1),  # before the window opens
        _signal("prov", T2, "e1", membership_mode="provisional"),
        _signal("other", T2, "e1", config_hash="not-this-config"),
        _signal("late", T2, "e1", day=31),  # after the evaluation date
        _signal("gap", T2, "e5", day=6),  # no price at +1 h
    ]
    detected = {s["signal_id"]: s for s in signals}
    prices = {
        "a": [_at(a, 0.50, 0), _at(a, 0.53)], "b": [_at(b, 0.50, 0), _at(b, 0.55)],
        "c": [_at(c, 0.50, 0), _at(c, 0.49)], "d": [_at(d, 0.50, 0), _at(d, 0.52)],
        "x": [_at(detected["x"], 0.40, 0), _at(detected["x"], 0.41)],
        "gap": [_at(detected["gap"], 0.50, 0)],
    }
    _write(tmp_path, config, signals, prices)
    (tmp_path / "cohort-v1" / "frozen-config.json").write_text(json.dumps(config))
    return tmp_path


def test_a_price_counts_only_within_the_tolerance_before_the_horizon() -> None:
    series = [(100, 0.4), (1000, 0.5)]
    assert price_at(series, 1000) == 0.5 and price_at(series, 1500) == 0.5
    assert price_at(series, 1000 + 15 * 60 + 1) is None  # the last point is too old
    assert price_at(series, 50) is None


def test_events_weigh_equally_and_the_bootstrap_is_reproducible() -> None:
    values = [("A", 1.0), ("A", 3.0), ("B", 10.0)]
    summary = summarize(values)
    assert summary["mean"] == pytest.approx(6.0)  # (2 + 10) / 2, not 14 / 3
    assert (summary["signals"], summary["events"]) == (3, 2)
    assert summarize(values) == summary  # fixed seed
    low, high = summary["ci95"]
    assert low <= summary["lower95_one_sided"] <= summary["mean"] <= high
    assert summarize([])["mean"] is None


def test_the_evaluation_refuses_to_peek() -> None:
    with pytest.raises(ValueError, match="no peeking"):
        evaluate(Path("."), _config(), now=EVAL - timedelta(seconds=1))
    tampered = _config() | {"primary_tier": "T3"}
    with pytest.raises(ValueError, match="hash"):
        evaluate(Path("."), tampered, now=EVAL)


def test_the_primary_outcome_is_t2s_event_weighted_net_improvement(data: Path) -> None:
    result = evaluate(data, _config(), now=EVAL + timedelta(days=1))
    # In window, frozen, this config: a, b, c, d, x and gap.
    assert result["signals"]["in_window"] == 6 and result["signals"]["captured"] == 5
    assert result["signals"]["excluded_by_reason"] == {"no book: no asks": 1}
    assert result["signals"]["outcome_missing_by_reason"] == {
        "1h: no price within 15 min before +1h": 1, "6h: no price within 15 min before +6h": 5}
    primary = result["primary"]
    # e1: (0.53 - 0.50 + 0.55 - 0.50) / 2 = 0.04; e2: 0.49 - 0.50 = -0.01.
    assert primary["mean"] == pytest.approx((0.04 - 0.01) / 2)
    assert (primary["events"], primary["signals"]) == (2, 3)
    assert result["groups"]["T2"]["1h_gross"]["mean"] == pytest.approx((0.05 + 0.0) / 2)
    assert result["groups"]["comparison"]["1h_net"]["mean"] == pytest.approx(0.02)
    assert result["ablations"]["clv_only"]["mean"] == pytest.approx(0.02)
    assert result["ablations"]["combined"]["mean"] is None  # T1 is empty
    assert result["t2_minus_comparison"]["1h"]["mean"] == pytest.approx(0.015 - 0.02)
    assert result["verdict"] in {"edge_after_costs", "null"}
    # T2's settlement: e1 resolved for outcome 0 (two wins), e2 and e5 unresolved.
    assert result["settlement_roi"]["censored"] == 2
    assert result["settlement_roi"]["mean"] == pytest.approx(1.0)  # (1 - 0.5) / 0.5
    model = result["calibration"]["model"]
    assert model["n"] == 2 and model["brier"] < result["calibration"]["market_mid"]["brier"]
    # The excluded signal's mark moved too: it is checked, never priced.
    assert result["exclusion_check_1h_move"]["excluded"]["signals"] == 1


def test_a_declared_under_powered_null_is_inconclusive(tmp_path: Path) -> None:
    config = _config(under_powered=True)
    s = _signal("a", T2, "e1")
    _write(tmp_path, config, [s], {"a": [_at(s, 0.40)]})
    assert evaluate(tmp_path, config, now=EVAL)["verdict"] == "inconclusive"
    config = _config()
    assert evaluate(tmp_path / "x", config, now=EVAL)["verdict"] == "null"  # no signals


def test_the_pilot_evaluates_once_a_day_after_the_evaluation_date(
    data: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    run = cohort_v1.run
    early = run(data, now=EVAL + timedelta(hours=23), freeze_at=OPENS, discovery={})
    assert "evaluated" not in early and not (data / "cohort-v1" / "result.json").exists()
    with caplog.at_level(logging.INFO, logger="marketsignalos.polymarket.cohort_v1"):
        due = run(data, now=EVAL + timedelta(hours=24), freeze_at=OPENS, discovery={})
    assert due["evaluated"] and due["verdict"] in {"edge_after_costs", "null"}
    stored = json.loads((data / "cohort-v1" / "result.json").read_text())
    assert stored["config_hash"] == _config()["config_hash"]
    assert any("cohort v1 result part 0" in r.getMessage() for r in caplog.records)
    later = run(data, now=EVAL + timedelta(days=2), freeze_at=OPENS, discovery={})
    assert "evaluated" not in later  # one look
    with pytest.raises(FileExistsError):
        cohort_v1_eval.write_once(data, stored)
