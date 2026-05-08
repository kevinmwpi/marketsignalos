from __future__ import annotations

import os
from pathlib import Path


def _repo_root() -> Path:
    # This file: apps/api/src/marketsignalos_api/_paths.py
    # parents[4] is the repo root.
    return Path(__file__).resolve().parents[4]


def _data_dir() -> Path:
    return _repo_root() / "services" / "ingestor" / "data"


def trade_store_path() -> Path:
    configured = os.getenv("INGESTOR_TRADE_STORE_PATH")
    return Path(configured) if configured else _data_dir() / "kalshi_trades.jsonl"


def fills_store_path() -> Path:
    configured = os.getenv("INGESTOR_FILLS_STORE_PATH")
    return Path(configured) if configured else _data_dir() / "kalshi_fills.jsonl"


def resolutions_store_path() -> Path:
    configured = os.getenv("INGESTOR_RESOLUTIONS_STORE_PATH")
    return Path(configured) if configured else _data_dir() / "kalshi_market_resolutions.jsonl"


def enrichment_store_path() -> Path:
    configured = os.getenv("INGESTOR_ACCOUNT_ENRICHMENT_STORE_PATH")
    return Path(configured) if configured else _data_dir() / "kalshi_account_enrichment.jsonl"
