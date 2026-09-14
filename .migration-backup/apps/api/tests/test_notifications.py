from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.main import app
from marketsignalos_api.services import notifications


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )


def _ledger_row(cond: str, recorded_at: str) -> dict[str, object]:
    return {
        "proxy_wallet": "0xalpha", "condition_id": cond, "outcome_index": 0,
        "wallet_name": "AlphaWhale", "outcome": "Yes", "title": f"Market {cond}",
        "event_slug": f"event-{cond}", "tradability": "poly_direct",
        "skill_likelihood": 0.99, "edge_lower_bound": 0.2,
        "entry_price": 0.45, "surface_price": 0.58, "surface_kalshi_price": 0.0,
        "bought_at": 1731500000, "recorded_at": recorded_at,
        "status": "open", "settled_at": "",
        "roi_at_surface": None, "roi_at_entry": None,
    }


def _exit_row(cond: str, detected_at: str) -> dict[str, object]:
    return {
        "proxy_wallet": "0xalpha", "wallet_name": "AlphaWhale",
        "skill_likelihood": 0.99, "condition_id": cond, "outcome_index": 0,
        "outcome": "Yes", "title": f"Market {cond}", "event_slug": f"event-{cond}",
        "exit_type": "closed", "previous_size": 5000.0, "current_size": 0.0,
        "reduction_pct": 1.0, "previous_value_usdc": 2900.0,
        "current_outcome_price": 0.58,
        "from_snapshot_id": "snapA", "to_snapshot_id": "snapB",
        "from_snapshot_at": "2026-06-09T08:00:00Z",
        "to_snapshot_at": "2026-06-10T08:00:00Z",
        "was_surfaced": True, "surfaced_recorded_at": "2026-06-09T09:00:00Z",
        "polymarket_market_url": f"https://polymarket.com/event/event-{cond}",
        "polymarket_profile_url": "https://polymarket.com/profile/0xalpha",
        "detected_at": detected_at,
    }


@pytest.fixture()
def captured_posts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the webhook transport with an in-memory recorder."""
    calls: list[dict[str, Any]] = []

    def _fake_post(url: str, payload: dict[str, Any]) -> None:
        calls.append({"url": url, "payload": payload})

    monkeypatch.setattr(notifications, "_post_webhook", _fake_post)
    return calls


def test_first_pass_initializes_and_never_floods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    pm_dir = tmp_path / "pmdata"
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    monkeypatch.setenv("SIGNAL_WEBHOOK_URL", "https://hooks.test/notify")
    # A pre-existing backlog that must NOT be delivered on the first pass.
    _write_jsonl(pm_dir / "signal_ledger.jsonl", [_ledger_row("0xc1", "2026-06-09T10:00:00+00:00")])

    body = TestClient(app).post("/signals/notifications/run").json()
    assert body["initialized"] is True
    assert body["sent"] is False
    assert captured_posts == []


def test_delivers_only_items_newer_than_watermark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    pm_dir = tmp_path / "pmdata"
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    monkeypatch.setenv("SIGNAL_WEBHOOK_URL", "https://hooks.test/notify")
    _write_jsonl(pm_dir / "signal_ledger.jsonl", [_ledger_row("0xc1", "2026-06-09T10:00:00+00:00")])
    client = TestClient(app)
    client.post("/signals/notifications/run")  # initialize

    # New signal + new exit arrive after initialization.
    _write_jsonl(
        pm_dir / "signal_ledger.jsonl",
        [
            _ledger_row("0xc1", "2026-06-09T10:00:00+00:00"),  # old — already seen
            _ledger_row("0xc2", "2026-06-10T11:00:00+00:00"),  # new
        ],
    )
    _write_jsonl(pm_dir / "exit_signals.jsonl", [_exit_row("0xc1", "2026-06-10T11:30:00+00:00")])

    body = client.post("/signals/notifications/run").json()
    assert body["sent"] is True
    assert body["new_signals"] == 1
    assert body["new_exits"] == 1
    assert len(captured_posts) == 1
    payload = captured_posts[0]["payload"]
    assert captured_posts[0]["url"] == "https://hooks.test/notify"
    assert [s["title"] for s in payload["new_signals"]] == ["Market 0xc2"]
    assert payload["new_signals"][0]["polymarket_market_url"] == (
        "https://polymarket.com/event/event-0xc2"
    )
    assert [e["exit_type"] for e in payload["new_exits"]] == ["closed"]
    assert payload["truncated"] is False

    # Third pass: watermark advanced, nothing new.
    body = client.post("/signals/notifications/run").json()
    assert body["sent"] is False
    assert body["new_signals"] == 0
    assert len(captured_posts) == 1


def test_failed_delivery_retries_same_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = tmp_path / "pmdata"
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    monkeypatch.setenv("SIGNAL_WEBHOOK_URL", "https://hooks.test/notify")
    client = TestClient(app)
    client.post("/signals/notifications/run")  # initialize (empty streams)

    _write_jsonl(pm_dir / "signal_ledger.jsonl", [_ledger_row("0xc9", "2026-06-10T12:00:00+00:00")])

    def _boom(url: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(notifications, "_post_webhook", _boom)
    body = client.post("/signals/notifications/run").json()
    assert body["sent"] is False
    assert "connection refused" in body["error"]
    status = client.get("/signals/notifications/status").json()
    assert "connection refused" in status["last_error"]

    # Transport recovers — the SAME batch is delivered on the next pass.
    delivered: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notifications,
        "_post_webhook",
        lambda url, payload: delivered.append(payload),
    )
    body = client.post("/signals/notifications/run").json()
    assert body["sent"] is True
    assert [s["title"] for s in delivered[0]["new_signals"]] == ["Market 0xc9"]
    assert client.get("/signals/notifications/status").json()["last_error"] == ""


def test_unconfigured_webhook_drops_batch_without_flooding_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    pm_dir = tmp_path / "pmdata"
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    monkeypatch.delenv("SIGNAL_WEBHOOK_URL", raising=False)
    client = TestClient(app)
    client.post("/signals/notifications/run")  # initialize

    _write_jsonl(pm_dir / "signal_ledger.jsonl", [_ledger_row("0xc1", "2026-06-10T11:00:00+00:00")])
    body = client.post("/signals/notifications/run").json()
    assert body["webhook_configured"] is False
    assert body["sent"] is False
    assert body["new_signals"] == 1  # observed, then dropped

    # Webhook configured later: the dropped batch must NOT replay.
    monkeypatch.setenv("SIGNAL_WEBHOOK_URL", "https://hooks.test/notify")
    body = client.post("/signals/notifications/run").json()
    assert body["sent"] is False
    assert body["new_signals"] == 0
    assert captured_posts == []


def test_status_endpoint_reports_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = tmp_path / "pmdata"
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    monkeypatch.delenv("SIGNAL_WEBHOOK_URL", raising=False)
    client = TestClient(app)

    status = client.get("/signals/notifications/status").json()
    assert status["webhook_configured"] is False
    assert status["initialized"] is False

    client.post("/signals/notifications/run")
    status = client.get("/signals/notifications/status").json()
    assert status["initialized"] is True
