from __future__ import annotations

from dataclasses import dataclass

from .kalshi_client import KalshiClient
from .models import NormalizedTrade


@dataclass(frozen=True, slots=True)
class IngestionBatch:
    ticker: str
    trades: list[NormalizedTrade]
    next_cursor: str | None


class KalshiTradeIngestionPipeline:
    """Pulls trade pages from Kalshi and normalizes them for downstream storage."""

    def __init__(self, client: KalshiClient) -> None:
        self._client = client

    def pull_trade_batch(
        self,
        ticker: str,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> IngestionBatch:
        raw_payload = self._client.list_trades(ticker=ticker, limit=limit, cursor=cursor)
        raw_trades = raw_payload.get("trades", [])
        normalized = [self._normalize_trade(ticker, trade) for trade in raw_trades]
        next_cursor = raw_payload.get("cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("Kalshi cursor must be a string when present")

        return IngestionBatch(
            ticker=ticker,
            trades=normalized,
            next_cursor=next_cursor,
        )

    @staticmethod
    def _normalize_trade(ticker: str, payload: object) -> NormalizedTrade:
        if not isinstance(payload, dict):
            raise ValueError("Kalshi trade payload must be an object")

        trade_id = str(payload["trade_id"])
        side = str(payload["side"]).lower()
        price = float(payload["yes_price"] if side == "yes" else payload["no_price"])
        quantity = int(payload["count"])
        traded_at = str(payload["created_time"])

        return NormalizedTrade(
            source="kalshi",
            market_ticker=ticker,
            trade_id=trade_id,
            side=side,
            price=price,
            quantity=quantity,
            traded_at=traded_at,
        )
