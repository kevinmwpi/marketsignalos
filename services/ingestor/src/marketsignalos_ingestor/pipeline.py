from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import MarketResolution, NormalizedFill, NormalizedTrade


@dataclass(frozen=True, slots=True)
class IngestionBatch:
    ticker: str
    trades: list[NormalizedTrade]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class FillIngestionBatch:
    ticker: str
    fills: list[NormalizedFill]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ResolutionIngestionBatch:
    resolutions: list[MarketResolution]
    next_cursor: str | None


class TradesClient(Protocol):
    def list_trades(
        self,
        ticker: str | None = None,
        *,
        limit: int = 500,
        cursor: str | None = None,
    ) -> dict[str, object]: ...


class FillsClient(Protocol):
    def list_fills(
        self,
        ticker: str,
        *,
        limit: int = 500,
        cursor: str | None = None,
    ) -> dict[str, object]: ...


class MarketsClient(Protocol):
    def list_markets(
        self,
        *,
        status: str = "settled",
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, object]: ...


def _payload_list(payload: dict[str, object], key: str) -> list[object]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"Kalshi {key} payload must be a list")
    return value


class KalshiTradeIngestionPipeline:
    """Pulls trade pages from Kalshi and normalizes them for downstream storage."""

    def __init__(self, client: TradesClient) -> None:
        self._client = client

    def pull_trade_batch(
        self,
        ticker: str | None = None,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> IngestionBatch:
        raw_payload = self._client.list_trades(ticker=ticker, limit=limit, cursor=cursor)
        raw_trades = _payload_list(raw_payload, "trades")
        normalized = [self._normalize_trade(ticker, trade) for trade in raw_trades]
        next_cursor = raw_payload.get("cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("Kalshi cursor must be a string when present")

        return IngestionBatch(
            ticker=ticker or "",
            trades=normalized,
            next_cursor=next_cursor,
        )

    @staticmethod
    def _normalize_trade(ticker: str | None, payload: object) -> NormalizedTrade:
        if not isinstance(payload, dict):
            raise ValueError("Kalshi trade payload must be an object")

        trade_id = str(payload["trade_id"])

        # Public /markets/trades uses taker_side; /portfolio/trades uses side.
        side = str(payload.get("taker_side") or payload.get("side", "yes")).lower()

        # Public endpoint: yes_price_dollars (dollar string like "0.86").
        # Portfolio endpoint: yes_price / no_price (cents int like 86).
        if "yes_price_dollars" in payload:
            price = float(payload["yes_price_dollars"] if side == "yes" else payload["no_price_dollars"])
        else:
            raw_cents = int(payload["yes_price"] if side == "yes" else payload["no_price"])
            price = raw_cents / 100.0

        # Public endpoint: count_fp (fractional float string). Portfolio: count (int).
        if "count_fp" in payload:
            quantity = max(1, round(float(payload["count_fp"])))
        else:
            quantity = int(payload["count"])

        # Public endpoint includes ticker per trade; use that if no ticker was requested.
        actual_ticker = str(payload.get("ticker") or ticker or "")
        traded_at = str(payload["created_time"])

        return NormalizedTrade(
            source="kalshi",
            market_ticker=actual_ticker,
            trade_id=trade_id,
            side=side,
            price=price,
            quantity=quantity,
            traded_at=traded_at,
        )


class KalshiFillIngestionPipeline:
    """Pulls account-level fills and normalizes them for downstream storage."""

    def __init__(self, client: FillsClient) -> None:
        self._client = client

    def pull_fill_batch(
        self,
        ticker: str,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> FillIngestionBatch:
        raw_payload = self._client.list_fills(ticker=ticker, limit=limit, cursor=cursor)
        raw_fills = _payload_list(raw_payload, "fills")
        normalized = [self._normalize_fill(ticker, payload) for payload in raw_fills]
        next_cursor = raw_payload.get("cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("Kalshi cursor must be a string when present")
        return FillIngestionBatch(ticker=ticker, fills=normalized, next_cursor=next_cursor)

    @staticmethod
    def _normalize_fill(ticker: str, payload: object) -> NormalizedFill:
        if not isinstance(payload, dict):
            raise ValueError("Kalshi fill payload must be an object")

        side = str(payload["side"]).lower()
        account_id = str(payload.get("user_id") or payload.get("account_id") or payload["member_id"])
        fill_id = str(payload.get("fill_id") or payload.get("id") or payload["trade_id"])
        trade_id = str(payload["trade_id"])
        price = float(payload["yes_price"] if side == "yes" else payload["no_price"])
        quantity = int(payload.get("count") or payload.get("quantity") or payload.get("size") or 0)
        traded_at = str(payload.get("created_time") or payload.get("filled_at"))

        return NormalizedFill(
            source="kalshi",
            account_id=account_id,
            market_ticker=ticker,
            fill_id=fill_id,
            trade_id=trade_id,
            side=side,
            price=price,
            quantity=quantity,
            traded_at=traded_at,
        )


class KalshiResolutionIngestionPipeline:
    """Pulls settled markets and normalizes final outcomes."""

    def __init__(self, client: MarketsClient) -> None:
        self._client = client

    def pull_resolution_batch(
        self,
        *,
        cursor: str | None = None,
        limit: int = 200,
    ) -> ResolutionIngestionBatch:
        raw_payload = self._client.list_markets(status="settled", limit=limit, cursor=cursor)
        raw_markets = _payload_list(raw_payload, "markets")
        normalized: list[MarketResolution] = []
        for payload in raw_markets:
            resolution = self._normalize_resolution(payload)
            if resolution:
                normalized.append(resolution)

        next_cursor = raw_payload.get("cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("Kalshi cursor must be a string when present")
        return ResolutionIngestionBatch(resolutions=normalized, next_cursor=next_cursor)

    @staticmethod
    def _normalize_resolution(payload: object) -> MarketResolution | None:
        if not isinstance(payload, dict):
            raise ValueError("Kalshi market payload must be an object")

        ticker = payload.get("ticker")
        result = payload.get("result")
        resolved_at = payload.get("close_time") or payload.get("settled_time") or payload.get("resolved_time")
        if ticker is None or result is None or resolved_at is None:
            return None

        settlement_side = str(result).lower()
        if settlement_side not in {"yes", "no"}:
            return None

        return MarketResolution(
            source="kalshi",
            market_ticker=str(ticker),
            resolved_at=str(resolved_at),
            settlement_side=settlement_side,
        )
