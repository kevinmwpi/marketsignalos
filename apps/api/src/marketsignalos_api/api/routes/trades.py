from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel


router = APIRouter(prefix="/signals", tags=["signals"])


class TradeSignal(BaseModel):
    source: str
    market_ticker: str
    trade_id: str
    side: str
    price: float
    quantity: int
    traded_at: str


def _default_trade_store_path() -> Path:
    repo_root = Path(__file__).resolve().parents[6]
    return repo_root / "services" / "ingestor" / "data" / "kalshi_trades.jsonl"


def _trade_store_path() -> Path:
    configured = os.getenv("INGESTOR_TRADE_STORE_PATH")
    if configured:
        return Path(configured)
    return _default_trade_store_path()


def _load_trade_signals(limit: int) -> list[TradeSignal]:
    path = _trade_store_path()
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    selected = lines[-limit:]

    signals: list[TradeSignal] = []
    for line in reversed(selected):
        payload = json.loads(line)
        signals.append(TradeSignal.model_validate(payload))
    return signals


@router.get("/trades", response_model=list[TradeSignal])
def list_trade_signals(limit: int = Query(default=50, ge=1, le=500)) -> list[TradeSignal]:
    return _load_trade_signals(limit=limit)
