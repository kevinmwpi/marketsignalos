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
    _fold_recent_traders_into_sweep,
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
    run_recent_traders_seed,
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
    # depth=50 = single offset per slice; both orders → 10 * 4 * 2 = 80 calls.
    sweep = run_deep_leaderboard(client, stores, depth=50, orders=("PNL", "VOL"))

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
    sweep = run_deep_leaderboard(client, stores, depth=50, orders=("PNL", "VOL"))

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
    run_deep_leaderboard(client, stores, depth=250, orders=("PNL", "VOL"))

    # 5 offsets requested, but short page at offset=50 stops further pagination.
    assert call_count["n"] == 2
    client.close()


def test_run_deep_leaderboard_defaults_to_vol_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PnL/profit ranking is the luck-bias vector — the default sweep must
    request only VOL slices (10 cats × 4 periods × 1 order = 40)."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    orders_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/leaderboard":
            return httpx.Response(404)
        orders_seen.append(dict(request.url.params)["orderBy"])
        return httpx.Response(200, json=[_lb_row("0xabc")])

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    sweep = run_deep_leaderboard(client, stores, depth=50)
    client.close()

    assert set(orders_seen) == {"VOL"}, "PnL must not be swept by default"
    assert len(orders_seen) == 40
    assert sweep.per_wallet["0xabc"].sources == {"leaderboard"}


def test_run_deep_leaderboard_rejects_unknown_order(tmp_path: Path) -> None:
    client = _client_with_handler(lambda r: httpx.Response(200, json=[]))
    stores = _build_stores(tmp_path)
    with pytest.raises(ValueError, match="unknown order"):
        run_deep_leaderboard(client, stores, depth=50, orders=("ROI",))
    client.close()


def test_run_deep_leaderboard_circuit_breaker_trips_on_consecutive_5xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sustained 5xx upstream should trip the breaker and abort the sweep
    early instead of hammering every slice in the matrix."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream down"})

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    sweep = run_deep_leaderboard(client, stores, depth=50, breaker_threshold=3)

    assert sweep.aborted_early is True
    assert sweep.abort_reason is not None
    assert "degraded" in sweep.abort_reason
    # Stopped after exactly `breaker_threshold` failing slices — NOT the
    # whole 40-slice default-VOL matrix.
    assert sweep.slices_attempted == 3
    assert sweep.slices_succeeded == 0
    assert sweep.discovered == set()
    client.close()


def test_run_deep_leaderboard_circuit_breaker_trips_on_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection-level failures (not just HTTP status) must trip the breaker.
    Previously such errors propagated uncaught and killed the run on the
    first blip."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    sweep = run_deep_leaderboard(client, stores, depth=50, breaker_threshold=2)

    assert sweep.aborted_early is True
    assert sweep.slices_attempted == 2
    assert sweep.discovered == set()
    client.close()


def test_run_deep_leaderboard_breaker_ignores_4xx_skipped_slices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain 4xx signals an unsupported slice combo, not a degraded API.
    Even if EVERY slice 400s, the breaker must NOT trip — this preserves
    the existing per-slice-skip contract."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad slice"})

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    sweep = run_deep_leaderboard(client, stores, depth=50, breaker_threshold=3)

    assert sweep.aborted_early is False
    # All 40 default-VOL slices attempted; none short-circuited by the breaker.
    assert sweep.slices_attempted == 40
    assert sweep.slices_succeeded == 0
    client.close()


def test_run_deep_leaderboard_breaker_resets_on_intervening_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful slice between failures resets the consecutive counter,
    so a flaky-but-not-down API never trips a sane threshold."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    seen_slices: set[tuple[str, str, str]] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        key = (params["category"], params["timePeriod"], params["orderBy"])
        seen_slices.add(key)
        # Every 4th unique slice succeeds → max consecutive failures = 3.
        if len(seen_slices) % 4 == 0:
            return httpx.Response(
                200, json=[_lb_row(f"0xS{len(seen_slices):02x}")],
            )
        return httpx.Response(502, json={"error": "blip"})

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    sweep = run_deep_leaderboard(client, stores, depth=50, breaker_threshold=5)

    assert sweep.aborted_early is False
    # Every default-VOL slice was attempted (40 total) since reset works.
    assert sweep.slices_attempted == 40
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


def test_fold_recent_traders_into_sweep_tags_subgraph_source() -> None:
    from marketsignalos_polymarket.runner import _DeepSweepResult, _WalletSweepMeta
    sweep = _DeepSweepResult(
        discovered={"0xlb"},
        leaderboard_entries=1,
        slices_attempted=1,
        slices_succeeded=1,
        per_wallet={"0xlb": _WalletSweepMeta(appearances=1, sources={"leaderboard"})},
    )
    # 0xlb was already on the leaderboard; 0xnew is subgraph-only.
    _fold_recent_traders_into_sweep(sweep, ["0xlb", "0xnew"])
    assert sweep.discovered == {"0xlb", "0xnew"}
    assert sweep.per_wallet["0xlb"].sources == {"leaderboard", "subgraph"}
    assert sweep.per_wallet["0xnew"].sources == {"subgraph"}


def test_apply_sweep_subgraph_only_wallet_has_no_leaderboard_clock() -> None:
    """A wallet surfaced purely from recent fills records sources=['subgraph']
    but must NOT get a last_leaderboard_seen_at stamp."""
    from marketsignalos_polymarket.runner import _DeepSweepResult, _WalletSweepMeta
    state: dict[str, PolymarketWalletReviewState] = {}
    sweep = _DeepSweepResult(
        discovered={"0xsub"},
        leaderboard_entries=0,
        slices_attempted=0,
        slices_succeeded=0,
        per_wallet={"0xsub": _WalletSweepMeta(sources={"subgraph"})},
    )
    _apply_sweep_to_review_state(state, sweep)
    assert state["0xsub"].status == "active"
    assert state["0xsub"].sources == ["subgraph"]
    assert state["0xsub"].last_leaderboard_seen_at is None


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
        if host == "api.goldsky.com":
            return httpx.Response(200, json={"data": {"orderFilledEvents": []}})
        return httpx.Response(200, json=[])

    client = _client_with_handler(handler)
    result = run_deep_pipeline(
        leaderboard_depth=50, wallet_batch_size=10,
        activity_pages=1, skip_kalshi=True, client=client,
    )
    client.close()

    assert result.discovered_this_run == 1
    assert result.recent_traders_discovered == 0  # subgraph returned no fills
    assert result.active_wallets == 1
    assert result.archived_wallets == 0
    assert result.pinned_wallets == 0
    assert result.deep_slices_succeeded > 0
    review = _load_review_state(tmp_path / "polymarket_wallet_review_state.jsonl")
    assert "0xdeep" in review
    # last_polled_at was stamped during hydration.
    assert review["0xdeep"].last_polled_at is not None


def _ofe(maker: str, taker: str, ts: int) -> dict[str, Any]:
    return {"maker": {"id": maker}, "taker": {"id": taker}, "timestamp": str(ts)}


def test_run_deep_pipeline_folds_in_recent_traders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recent on-chain traders are unioned into the sweep: they enter
    review-state tagged 'subgraph', are protected from prune (in discovered),
    and get hydrated alongside leaderboard wallets."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "data-api.polymarket.com" and path == "/v1/leaderboard":
            return httpx.Response(200, json=[_lb_row("0xlb")])
        if host == "api.goldsky.com":
            return httpx.Response(200, json={"data": {"orderFilledEvents": [
                _ofe("0xRecent1", "0xRecent2", 100),
            ]}})
        if host == "data-api.polymarket.com" and path == "/value":
            return httpx.Response(200, json=[{"user": "0x", "value": 0.0}])
        if host == "data-api.polymarket.com":  # /activity, /positions
            return httpx.Response(200, json=[])
        if host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    client = _client_with_handler(handler)
    result = run_deep_pipeline(
        leaderboard_depth=50, wallet_batch_size=10, activity_pages=1,
        recent_trader_limit=10, skip_kalshi=True, client=client,
    )
    client.close()

    assert result.recent_traders_discovered == 2
    assert result.discovered_this_run == 3  # 0xlb + the two subgraph wallets

    review = _load_review_state(tmp_path / "polymarket_wallet_review_state.jsonl")
    assert {"0xlb", "0xrecent1", "0xrecent2"} <= set(review)
    assert "subgraph" in review["0xrecent1"].sources
    assert "subgraph" in review["0xrecent2"].sources
    assert "leaderboard" in review["0xlb"].sources
    assert review["0xrecent1"].status == "active"


def test_run_deep_pipeline_can_skip_recent_traders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.goldsky.com":
            raise AssertionError("subgraph must not be called when disabled")
        if request.url.host == "data-api.polymarket.com" and request.url.path == "/v1/leaderboard":
            return httpx.Response(200, json=[_lb_row("0xlb")])
        if request.url.host == "data-api.polymarket.com" and request.url.path == "/value":
            return httpx.Response(200, json=[{"user": "0x", "value": 0.0}])
        return httpx.Response(200, json=[])

    client = _client_with_handler(handler)
    result = run_deep_pipeline(
        leaderboard_depth=50, wallet_batch_size=10, activity_pages=1,
        seed_recent_traders=False, skip_kalshi=True, client=client,
    )
    client.close()
    assert result.recent_traders_discovered == 0


def test_run_recent_traders_seed_writes_subgraph_wallets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.goldsky.com":
            return httpx.Response(200, json={"data": {"orderFilledEvents": [
                _ofe("0xAaa", "0xBbb", 100),
            ]}})
        return httpx.Response(404)

    client = _client_with_handler(handler)
    counts = run_recent_traders_seed(limit=10, max_pages=2, client=client)
    client.close()

    assert counts["recent_traders_discovered"] == 2
    assert counts["added"] == 2
    review = _load_review_state(tmp_path / "polymarket_wallet_review_state.jsonl")
    assert review["0xaaa"].sources == ["subgraph"]
    assert review["0xbbb"].status == "active"
    assert review["0xaaa"].last_leaderboard_seen_at is None


def test_run_deep_pipeline_partial_success_on_degraded_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A breaker abort mid-sweep must NOT raise: the deep pipeline completes
    as a partial success, skips pruning (so unreached wallets aren't wrongly
    archived), and surfaces a warning in the result."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))

    # Pre-seed a dormant, unprotected wallet that prune WOULD archive.
    review_path = tmp_path / "polymarket_wallet_review_state.jsonl"
    _write_review_state(review_path, {
        "0xdormant": PolymarketWalletReviewState(
            proxy_wallet="0xdormant", status="active",
            first_seen_at="2024-01-01T00:00:00Z",
        ),
    })
    (tmp_path / "polymarket_wallet_enrichment.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "polymarket_positions.jsonl").write_text("", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        # Leaderboard slices → 502 (will trip the breaker after threshold).
        if request.url.path == "/v1/leaderboard":
            return httpx.Response(502, json={"error": "down"})
        # Everything else returns empty so the rest of the pipeline can
        # complete cleanly on the partial sweep.
        return httpx.Response(200, json=[])

    client = _client_with_handler(handler)
    result = run_deep_pipeline(
        leaderboard_depth=50, wallet_batch_size=10,
        activity_pages=1, seed_recent_traders=False, skip_kalshi=True,
        client=client,
    )
    client.close()

    # Partial-success contract: warning surfaces, but no exception raised.
    assert result.warning is not None
    assert "degraded" in result.warning
    # Prune was skipped → dormant wallet survives (the critical bug we
    # avoid by gating prune on aborted_early).
    assert result.pruned_this_run == 0
    assert result.discovered_this_run == 0
    reloaded = _load_review_state(review_path)
    assert reloaded["0xdormant"].status == "active"
    # The warning flows through to_dict() so the API surfaces it as
    # last_summary.warning.
    assert result.to_dict()["warning"] == result.warning


def test_run_deep_pipeline_subgraph_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subgraph blip must not kill an otherwise-healthy run. Without the
    try/except wrap, a degraded subgraph during the recent-traders step
    would raise and the breaker's partial-success path could never surface
    its warning to the UI."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))

    def handler(request: httpx.Request) -> httpx.Response:
        # Subgraph refuses to connect — the rest of the API is healthy.
        if request.url.host == "api.goldsky.com":
            raise httpx.ConnectError("subgraph unreachable", request=request)
        if request.url.path == "/v1/leaderboard":
            return httpx.Response(200, json=[_lb_row("0xfromlb")])
        return httpx.Response(200, json=[])

    client = _client_with_handler(handler)
    result = run_deep_pipeline(
        leaderboard_depth=50, wallet_batch_size=10,
        activity_pages=1, skip_kalshi=True, client=client,
    )
    client.close()

    # Subgraph failure absorbed; leaderboard wallet still flowed through.
    assert result.recent_traders_discovered == 0
    assert result.discovered_this_run >= 1
