from __future__ import annotations

from marketsignalos_ingestor.pipeline import (
    KalshiFillIngestionPipeline,
    KalshiResolutionIngestionPipeline,
    KalshiTradeIngestionPipeline,
)


class StubKalshiClient:
    def __init__(self) -> None:
        self.last_call: dict[str, object] | None = None

    def list_trades(self, ticker: str, *, limit: int = 500, cursor: str | None = None) -> dict[str, object]:
        self.last_call = {"ticker": ticker, "limit": limit, "cursor": cursor}
        return {
            "cursor": "next-page",
            "trades": [
                {
                    "trade_id": "42",
                    "side": "yes",
                    "yes_price": 62,
                    "no_price": 38,
                    "count": 7,
                    "created_time": "2026-01-01T00:00:00Z",
                }
            ],
        }


def test_pull_trade_batch_normalizes_payload() -> None:
    stub_client = StubKalshiClient()
    pipeline = KalshiTradeIngestionPipeline(stub_client)  # type: ignore[arg-type]

    batch = pipeline.pull_trade_batch("KXTEST-1", cursor="abc", limit=100)

    assert stub_client.last_call == {"ticker": "KXTEST-1", "limit": 100, "cursor": "abc"}
    assert batch.ticker == "KXTEST-1"
    assert batch.next_cursor == "next-page"
    assert len(batch.trades) == 1

    trade = batch.trades[0]
    assert trade.source == "kalshi"
    assert trade.market_ticker == "KXTEST-1"
    assert trade.trade_id == "42"
    assert trade.side == "yes"
    assert trade.price == 62.0
    assert trade.quantity == 7
    assert trade.traded_at == "2026-01-01T00:00:00Z"


class StubKalshiFillClient:
    def list_fills(self, ticker: str, *, limit: int = 500, cursor: str | None = None) -> dict[str, object]:
        assert ticker == "KXTEST-2"
        assert limit == 50
        assert cursor == "cursor-1"
        return {
            "cursor": "cursor-2",
            "fills": [
                {
                    "member_id": "acct-1",
                    "fill_id": "fill-1",
                    "trade_id": "trade-1",
                    "side": "no",
                    "yes_price": 43,
                    "no_price": 57,
                    "count": 3,
                    "created_time": "2026-01-03T00:00:00Z",
                }
            ],
        }


def test_pull_fill_batch_normalizes_payload() -> None:
    pipeline = KalshiFillIngestionPipeline(StubKalshiFillClient())  # type: ignore[arg-type]

    batch = pipeline.pull_fill_batch("KXTEST-2", cursor="cursor-1", limit=50)

    assert batch.next_cursor == "cursor-2"
    assert len(batch.fills) == 1
    fill = batch.fills[0]
    assert fill.account_id == "acct-1"
    assert fill.fill_id == "fill-1"
    assert fill.trade_id == "trade-1"
    assert fill.side == "no"
    assert fill.price == 57.0
    assert fill.quantity == 3


class StubKalshiMarketsClient:
    def list_markets(
        self,
        *,
        status: str = "settled",
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, object]:
        assert status == "settled"
        assert limit == 2
        assert cursor == "m-cursor-1"
        return {
            "cursor": "m-cursor-2",
            "markets": [
                {"ticker": "KX-1", "result": "yes", "close_time": "2026-01-04T00:00:00Z"},
                {"ticker": "KX-2", "result": "no", "close_time": "2026-01-05T00:00:00Z"},
            ],
        }


def test_pull_resolution_batch_normalizes_payload() -> None:
    pipeline = KalshiResolutionIngestionPipeline(StubKalshiMarketsClient())  # type: ignore[arg-type]
    batch = pipeline.pull_resolution_batch(cursor="m-cursor-1", limit=2)

    assert batch.next_cursor == "m-cursor-2"
    assert [row.market_ticker for row in batch.resolutions] == ["KX-1", "KX-2"]
    assert [row.settlement_side for row in batch.resolutions] == ["yes", "no"]
