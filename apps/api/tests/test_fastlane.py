from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.main import app
from marketsignalos_api.services import fastlane


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )


def _enrichment_row(
    wallet: str,
    *,
    rank_score: float,
    tailable: bool = True,
    archetype: str = "systematic",
) -> dict[str, object]:
    return {
        "proxy_wallet": wallet,
        "name": f"name-{wallet}",
        "pseudonym": f"pseudo-{wallet}",
        "rank_score": rank_score,
        "skill_likelihood": 0.95,
        "style_archetype": archetype,
        "data_quality_status": "trusted",
        "tailability_status": "tailable" if tailable else "blocked",
    }


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "pmdata"
    d.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        d / "polymarket_wallet_enrichment.jsonl",
        [
            _enrichment_row("0xalpha", rank_score=5.0),
            _enrichment_row("0xbeta", rank_score=9.0),
            _enrichment_row("0xblocked", rank_score=99.0, tailable=False),
        ],
    )
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(d))
    return d


def _buy(wallet: str, ts: int, *, usdc: float = 500.0, cond: str = "0xcond") -> dict[str, Any]:
    return {
        "proxyWallet": wallet,
        "timestamp": ts,
        "conditionId": cond,
        "type": "TRADE",
        "side": "BUY",
        "size": 1000.0,
        "usdcSize": usdc,
        "price": 0.5,
        "outcomeIndex": 0,
        "outcome": "Yes",
        "slug": "some-market",
        "title": "Some market",
        "eventSlug": "some-event",
        "transactionHash": f"0xtx{ts}",
    }


def _sell(wallet: str, ts: int, *, usdc: float = 500.0) -> dict[str, Any]:
    row = _buy(wallet, ts, usdc=usdc)
    row["side"] = "SELL"
    return row


class _FakeFeed:
    """In-memory activity feed: wallet -> events; records fetch calls."""

    def __init__(self) -> None:
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, int | None]] = []

    def fetch(self, address: str, since_ts: int | None) -> list[dict[str, Any]]:
        self.calls.append((address, since_ts))
        rows = self.events.get(address, [])
        if since_ts is None:
            return rows
        return [r for r in rows if int(r["timestamp"]) >= since_ts]


@pytest.fixture()
def captured_posts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_post(url: str, payload: dict[str, Any]) -> None:
        calls.append({"url": url, "payload": payload})

    monkeypatch.setattr(fastlane, "_post_webhook", _fake_post)
    return calls


def test_first_tick_initializes_watermarks_without_alerting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    d = _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("SIGNAL_WEBHOOK_URL", "https://hook.example/x")
    feed = _FakeFeed()
    feed.events["0xalpha"] = [_buy("0xalpha", 1000)]
    feed.events["0xbeta"] = [_buy("0xbeta", 2000)]

    result = fastlane.run_fastlane_tick(fetch=feed.fetch)
    assert result["wallets_watched"] == 2
    assert result["wallets_initialized"] == 2
    assert result["new_buys"] == 0
    assert result["sent"] is False
    assert captured_posts == []

    state = json.loads((d / "fastlane_state.json").read_text(encoding="utf-8"))
    assert state["wallets"] == {"0xalpha": 1000, "0xbeta": 2000}


def test_new_buy_alerts_once_and_advances_watermark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("SIGNAL_WEBHOOK_URL", "https://hook.example/x")
    feed = _FakeFeed()
    feed.events["0xbeta"] = [_buy("0xbeta", 2000)]
    fastlane.run_fastlane_tick(fetch=feed.fetch)  # initialize

    feed.events["0xbeta"].append(_buy("0xbeta", 3000, usdc=1234.0))
    result = fastlane.run_fastlane_tick(fetch=feed.fetch)
    assert result["new_buys"] == 1
    assert result["sent"] is True

    payload = captured_posts[-1]["payload"]
    assert payload["stream"] == "fastlane"
    alert = payload["new_buys"][0]
    assert alert["proxy_wallet"] == "0xbeta"
    assert alert["wallet_name"] == "name-0xbeta"
    assert alert["side"] == "BUY"
    assert alert["usdc_size"] == 1234.0
    assert alert["skill_likelihood"] == 0.95
    assert alert["wallet_archetype"] == "systematic"
    assert alert["polymarket_market_url"] == "https://polymarket.com/event/some-event"

    # Same feed again: watermark advanced, nothing new to deliver.
    result = fastlane.run_fastlane_tick(fetch=feed.fetch)
    assert result["new_buys"] == 0
    assert len(captured_posts) == 1


def test_failed_delivery_retries_same_batch_next_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("SIGNAL_WEBHOOK_URL", "https://hook.example/x")
    feed = _FakeFeed()
    feed.events["0xbeta"] = [_buy("0xbeta", 2000)]
    fastlane.run_fastlane_tick(fetch=feed.fetch)

    feed.events["0xbeta"].append(_buy("0xbeta", 3000))

    def _broken_post(url: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("webhook down")

    result = fastlane.run_fastlane_tick(fetch=feed.fetch, post=_broken_post)
    assert result["sent"] is False
    assert "webhook down" in result["error"]

    # Watermark stayed put, so a healthy tick redelivers the same BUY.
    result = fastlane.run_fastlane_tick(fetch=feed.fetch)
    assert result["new_buys"] == 1
    assert result["sent"] is True
    assert len(captured_posts) == 1


def test_min_entry_usdc_filters_dust_but_still_advances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("SIGNAL_WEBHOOK_URL", "https://hook.example/x")
    monkeypatch.setenv("FASTLANE_MIN_ENTRY_USDC", "100")
    feed = _FakeFeed()
    feed.events["0xbeta"] = [_buy("0xbeta", 2000)]
    fastlane.run_fastlane_tick(fetch=feed.fetch)

    feed.events["0xbeta"].append(_buy("0xbeta", 3000, usdc=5.0))
    result = fastlane.run_fastlane_tick(fetch=feed.fetch)
    assert result["new_buys"] == 0
    assert captured_posts == []

    # The dust trade is behind the watermark now — never re-alerted.
    feed.events["0xbeta"].append(_buy("0xbeta", 4000, usdc=500.0))
    result = fastlane.run_fastlane_tick(fetch=feed.fetch)
    assert result["new_buys"] == 1
    assert len(captured_posts[-1]["payload"]["new_buys"]) == 1


def test_sells_surface_in_their_own_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("SIGNAL_WEBHOOK_URL", "https://hook.example/x")
    feed = _FakeFeed()
    feed.events["0xbeta"] = [_buy("0xbeta", 2000)]
    fastlane.run_fastlane_tick(fetch=feed.fetch)

    feed.events["0xbeta"].append(_sell("0xbeta", 3000))
    result = fastlane.run_fastlane_tick(fetch=feed.fetch)
    assert result["new_sells"] == 1
    assert result["new_buys"] == 0
    payload = captured_posts[-1]["payload"]
    assert payload["new_sells"][0]["side"] == "SELL"


def test_watchlist_takes_top_ranked_tailable_wallets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("FASTLANE_WALLETS", "1")
    feed = _FakeFeed()
    result = fastlane.run_fastlane_tick(fetch=feed.fetch)
    assert result["wallets_watched"] == 1
    # beta outranks alpha on rank_score; blocked is excluded outright.
    assert [addr for addr, _ in feed.calls] == ["0xbeta"]


def test_no_webhook_still_tracks_watermarks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    d = _seed(tmp_path, monkeypatch)
    monkeypatch.delenv("SIGNAL_WEBHOOK_URL", raising=False)
    feed = _FakeFeed()
    feed.events["0xbeta"] = [_buy("0xbeta", 2000)]
    fastlane.run_fastlane_tick(fetch=feed.fetch)

    feed.events["0xbeta"].append(_buy("0xbeta", 3000))
    result = fastlane.run_fastlane_tick(fetch=feed.fetch)
    # The BUY is counted but intentionally dropped — a webhook configured
    # later must not be flooded with stale alerts.
    assert result["new_buys"] == 1
    assert result["sent"] is False
    assert captured_posts == []
    state = json.loads((d / "fastlane_state.json").read_text(encoding="utf-8"))
    assert state["wallets"]["0xbeta"] == 3000


def test_fetch_error_skips_wallet_without_killing_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    _seed(tmp_path, monkeypatch)

    def _flaky_fetch(address: str, since_ts: int | None) -> list[dict[str, Any]]:
        if address == "0xbeta":
            raise RuntimeError("upstream 503")
        return [_buy(address, 1000)]

    result = fastlane.run_fastlane_tick(fetch=_flaky_fetch)
    assert result["fetch_errors"] == 1
    assert result["wallets_initialized"] == 1  # alpha still initialized


def test_config_off_unless_interval_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FASTLANE_EVERY_SECONDS", raising=False)
    assert fastlane.FastlaneConfig.from_env() is None
    monkeypatch.setenv("FASTLANE_EVERY_SECONDS", "0")
    assert fastlane.FastlaneConfig.from_env() is None
    monkeypatch.setenv("FASTLANE_EVERY_SECONDS", "not-a-number")
    assert fastlane.FastlaneConfig.from_env() is None


def test_config_clamps_interval_and_wallets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASTLANE_EVERY_SECONDS", "5")
    monkeypatch.setenv("FASTLANE_WALLETS", "5000")
    config = fastlane.FastlaneConfig.from_env()
    assert config is not None
    assert config.interval_seconds == 30  # politeness floor
    assert config.max_wallets == 100


def test_status_and_run_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_posts: list[dict[str, Any]],
) -> None:
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fastlane, "_default_fetch", lambda address, since_ts: []
    )
    client = TestClient(app)

    body = client.post("/signals/fastlane/run").json()
    assert body["wallets_watched"] == 2
    assert body["new_buys"] == 0

    status = client.get("/signals/fastlane/status").json()
    assert status["poller"] == {"enabled": False}
    assert status["wallets_tracked"] == 2
    assert status["last_tick_at"]
    assert status["last_error"] == ""
