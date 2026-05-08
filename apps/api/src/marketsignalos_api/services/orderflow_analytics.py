from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel

from marketsignalos_api._paths import trade_store_path


class OrderflowAnomaly(BaseModel):
    source: str
    market_ticker: str
    trade_id: str
    traded_at: str
    side: str
    price: float
    quantity: int
    anomaly_type: str
    anomaly_score: float
    details: str


class _TradeRow(BaseModel):
    source: str
    market_ticker: str
    trade_id: str
    side: str
    price: float
    quantity: int
    traded_at: str


def _load_trades_from_jsonl(path: Path, max_rows: int) -> list[_TradeRow]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    selected = lines[-max_rows:]
    rows = [_TradeRow.model_validate(json.loads(line)) for line in selected]
    rows.sort(key=lambda row: (row.traded_at, row.trade_id))
    return rows


def _load_trades_from_db(max_rows: int) -> list[_TradeRow]:
    from marketsignalos_api.db import db_cursor

    with db_cursor() as cur:
        cur.execute(  # type: ignore[union-attr]
            """
            SELECT source, market_ticker, trade_id, side,
                   price::float AS price, quantity,
                   traded_at::text AS traded_at
            FROM trades
            ORDER BY traded_at DESC
            LIMIT %s
            """,
            (max_rows,),
        )
        rows: list[dict[str, object]] = [dict(r) for r in cur.fetchall()]  # type: ignore[union-attr]

    result = [_TradeRow.model_validate(row) for row in rows]
    result.sort(key=lambda row: (row.traded_at, row.trade_id))
    return result


def _load_trades(max_rows: int) -> list[_TradeRow]:
    from marketsignalos_api.db import get_database_url

    if get_database_url():
        return _load_trades_from_db(max_rows)
    return _load_trades_from_jsonl(trade_store_path(), max_rows)


def _stddev(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def detect_orderflow_anomalies(
    *,
    max_rows: int,
    limit: int,
    min_odds_jump: float,
    min_size_zscore: float,
    min_large_quantity: int,
) -> list[OrderflowAnomaly]:
    trades = _load_trades(max_rows=max_rows)
    if not trades:
        return []

    by_ticker: dict[str, list[_TradeRow]] = defaultdict(list)
    for trade in trades:
        by_ticker[trade.market_ticker].append(trade)

    anomalies: list[OrderflowAnomaly] = []

    for ticker_trades in by_ticker.values():
        prev_price: float | None = None
        quantities_so_far: list[int] = []

        for trade in ticker_trades:
            if prev_price is not None:
                jump = abs(trade.price - prev_price)
                if jump >= min_odds_jump:
                    anomalies.append(
                        OrderflowAnomaly(
                            source=trade.source,
                            market_ticker=trade.market_ticker,
                            trade_id=trade.trade_id,
                            traded_at=trade.traded_at,
                            side=trade.side,
                            price=trade.price,
                            quantity=trade.quantity,
                            anomaly_type="odds_jump",
                            anomaly_score=round(jump, 4),
                            details=f"Price jumped by {jump:.2f} points from previous trade",
                        )
                    )

            if len(quantities_so_far) >= 3:
                mean_qty = sum(quantities_so_far) / len(quantities_so_far)
                sd_qty = _stddev(quantities_so_far)
                if sd_qty > 0:
                    zscore = (trade.quantity - mean_qty) / sd_qty
                    if zscore >= min_size_zscore:
                        anomalies.append(
                            OrderflowAnomaly(
                                source=trade.source,
                                market_ticker=trade.market_ticker,
                                trade_id=trade.trade_id,
                                traded_at=trade.traded_at,
                                side=trade.side,
                                price=trade.price,
                                quantity=trade.quantity,
                                anomaly_type="size_outlier",
                                anomaly_score=round(zscore, 4),
                                details=(
                                    f"Quantity z-score {zscore:.2f} versus earlier trades"
                                    f" in {trade.market_ticker}"
                                ),
                            )
                        )

            if trade.quantity >= min_large_quantity:
                anomalies.append(
                    OrderflowAnomaly(
                        source=trade.source,
                        market_ticker=trade.market_ticker,
                        trade_id=trade.trade_id,
                        traded_at=trade.traded_at,
                        side=trade.side,
                        price=trade.price,
                        quantity=trade.quantity,
                        anomaly_type="large_bet",
                        anomaly_score=float(trade.quantity),
                        details=f"Quantity {trade.quantity} exceeded large-bet threshold",
                    )
                )

            prev_price = trade.price
            quantities_so_far.append(trade.quantity)

    anomalies.sort(key=lambda row: (row.traded_at, row.anomaly_score), reverse=True)
    return anomalies[:limit]
