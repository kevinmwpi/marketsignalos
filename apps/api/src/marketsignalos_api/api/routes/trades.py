from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

from marketsignalos_api._paths import trade_store_path

router = APIRouter(prefix="/signals", tags=["signals"])


class TradeSignal(BaseModel):
    source: str
    market_ticker: str
    trade_id: str
    side: str
    price: float
    quantity: int
    traded_at: str


def _load_from_jsonl(path: Path, limit: int) -> list[TradeSignal]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    selected = lines[-limit:]
    signals: list[TradeSignal] = []
    for line in reversed(selected):
        signals.append(TradeSignal.model_validate(json.loads(line)))
    return signals


def _load_from_db(limit: int) -> list[TradeSignal]:
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
            (limit,),
        )
        rows: list[dict[str, object]] = [dict(r) for r in cur.fetchall()]  # type: ignore[union-attr]

    return [TradeSignal.model_validate(row) for row in rows]


def _load_trade_signals(limit: int) -> list[TradeSignal]:
    from marketsignalos_api.db import get_database_url

    if get_database_url():
        return _load_from_db(limit)
    return _load_from_jsonl(trade_store_path(), limit)


@router.get("/trades", response_model=list[TradeSignal])
def list_trade_signals(limit: int = Query(default=50, ge=1, le=500)) -> list[TradeSignal]:
    return _load_trade_signals(limit=limit)
