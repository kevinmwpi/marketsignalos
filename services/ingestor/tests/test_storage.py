from __future__ import annotations

import json
from pathlib import Path

from marketsignalos_ingestor.models import MarketResolution, NormalizedFill, NormalizedTrade
from marketsignalos_ingestor.storage import (
    JsonCheckpointStore,
    JsonlFillStore,
    JsonlResolutionStore,
    JsonlTradeStore,
)


def test_jsonl_trade_store_writes_records(tmp_path: Path) -> None:
    store = JsonlTradeStore(tmp_path / "trades.jsonl")
    written = store.write_trades(
        [
            NormalizedTrade(
                source="kalshi",
                market_ticker="KXTEST",
                trade_id="t1",
                side="yes",
                price=55,
                quantity=2,
                traded_at="2026-01-01T00:00:00Z",
            )
        ]
    )

    assert written == 1
    lines = (tmp_path / "trades.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["trade_id"] == "t1"


def test_json_checkpoint_store_round_trip(tmp_path: Path) -> None:
    store = JsonCheckpointStore(tmp_path / "checkpoints.json")

    assert store.get_cursor("KXTEST") is None
    store.set_cursor("KXTEST", "cursor-123")
    assert store.get_cursor("KXTEST") == "cursor-123"


def test_jsonl_fill_store_dedupes_records(tmp_path: Path) -> None:
    store = JsonlFillStore(tmp_path / "fills.jsonl")
    fills = [
        NormalizedFill(
            source="kalshi",
            account_id="acct-1",
            market_ticker="KX",
            fill_id="fill-1",
            trade_id="trade-1",
            side="yes",
            price=55,
            quantity=1,
            traded_at="2026-01-01T00:00:00Z",
        ),
        NormalizedFill(
            source="kalshi",
            account_id="acct-1",
            market_ticker="KX",
            fill_id="fill-1",
            trade_id="trade-1",
            side="yes",
            price=55,
            quantity=1,
            traded_at="2026-01-01T00:00:00Z",
        ),
    ]

    assert store.write_fills(fills) == 1
    assert store.write_fills(fills) == 0


def test_jsonl_resolution_store_dedupes_records(tmp_path: Path) -> None:
    store = JsonlResolutionStore(tmp_path / "resolutions.jsonl")
    rows = [
        MarketResolution(
            source="kalshi",
            market_ticker="KX",
            resolved_at="2026-01-01T00:00:00Z",
            settlement_side="yes",
        ),
        MarketResolution(
            source="kalshi",
            market_ticker="KX",
            resolved_at="2026-01-01T00:00:00Z",
            settlement_side="yes",
        ),
    ]

    assert store.write_resolutions(rows) == 1
    assert store.write_resolutions(rows) == 0
