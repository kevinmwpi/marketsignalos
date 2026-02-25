from __future__ import annotations

from marketsignalos_ingestor.pipeline import KalshiTradeIngestionPipeline


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
