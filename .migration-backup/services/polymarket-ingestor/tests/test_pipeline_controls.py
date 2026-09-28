from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from marketsignalos_polymarket import runner
from marketsignalos_polymarket.models import PolymarketWalletHydration
from marketsignalos_polymarket.polymarket_client import PolymarketClient, PolymarketClientConfig


@pytest.fixture
def pilot_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "watchlist.txt"))
    monkeypatch.setenv("POLYMARKET_WALLET_CONCURRENCY", "1")
    monkeypatch.setattr(runner, "_reference_refresh_is_stale", lambda *args, **kwargs: False)
    return tmp_path


@pytest.fixture
def empty_client() -> Iterator[PolymarketClient]:
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[]))) as http:
        yield PolymarketClient(
            PolymarketClientConfig(max_retries=1, retry_backoff_seconds=0), client=http,
        )


def test_collection_only_preserves_scores_and_avoids_skill_watchlist_merge(
    pilot_data: Path, empty_client: PolymarketClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    score_file = pilot_data / "polymarket_wallet_enrichment.jsonl"
    original = '{"proxy_wallet":"0xold","computed_at":"2026-01-01T00:00:00Z"}\n'
    score_file.write_text(original, encoding="utf-8")
    enrich = Mock(side_effect=AssertionError("collection must not score"))
    merge = Mock(side_effect=AssertionError("collection must not republish qualified wallets"))
    monkeypatch.setattr(runner, "run_enrichment", enrich)
    monkeypatch.setattr(runner, "_merge_skill_qualified_wallets_into_watchlist", merge)

    result = runner.run_pipeline(
        client=empty_client, windows=["all"], skip_enrichment=True, refresh_reference=False,
    )

    assert result.enrichment_performed is False
    assert result.to_dict()["enrichment_performed"] is False
    assert result.enrichment_wallets == 0
    assert score_file.read_text(encoding="utf-8") == original
    enrich.assert_not_called()
    merge.assert_not_called()


def test_default_pipeline_still_scores_and_merges(
    pilot_data: Path, empty_client: PolymarketClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrich = Mock(return_value=7)
    merge = Mock(return_value=0)
    monkeypatch.setattr(runner, "run_enrichment", enrich)
    monkeypatch.setattr(runner, "_merge_skill_qualified_wallets_into_watchlist", merge)
    result = runner.run_pipeline(client=empty_client, windows=["all"], refresh_reference=False)
    assert result.enrichment_performed is True
    assert result.to_dict()["enrichment_wallets"] == 7
    enrich.assert_called_once()
    merge.assert_called_once()


def test_bounded_pipeline_rotates_oldest_polled_wallets_and_reports_errors(
    pilot_data: Path, empty_client: PolymarketClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (pilot_data / "watchlist.txt").write_text("0xa\n0xb\n0xc\n", encoding="utf-8")
    stores = runner._build_stores(pilot_data)
    stores.hydration.upsert_hydration([
        PolymarketWalletHydration(proxy_wallet="0xa", last_refreshed_at="2026-08-02T00:00:00Z"),
        PolymarketWalletHydration(proxy_wallet="0xb", last_refreshed_at="2026-08-01T00:00:00Z"),
    ])
    calls: list[list[str]] = []

    def collect(client: PolymarketClient, stores: runner._Stores, **kwargs: Any) -> tuple[int, int, int]:
        calls.append(kwargs["addresses"])
        assert kwargs["max_activity_requests_per_wallet"] == 3
        assert kwargs["max_pages_per_wallet"] == 2
        stores.hydration.upsert_hydration([
            PolymarketWalletHydration(
                proxy_wallet=wallet, last_refreshed_at="2026-09-09T00:00:00Z",
                errors=["activity request budget reached"],
            ) for wallet in kwargs["addresses"]
        ])
        return 2, 0, 1

    monkeypatch.setattr(runner, "run_wallets", collect)
    for _ in range(3):
        result = runner.run_pipeline(
            client=empty_client, windows=["all"], skip_enrichment=True,
            refresh_reference=False, wallet_batch_size=1, max_pages_per_wallet=2,
            max_activity_requests_per_wallet=3,
        )
        assert result.wallets_polled == 1
        assert result.wallets_with_errors == 1
        assert result.activity_budget_exhausted_wallets == 1
    assert calls == [["0xc"], ["0xb"], ["0xa"]]


@pytest.mark.parametrize("kwargs", [
    {"wallet_batch_size": 0}, {"wallet_batch_size": -1},
    {"max_activity_requests_per_wallet": 0}, {"max_pages_per_wallet": 0},
])
def test_invalid_bounds_fail_before_opening_stores(
    kwargs: dict[str, int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = Mock(side_effect=AssertionError("must validate before accessing data"))
    monkeypatch.setattr(runner, "_build_stores", build)
    with pytest.raises(ValueError, match="must be positive"):
        runner.run_pipeline(**kwargs)  # type: ignore[arg-type]
    build.assert_not_called()


def _activity(timestamp: int, transaction: str) -> dict[str, Any]:
    return {
        "proxyWallet": "0xabc", "timestamp": timestamp, "transactionHash": transaction,
        "conditionId": "0xc", "type": "TRADE", "side": "BUY", "price": 0.5,
        "size": 1, "usdcSize": 0.5, "outcomeIndex": 0,
    }


def test_request_cap_covers_boundary_calls_and_prevents_subgraph_escape(
    pilot_data: Path,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "goldsky" not in str(request.url)
        if request.url.path == "/activity":
            calls.append(request)
            return httpx.Response(200, json=[_activity(100, "a"), _activity(100, "b")])
        return httpx.Response(200, json=[])

    stores = runner._build_stores(pilot_data)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = PolymarketClient(PolymarketClientConfig(max_retries=1), client=http)
        runner.run_wallets(
            client, stores, addresses=["0xabc"], activity_page_size=2,
            max_pages_per_wallet=20, exhaust_activity=True, max_activity_requests_per_wallet=2,
        )
    assert len(calls) == 2
    state = stores.hydration.load_hydration()["0xabc"]
    assert not state.activity_history_complete
    assert state.oldest_activity_cursor_timestamp == 100  # boundary must be revisited
    assert "activity request budget reached" in state.errors


def test_capped_recent_refresh_retains_checkpoint_and_blocks_trust(pilot_data: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/activity":
            calls.append(request)
            return httpx.Response(200, json=[_activity(100, "a"), _activity(90, "b")])
        return httpx.Response(200, json=[])

    stores = runner._build_stores(pilot_data)
    stores.checkpoints.set_last_timestamp("0xabc", 50)
    stores.hydration.upsert_hydration([
        PolymarketWalletHydration(proxy_wallet="0xabc", activity_history_complete=True),
    ])
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = PolymarketClient(PolymarketClientConfig(max_retries=1), client=http)
        runner.run_wallets(
            client, stores, addresses=["0xabc"], activity_page_size=2,
            max_activity_requests_per_wallet=1,
        )
    assert len(calls) == 1
    assert stores.checkpoints.get_last_timestamp("0xabc") == 50
    state = stores.hydration.load_hydration()["0xabc"]
    assert not state.activity_history_complete
    assert "recent activity refresh incomplete; checkpoint retained" in state.errors


def test_request_budget_is_shared_between_recent_and_history(pilot_data: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/activity":
            calls.append(request)
            rows = [_activity(101, "new")] if len(calls) == 1 else [
                _activity(50, "a"), _activity(49, "b"),
            ]
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json=[])

    stores = runner._build_stores(pilot_data)
    stores.checkpoints.set_last_timestamp("0xabc", 100)
    stores.hydration.upsert_hydration([
        PolymarketWalletHydration(proxy_wallet="0xabc", oldest_activity_cursor_timestamp=50),
    ])
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = PolymarketClient(PolymarketClientConfig(max_retries=1), client=http)
        runner.run_wallets(
            client, stores, addresses=["0xabc"], activity_page_size=2,
            max_activity_requests_per_wallet=2,
        )
    assert len(calls) == 2
    assert stores.checkpoints.get_last_timestamp("0xabc") == 101
    state = stores.hydration.load_hydration()["0xabc"]
    assert state.oldest_activity_cursor_timestamp == 49
    assert not state.activity_history_complete


def test_pipeline_cli_parses_collection_controls() -> None:
    args = runner._build_parser().parse_args([
        "pipeline", "--skip-enrichment", "--wallet-batch-size", "5",
        "--max-pages-per-wallet", "2", "--max-activity-requests-per-wallet", "3",
    ])
    assert args.skip_enrichment
    assert args.wallet_batch_size == 5
    assert args.max_pages_per_wallet == 2
    assert args.max_activity_requests_per_wallet == 3
