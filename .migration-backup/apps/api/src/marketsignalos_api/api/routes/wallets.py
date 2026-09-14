from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from marketsignalos_api.services.wallet_detail import compute_wallet_detail

router = APIRouter(prefix="/signals/wallets", tags=["signals"])


@router.get("/{proxy_wallet}")
def wallet_detail(
    proxy_wallet: str,
    bet_limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    """
    Full dossier for one wallet: enrichment scores (lifetime, recent, CLV),
    still-held positions, per-position bet history, equity curve, category
    breakdown, and the wallet's paper-trade ledger and exit-signal rows.
    """
    detail = compute_wallet_detail(proxy_wallet, bet_limit=bet_limit)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"No enrichment data for wallet {proxy_wallet.lower()}",
        )
    return detail
