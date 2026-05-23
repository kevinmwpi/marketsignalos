"""Tests for the deep-review pipeline: matrix sweep, review-state I/O, and
pruning. Keeps `test_runner.py` focused on the shallow pipeline."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketsignalos_polymarket.models import (
    PolymarketPosition,
    PolymarketWalletEnrichment,
    PolymarketWalletReviewState,
)
from marketsignalos_polymarket.polymarket_client import (
    PolymarketClient,
    PolymarketClientConfig,
)
from marketsignalos_polymarket.runner import (
    _apply_sweep_to_review_state,
    _build_stores,
    _load_review_state,
    _load_skilled_wallets,
    _load_wallets_with_open_positions,
    _pick_hydration_batch,
    _write_review_state,
    prune_review_state,
    run_deep_leaderboard,
    run_deep_pipeline,
    run_pin_wallet,
    run_prune_wallets,
)


def _client_with_handler(handler: Any) -> PolymarketClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, headers={"Accept": "application/json"})
    return PolymarketClient(
        config=PolymarketClientConfig(max_retries=1, retry_backoff_seconds=0.001),
        client=http,
    )


def _lb_row(wallet: str, amount: float = 1.0) -> dict[str, Any]:
    return {"proxyWallet": wallet, "amount": amount, "pseudonym": "p", "name": "n"}


# ── Review-state I/O ─────────────────────────────────────────────────────────


def test_review_state_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    path = tmp_path / "polymarket_wallet_review_state.jsonl"
    state = {
        "0xa": PolymarketWalletReviewState(
            proxy_wallet="0xa",
            status="active",
            first_seen_at="2026-05-20T00:00:00Z",
            last_leaderboard_seen_at="2026-05-20T00:00:00Z",
            last_activity_at=1_700_000_000,
            last_polled_at="2026-05-20T00:01:00Z",
            best_rank=3,
            appearances=5,
            categories=["POLITICS", "OVERALL"],
            time_periods=["WEEK", "ALL"],
            orders=["PNL"],
        ),
        "0xb": PolymarketWalletReviewState(
            proxy_wallet="0xb",
            status="archived",
            first_seen_at="2026-01-01T00:00:00Z",
            archived_at="2026-05-15T00:00:00Z",
            archived_reason="dormant_and_undiscovered",
        ),
    }
    _write_review_state(path, state)
    loaded = _load_review_state(path)
    assert set(loaded.keys()) == {"0xa", "0xb"}
    assert loaded["0xa"].best_rank == 3
    assert loaded["0xa"].categories == ["POLITICS", "OVERALL"]
    assert loaded["0xa"].last_activity_at == 1_700_000_000
    assert loaded["0xb"].status == "archived"
    assert loaded["0xb"].archived_reason == "dormant_and_undiscovered"


def test_load_review_state_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _load_review_state(tmp_path / "missing.jsonl") == {}


# ── Matrix sweep ─────────────────────────────────────────────────────────────


def test_run_deep_leaderboard_iterates_full_matrix_with_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every (category, time_period, order_by, offset) combo should be hit
    once. A wallet that appears in multiple slices is counted as appearances
    but only added once to `discovered`."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "data-api.polymarket.com":
            return httpx.Response(404)
        if request.url.path != "/v1/leaderboard":
            return httpx.Response(404)
        params = dict(request.url.params)
        seen_params.append(params)
        # Single wallet across all slices — exercises the per-wallet appearance accumulator.
        return httpx.Response(200, json=[_lb_row("0xabc", amount=1.0)])

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    # depth=50 = single offset per slice, so total slice calls = 10 * 4 * 2 = 80.
    sweep = run_deep_leaderboard(client, stores, depth=50)

    assert len(seen_params) == 80, (
        f"expected 80 matrix calls (10 cats * 4 periods * 2 orders), got {len(seen_params)}"
    )
    # Every requested combination appears exactly once.
    combos = {(p["category"], p["timePeriod"], p["orderBy"]) for p in seen_params}
    assert len(combos) == 80
    assert sweep.discovered == {"0xabc"}
    assert sweep.slices_succeeded == 80
    assert sweep.per_wallet["0xabc"].appearances == 80
    # Best rank == 1 because the wallet was always at offset 0, position 1.
    assert sweep.per_wallet["0xabc"].best_rank == 1
    assert set(sweep.per_wallet["0xabc"].categories) == set(
        PolymarketClient.LEADERBOARD_CATEGORIES
    )
    client.close()


def test_run_deep_leaderboard_skips_slice_4xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        # Reject one specific slice — sweep must continue.
        if params.get("category") == "WEATHER" and params.get("orderBy") == "VOL":
            return httpx.Response(400, json={"error": "bad slice"})
        return httpx.Response(200, json=[_lb_row("0xabc")])

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    sweep = run_deep_leaderboard(client, stores, depth=50)

    # 80 attempted - 4 rejected (WEATHER × VOL × 4 time periods).
    assert sweep.slices_succeeded == 76
    assert sweep.discovered == {"0xabc"}
    client.close()


def test_run_deep_leaderboard_walks_offsets_until_short_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short page should end the offset loop for that slice."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/leaderboard":
            return httpx.Response(404)
        # Only count for a single slice — return a full first page and short second.
        params = dict(request.url.params)
        if params.get("category") == "OVERALL" and params.get("orderBy") == "PNL" \
                and params.get("timePeriod") == "ALL":
            call_count["n"] += 1
            offset = int(params.get("offset", 0) or 0)
            if offset == 0:
                return httpx.Response(200, json=[_lb_row(f"0x{i:02x}") for i in range(50)])
            # Short page — only 5 rows, paginator should stop after this.
            return httpx.Response(200, json=[_lb_row(f"0x1{i:02x}") for i in range(5)])
        return httpx.Response(200, json=[])

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    run_deep_leaderboard(client, stores, depth=250)

    # 5 offsets requested, but short page at offset=50 stops further pagination.
    assert call_count["n"] == 2
    client.close()


# ── Pruning ──────────────────────────────────────────────────────────────────


def _write_enrichment(
    path: Path, rows: list[PolymarketWalletEnrichment]
) -> None:
    from dataclasses import asdict
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(asdict(r), separators=(",", ":")) + "\n")


def _write_positions(path: Path, rows: list[PolymarketPosition]) -> None:
    from dataclasses import asdict
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(asdict(r), separators=(",", ":")) + "\n")


def _enrichment(
    wallet: str, *, skill: float, resolved: int, last_activity_at: int,
) -> PolymarketWalletEnrichment:
    return PolymarketWalletEnrichment(
        proxy_wallet=wallet, name="n", pseudonym="p",
        resolved_trades=resolved, wins=resolved // 2, losses=resolved // 2,
        win_rate=0.5, skill_likelihood=skill, stddevs_above_expected=0.0,
        edge_mean=0.0, edge_lower_bound=0.0, effective_sample_size=float(resolved),
        resolved_volume_usdc=1000.0, rank_score=1.0,
        total_volume_usdc=1000.0, total_pnl_usdc=100.0,
        avg_position_size_usdc=10.0, trade_count=resolved, last_activity_at=last_activity_at,
    )


def _position(wallet: str, *, size: float, snapshot_at: str) -> PolymarketPosition:
    return PolymarketPosition(
        proxy_wallet=wallet, condition_id="0xc", outcome_index=0, outcome="Yes",
        size=size, avg_price=0.5, current_value_usdc=size * 0.5,
        slug="s", title="t", event_slug="e", snapshot_at=snapshot_at,
    )


def test_prune_archives_only_dormant_undiscovered_unprotected(
    tmp_path: Path,
) -> None:
    enrichment = tmp_path / "polymarket_wallet_enrichment.jsonl"
    positions = tmp_path / "polymarket_positions.jsonl"
    now = int(time.time())
    long_ago = now - 200 * 86400  # 200 days
    recent = now - 5 * 86400      # 5 days

    _write_enrichment(enrichment, [
        # Dormant unskilled — prune candidate.
        _enrichment("0xdormant", skill=0.1, resolved=5, last_activity_at=long_ago),
        # Skilled wallet — protected even when dormant.
        _enrichment("0xskilled", skill=0.95, resolved=50, last_activity_at=long_ago),
        # Recent activity — protected by freshness.
        _enrichment("0xrecent", skill=0.2, resolved=10, last_activity_at=recent),
    ])
    # No open positions written — that's a separate protection axis.

    state = {
        "0xdormant": PolymarketWalletReviewState(
            proxy_wallet="0xdormant", status="active", first_seen_at="2025-01-01T00:00:00Z",
        ),
        "0xskilled": PolymarketWalletReviewState(
            proxy_wallet="0xskilled", status="active", first_seen_at="2025-01-01T00:00:00Z",
        ),
        "0xrecent": PolymarketWalletReviewState(
            proxy_wallet="0xrecent", status="active", first_seen_at="2025-01-01T00:00:00Z",
        ),
        "0xpinned": PolymarketWalletReviewState(
            proxy_wallet="0xpinned", status="pinned", first_seen_at="2025-01-01T00:00:00Z",
            last_activity_at=long_ago,
        ),
        # In current discovery — protected this run regardless of activity.
        "0xdiscovered": PolymarketWalletReviewState(
            proxy_wallet="0xdiscovered", status="active", first_seen_at="2025-01-01T00:00:00Z",
            last_activity_at=long_ago,
        ),
    }

    archived = prune_review_state(
        state=state,
        discovered={"0xdiscovered"},
        enrichment_path=enrichment,
        positions_path=positions,
        dormant_days=90,
    )

    assert archived == 1
    assert state["0xdormant"].status == "archived"
    assert state["0xdormant"].archived_reason == "dormant_and_undiscovered"
    assert state["0xskilled"].status == "active"
    assert state["0xrecent"].status == "active"
    assert state["0xpinned"].status == "pinned"
    assert state["0xdiscovered"].status == "active"


def test_prune_protects_wallets_with_open_positions(tmp_path: Path) -> None:
    """A wallet with no enrichment row but a still-held position must NOT be archived."""
    enrichment = tmp_path / "polymarket_wallet_enrichment.jsonl"
    positions = tmp_path / "polymarket_positions.jsonl"
    enrichment.write_text("", encoding="utf-8")
    _write_positions(positions, [
        _position("0xholder", size=10.0, snapshot_at="2026-05-20T00:00:00Z"),
        # Exited position (size 0) — does NOT count as still-held.
        _position("0xexited", size=0.0, snapshot_at="2026-05-20T00:00:00Z"),
    ])

    state = {
        "0xholder": PolymarketWalletReviewState(
            proxy_wallet="0xholder", status="active", first_seen_at="2025-01-01T00:00:00Z",
        ),
        "0xexited": PolymarketWalletReviewState(
            proxy_wallet="0xexited", status="active", first_seen_at="2025-01-01T00:00:00Z",
        ),
    }
    archived = prune_review_state(
        state=state, discovered=set(),
        enrichment_path=enrichment, positions_path=positions, dormant_days=90,
    )
    assert archived == 1
    assert state["0xholder"].status == "active"
    assert state["0xexited"].status == "archived"


def test_prune_dry_run_does_not_mutate_state(tmp_path: Path) -> None:
    enrichment = tmp_path / "polymarket_wallet_enrichment.jsonl"
    positions = tmp_path / "polymarket_positions.jsonl"
    enrichment.write_text("", encoding="utf-8")
    positions.write_text("", encoding="utf-8")
    state = {
        "0xdorm": PolymarketWalletReviewState(
            proxy_wallet="0xdorm", status="active", first_seen_at="2025-01-01T00:00:00Z",
        ),
    }
    archived = prune_review_state(
        state=state, discovered=set(),
        enrichment_path=enrichment, positions_path=positions,
        dormant_days=90, dry_run=True,
    )
    assert archived == 1
    # Dry run reports the count but leaves state untouched.
    assert state["0xdorm"].status == "active"


def test_prune_uses_position_snapshot_recency(tmp_path: Path) -> None:
    """If a later snapshot shows size=0, the earlier size>0 entry must NOT save
    the wallet from archival — only the LATEST snapshot is authoritative."""
    enrichment = tmp_path / "polymarket_wallet_enrichment.jsonl"
    positions = tmp_path / "polymarket_positions.jsonl"
    enrichment.write_text("", encoding="utf-8")
    _write_positions(positions, [
        _position("0xexit", size=10.0, snapshot_at="2026-05-01T00:00:00Z"),
        _position("0xexit", size=0.0,  snapshot_at="2026-05-15T00:00:00Z"),  # newer
    ])
    state = {
        "0xexit": PolymarketWalletReviewState(
            proxy_wallet="0xexit", status="active", first_seen_at="2025-01-01T00:00:00Z",
        ),
    }
    archived = prune_review_state(
        state=state, discovered=set(),
        enrichment_path=enrichment, positions_path=positions, dormant_days=90,
    )
    assert archived == 1


# ── Sweep → review-state apply ──────────────────────────────────────────────


def test_apply_sweep_reactivates_archived_wallet() -> None:
    from marketsignalos_polymarket.runner import _DeepSweepResult, _WalletSweepMeta
    state = {
        "0xback": PolymarketWalletReviewState(
            proxy_wallet="0xback", status="archived",
            first_seen_at="2025-01-01T00:00:00Z",
            archived_at="2026-04-01T00:00:00Z",
            archived_reason="dormant_and_undiscovered",
        ),
    }
    sweep = _DeepSweepResult(
        discovered={"0xback"},
        leaderboard_entries=1,
        slices_attempted=1,
        slices_succeeded=1,
        per_wallet={"0xback": _WalletSweepMeta(
            appearances=1, categories={"POLITICS"}, time_periods={"WEEK"},
            orders={"PNL"}, best_rank=7,
        )},
    )
    _apply_sweep_to_review_state(state, sweep)
    assert state["0xback"].status == "active"
    assert state["0xback"].archived_at is None
    assert state["0xback"].archived_reason is None
    assert state["0xback"].best_rank == 7
    assert state["0xback"].categories == ["POLITICS"]


def test_apply_sweep_does_not_unpin() -> None:
    from marketsignalos_polymarket.runner import _DeepSweepResult, _WalletSweepMeta
    state = {
        "0xpin": PolymarketWalletReviewState(
            proxy_wallet="0xpin", status="pinned", first_seen_at="2025-01-01T00:00:00Z",
        ),
    }
    sweep = _DeepSweepResult(
        discovered={"0xpin"}, leaderboard_entries=1, slices_attempted=1, slices_succeeded=1,
        per_wallet={"0xpin": _WalletSweepMeta(appearances=1, best_rank=1)},
    )
    _apply_sweep_to_review_state(state, sweep)
    assert state["0xpin"].status == "pinned"


# ── Batch cursor ─────────────────────────────────────────────────────────────


def test_pick_hydration_batch_oldest_polled_first() -> None:
    state = {
        "0xa": PolymarketWalletReviewState(
            proxy_wallet="0xa", status="active", first_seen_at="2025-01-01T00:00:00Z",
            last_polled_at="2026-05-20T00:00:00Z",
        ),
        "0xb": PolymarketWalletReviewState(
            proxy_wallet="0xb", status="active", first_seen_at="2025-01-01T00:00:00Z",
            last_polled_at="2026-05-10T00:00:00Z",
        ),
        # Net-new — never polled, should sort to the front (empty string < any ISO ts).
        "0xnew": PolymarketWalletReviewState(
            proxy_wallet="0xnew", status="active", first_seen_at="2025-01-01T00:00:00Z",
            last_polled_at=None,
        ),
        # Archived — must be skipped.
        "0xskip": PolymarketWalletReviewState(
            proxy_wallet="0xskip", status="archived", first_seen_at="2025-01-01T00:00:00Z",
        ),
        # Pinned — must be polled.
        "0xpin": PolymarketWalletReviewState(
            proxy_wallet="0xpin", status="pinned", first_seen_at="2025-01-01T00:00:00Z",
            last_polled_at="2026-05-15T00:00:00Z",
        ),
    }
    batch = _pick_hydration_batch(state, batch_size=10)
    assert "0xskip" not in batch
    # Net-new wallets first (empty last_polled_at), then oldest-polled.
    assert batch[0] == "0xnew"
    assert batch == ["0xnew", "0xb", "0xpin", "0xa"]


def test_pick_hydration_batch_respects_batch_size() -> None:
    state = {
        f"0x{i:02x}": PolymarketWalletReviewState(
            proxy_wallet=f"0x{i:02x}", status="active",
            first_seen_at="2025-01-01T00:00:00Z",
            last_polled_at=f"2026-05-{i+1:02d}T00:00:00Z",
        )
        for i in range(10)
    }
    batch = _pick_hydration_batch(state, batch_size=3)
    assert len(batch) == 3
    # Oldest first: 0x00, 0x01, 0x02.
    assert batch == ["0x00", "0x01", "0x02"]


# ── Protections helpers ──────────────────────────────────────────────────────


def test_load_skilled_wallets_applies_default_thresholds(tmp_path: Path) -> None:
    path = tmp_path / "polymarket_wallet_enrichment.jsonl"
    _write_enrichment(path, [
        _enrichment("0xpass", skill=0.85, resolved=25, last_activity_at=1),
        _enrichment("0xlowskill", skill=0.5, resolved=100, last_activity_at=1),
        _enrichment("0xfewresolved", skill=0.99, resolved=10, last_activity_at=1),
        _enrichment("0xedge", skill=0.8, resolved=20, last_activity_at=1),  # on boundary
    ])
    skilled = _load_skilled_wallets(path)
    assert skilled == {"0xpass", "0xedge"}


def test_load_wallets_with_open_positions_uses_latest_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "polymarket_positions.jsonl"
    _write_positions(path, [
        _position("0xa", size=5.0, snapshot_at="2026-05-01T00:00:00Z"),
        _position("0xa", size=0.0, snapshot_at="2026-05-15T00:00:00Z"),  # exited
        _position("0xb", size=0.0, snapshot_at="2026-05-01T00:00:00Z"),
        _position("0xb", size=3.0, snapshot_at="2026-05-15T00:00:00Z"),  # entered
    ])
    open_set = _load_wallets_with_open_positions(path)
    assert open_set == {"0xb"}


# ── End-to-end pin / unpin ───────────────────────────────────────────────────


def test_pin_unpin_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    status1 = run_pin_wallet("0xABC", pin=True)
    assert status1 == "pinned"
    state = _load_review_state(tmp_path / "polymarket_wallet_review_state.jsonl")
    assert state["0xabc"].status == "pinned"

    status2 = run_pin_wallet("0xABC", pin=False)
    assert status2 == "active"
    state = _load_review_state(tmp_path / "polymarket_wallet_review_state.jsonl")
    assert state["0xabc"].status == "active"


def test_run_prune_wallets_writes_back_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    review_path = tmp_path / "polymarket_wallet_review_state.jsonl"
    state = {
        "0xdorm": PolymarketWalletReviewState(
            proxy_wallet="0xdorm", status="active",
            first_seen_at="2024-01-01T00:00:00Z",
        ),
    }
    _write_review_state(review_path, state)
    (tmp_path / "polymarket_wallet_enrichment.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "polymarket_positions.jsonl").write_text("", encoding="utf-8")

    counts = run_prune_wallets(dormant_days=90)
    assert counts["pruned_this_run"] == 1
    assert counts["archived_wallets"] == 1
    assert counts["active_wallets"] == 0

    reloaded = _load_review_state(review_path)
    assert reloaded["0xdorm"].status == "archived"


# ── End-to-end deep pipeline ────────────────────────────────────────────────


def test_run_deep_pipeline_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One wallet returned by every slice; sweep → state apply → batch hydrate → write."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "data-api.polymarket.com" and path == "/v1/leaderboard":
            return httpx.Response(200, json=[_lb_row("0xdeep")])
        if host == "data-api.polymarket.com" and path == "/activity":
            return httpx.Response(200, json=[])
        if host == "data-api.polymarket.com" and path == "/positions":
            return httpx.Response(200, json=[])
        if host == "data-api.polymarket.com" and path == "/value":
            return httpx.Response(200, json=[{"user": "0xdeep", "value": 0.0}])
        if host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    client = _client_with_handler(handler)
    result = run_deep_pipeline(
        leaderboard_depth=50, wallet_batch_size=10,
        activity_pages=1, skip_kalshi=True, client=client,
    )
    client.close()

    assert result.discovered_this_run == 1
    assert result.active_wallets == 1
    assert result.archived_wallets == 0
    assert result.pinned_wallets == 0
    assert result.deep_slices_succeeded > 0
    review = _load_review_state(tmp_path / "polymarket_wallet_review_state.jsonl")
    assert "0xdeep" in review
    # last_polled_at was stamped during hydration.
    assert review["0xdeep"].last_polled_at is not None
