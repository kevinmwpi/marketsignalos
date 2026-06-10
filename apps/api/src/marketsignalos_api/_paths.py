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


def profile_snapshots_path() -> Path:
    configured = os.getenv("KALSHI_PROFILE_SNAPSHOTS_PATH")
    return Path(configured) if configured else _data_dir() / "kalshi_profile_snapshots.jsonl"


def polymarket_enrichment_path() -> Path:
    configured = os.getenv("POLYMARKET_ENRICHMENT_PATH")
    if configured:
        return Path(configured)
    base = os.getenv("POLYMARKET_DATA_DIR")
    if base:
        return Path(base) / "polymarket_wallet_enrichment.jsonl"
    return _data_dir() / "polymarket_wallet_enrichment.jsonl"


def _polymarket_dir() -> Path:
    """Same as _data_dir, but honors POLYMARKET_DATA_DIR override."""
    base = os.getenv("POLYMARKET_DATA_DIR")
    return Path(base) if base else _data_dir()


def polymarket_positions_path() -> Path:
    return _polymarket_dir() / "polymarket_positions.jsonl"


def polymarket_position_snapshots_path() -> Path:
    return _polymarket_dir() / "polymarket_position_snapshots.jsonl"


def polymarket_hydration_path() -> Path:
    return _polymarket_dir() / "polymarket_wallet_hydration.jsonl"


def polymarket_activity_path() -> Path:
    return _polymarket_dir() / "polymarket_activity.jsonl"


def polymarket_markets_path() -> Path:
    return _polymarket_dir() / "polymarket_markets.jsonl"


def kalshi_markets_path() -> Path:
    return _polymarket_dir() / "kalshi_markets.jsonl"


def market_links_path() -> Path:
    return _polymarket_dir() / "market_links.jsonl"


def signal_ledger_path() -> Path:
    return _polymarket_dir() / "signal_ledger.jsonl"


def exit_signals_path() -> Path:
    return _polymarket_dir() / "exit_signals.jsonl"


def exit_state_path() -> Path:
    return _polymarket_dir() / "exit_state.json"


def notifications_state_path() -> Path:
    return _polymarket_dir() / "notifications_state.json"
