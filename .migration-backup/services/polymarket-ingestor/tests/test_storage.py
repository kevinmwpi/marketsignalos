from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from marketsignalos_polymarket.models import (
    MarketLink,
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketPosition,
    PolymarketPositionSnapshot,
    PolymarketWalletBet,
    PolymarketWalletHydration,
    PolymarketWalletValue,
)
from marketsignalos_polymarket.storage import (
    DualActivityStore,
    DualLeaderboardStore,
    DualMarketLinkStore,
    DualMarketStore,
    DualPositionStore,
    DualWalletBetStore,
    DualWalletCheckpointStore,
    DualWalletValueStore,
    JsonlActivityStore,
    JsonlLeaderboardStore,
    JsonlMarketLinkStore,
    JsonlMarketStore,
    JsonlPositionSnapshotStore,
    JsonlPositionStore,
    JsonlWalletBetStore,
    JsonlWalletHydrationStore,
    JsonlWalletValueStore,
    JsonWalletCheckpointStore,
)


def test_leaderboard_store_appends(tmp_path: Path) -> None:
    store = JsonlLeaderboardStore(tmp_path / "lb.jsonl")
    entry = PolymarketLeaderboardEntry(
        proxy_wallet="0xabc",
        name="Theo4",
        pseudonym="Theo4",
        amount_usdc=22_053_933.75,
        metric="profit",
        window="all",
    )
    assert store.write_leaderboard([entry]) == 1
    line = (tmp_path / "lb.jsonl").read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["proxy_wallet"] == "0xabc"
    assert payload["amount_usdc"] == 22_053_933.75


def test_activity_store_dedupes_on_tx_hash_condition_outcome(tmp_path: Path) -> None:
    store = JsonlActivityStore(tmp_path / "activity.jsonl")
    a = PolymarketActivity(
        proxy_wallet="0xabc",
        timestamp=1715000000,
        condition_id="0xcond",
        type="TRADE",
        side="BUY",
        size=100.0,
        usdc_size=62.0,
        price=0.62,
        outcome_index=0,
        outcome="Yes",
        slug="some-market",
        title="Some market",
        event_slug="some-event",
        transaction_hash="0xtx1",
    )
    # same tx_hash but different outcome leg should NOT dedupe
    b = PolymarketActivity(
        proxy_wallet="0xabc",
        timestamp=1715000000,
        condition_id="0xcond",
        type="TRADE",
        side="BUY",
        size=50.0,
        usdc_size=19.0,
        price=0.38,
        outcome_index=1,
        outcome="No",
        slug="some-market",
        title="Some market",
        event_slug="some-event",
        transaction_hash="0xtx1",
    )
    # exact duplicate of `a` should dedupe
    a_dup = replace(a)

    assert store.write_activity([a, b]) == 2
    assert store.write_activity([a_dup]) == 0

    lines = (tmp_path / "activity.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_market_store_refreshes_latest_state_by_condition(tmp_path: Path) -> None:
    store = JsonlMarketStore(tmp_path / "markets.jsonl")
    open_market = PolymarketMarket(
        gamma_id="1", condition_id="0xc", slug="s", question="q?", category="cat",
        end_date="2026-01-01T00:00:00Z", outcomes=["Yes", "No"], outcome_prices=[0.5, 0.5],
        volume_usdc=1000, liquidity_usdc=200, closed=False, active=True,
        last_trade_price=0.5, best_bid=0.49, best_ask=0.51,
    )
    closed_market = replace(open_market, closed=True, active=False)

    assert store.write_markets([open_market]) == 1
    # Live records refresh even when closed status is unchanged.
    assert store.write_markets([open_market]) == 1
    assert store.write_markets([closed_market]) == 1
    lines = (tmp_path / "markets.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["closed"] is True


def test_position_store_appends_snapshots(tmp_path: Path) -> None:
    store = JsonlPositionStore(tmp_path / "positions.jsonl")
    pos = PolymarketPosition(
        proxy_wallet="0xabc", condition_id="0xc", outcome_index=0, outcome="Yes",
        size=100, avg_price=0.62, current_value_usdc=68, slug="s", title="t", event_slug="e",
    )
    assert store.write_positions([pos]) == 1
    # Snapshots: writing the same position twice appends, doesn't dedupe.
    assert store.write_positions([pos]) == 1
    lines = (tmp_path / "positions.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_position_snapshot_store_replaces_wallet_manifest(tmp_path: Path) -> None:
    path = tmp_path / "snapshots.jsonl"
    store = JsonlPositionSnapshotStore(path)
    store.write_position_snapshot(PolymarketPositionSnapshot(
        proxy_wallet="0xabc", snapshot_id="old", position_count=2, complete=True,
    ))
    store.write_position_snapshot(PolymarketPositionSnapshot(
        proxy_wallet="0xabc", snapshot_id="new", position_count=0, complete=True,
    ))
    rows = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["snapshot_id"] == "new"


def test_wallet_hydration_store_round_trip(tmp_path: Path) -> None:
    store = JsonlWalletHydrationStore(tmp_path / "hydration.jsonl")
    state = PolymarketWalletHydration(
        proxy_wallet="0xabc", activity_history_complete=True, metadata_coverage=1.0,
    )
    assert store.upsert_hydration([state]) == 1
    assert store.load_hydration()["0xabc"].activity_history_complete is True


def test_wallet_value_store(tmp_path: Path) -> None:
    store = JsonlWalletValueStore(tmp_path / "values.jsonl")
    assert store.write_values([PolymarketWalletValue(proxy_wallet="0xabc", value_usdc=12345.67)]) == 1


def test_checkpoint_store_round_trip_and_monotonic(tmp_path: Path) -> None:
    store = JsonWalletCheckpointStore(tmp_path / "ckpt.json")
    assert store.get_last_timestamp("0xABC") is None

    store.set_last_timestamp("0xABC", 1_700_000_000)
    # Wallet keys must normalize to lowercase so case variations don't multiply checkpoints.
    assert store.get_last_timestamp("0xabc") == 1_700_000_000

    # Setting an OLDER timestamp must not regress the watermark.
    store.set_last_timestamp("0xabc", 1_600_000_000)
    assert store.get_last_timestamp("0xabc") == 1_700_000_000

    # Setting a NEWER timestamp advances it.
    store.set_last_timestamp("0xabc", 1_800_000_000)
    assert store.get_last_timestamp("0xabc") == 1_800_000_000


def test_checkpoint_store_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "ckpt.json"
    JsonWalletCheckpointStore(path).set_last_timestamp("0xa", 1234)
    assert JsonWalletCheckpointStore(path).get_last_timestamp("0xa") == 1234


def _link(ticker: str, cond: str, status: str, by: str, confidence: float = 0.8) -> MarketLink:
    return MarketLink(
        kalshi_ticker=ticker,
        polymarket_condition_id=cond,
        polymarket_slug="s",
        kalshi_title="kt",
        polymarket_title="pt",
        kalshi_end_date="2026-01-01T00:00:00Z",
        polymarket_end_date="2026-01-01T00:00:00Z",
        confidence=confidence,
        status=status,
        matched_by=by,
    )


def test_market_link_store_round_trip(tmp_path: Path) -> None:
    store = JsonlMarketLinkStore(tmp_path / "links.jsonl")
    assert store.load_links() == []
    written = store.upsert_links([_link("K1", "0xp1", "pending", "auto")])
    assert written == 1
    assert len(store.load_links()) == 1


def test_market_link_store_manual_decisions_are_sticky(tmp_path: Path) -> None:
    """A re-run of the auto matcher must not overwrite a human rejection."""
    store = JsonlMarketLinkStore(tmp_path / "links.jsonl")
    store.upsert_links([_link("K1", "0xp1", "rejected", "manual")])

    # The auto matcher comes back later and tries to insert it as pending —
    # this would silently re-surface a known false positive. Must be blocked.
    store.upsert_links([_link("K1", "0xp1", "pending", "auto")])

    links = store.load_links()
    assert len(links) == 1
    assert links[0].status == "rejected"
    assert links[0].matched_by == "manual"


def test_market_link_store_auto_can_be_updated_by_another_auto_pass(tmp_path: Path) -> None:
    """If matched_by is still 'auto', a re-run CAN update confidence / status."""
    store = JsonlMarketLinkStore(tmp_path / "links.jsonl")
    store.upsert_links([_link("K1", "0xp1", "pending", "auto", confidence=0.5)])
    store.upsert_links([_link("K1", "0xp1", "approved", "auto", confidence=0.9)])
    links = store.load_links()
    assert len(links) == 1
    assert links[0].status == "approved"
    assert links[0].confidence == 0.9


def test_market_link_store_separate_keys_dont_collide(tmp_path: Path) -> None:
    store = JsonlMarketLinkStore(tmp_path / "links.jsonl")
    store.upsert_links([
        _link("K1", "0xp1", "approved", "auto"),
        _link("K1", "0xp2", "pending", "auto"),
        _link("K2", "0xp1", "pending", "auto"),
    ])
    assert len(store.load_links()) == 3


# ── Dual-write wrappers ──────────────────────────────────────────────────────
#
# Use two independent JSONL stores in different tmp_path subdirs as stand-ins
# for the JSONL+Postgres pair. The Dual wrappers are storage-backend-agnostic,
# so this exercises the fan-out + read-from-primary contract without needing
# a live DB.

class _CountingSecondary:
    """Records calls to write_* / set_last_timestamp / upsert_links so tests
    can assert the secondary saw the same payload as the primary."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def write_leaderboard(self, entries: list) -> int:  # type: ignore[type-arg]
        self.calls.append(("leaderboard", len(entries)))
        return len(entries)

    def write_activity(self, events: list) -> int:  # type: ignore[type-arg]
        self.calls.append(("activity", len(events)))
        return len(events)

    def write_positions(self, positions: list) -> int:  # type: ignore[type-arg]
        self.calls.append(("positions", len(positions)))
        return len(positions)

    def write_markets(self, markets: list) -> int:  # type: ignore[type-arg]
        self.calls.append(("markets", len(markets)))
        return len(markets)

    def write_values(self, values: list) -> int:  # type: ignore[type-arg]
        self.calls.append(("values", len(values)))
        return len(values)

    def write_enrichment(self, enrichments: list) -> int:  # type: ignore[type-arg]
        self.calls.append(("enrichment", len(enrichments)))
        return len(enrichments)

    def upsert_links(self, links: list) -> int:  # type: ignore[type-arg]
        self.calls.append(("links", len(links)))
        return len(links)

    def load_links(self) -> list:  # type: ignore[type-arg]
        # Secondary read should NOT be hit by the Dual wrapper — return a
        # sentinel that would fail the test if it were ever surfaced.
        raise AssertionError("DualMarketLinkStore.load_links must read from primary")

    def get_last_timestamp(self, wallet: str) -> int | None:
        raise AssertionError(
            "DualWalletCheckpointStore.get_last_timestamp must read from primary"
        )

    def set_last_timestamp(self, wallet: str, timestamp: int) -> None:
        self.calls.append((f"checkpoint:{wallet}", timestamp))


def test_dual_leaderboard_fans_out_to_secondary(tmp_path: Path) -> None:
    primary = JsonlLeaderboardStore(tmp_path / "lb.jsonl")
    secondary = _CountingSecondary()
    store = DualLeaderboardStore(primary, secondary)
    entry = PolymarketLeaderboardEntry(
        proxy_wallet="0xabc", name="A", pseudonym="a",
        amount_usdc=1.0, metric="profit", window="all",
    )
    # Returned count comes from the primary (JSONL).
    assert store.write_leaderboard([entry]) == 1
    assert secondary.calls == [("leaderboard", 1)]


def test_dual_activity_fans_out(tmp_path: Path) -> None:
    primary = JsonlActivityStore(tmp_path / "a.jsonl")
    secondary = _CountingSecondary()
    store = DualActivityStore(primary, secondary)  # type: ignore[arg-type]
    a = PolymarketActivity(
        proxy_wallet="0xabc", timestamp=1, condition_id="c", type="TRADE",
        side="BUY", size=1.0, usdc_size=1.0, price=0.5, outcome_index=0,
        outcome="Yes", slug="s", title="t", event_slug="e",
        transaction_hash="0xtx",
    )
    store.write_activity([a])
    assert secondary.calls == [("activity", 1)]


def test_dual_position_fans_out(tmp_path: Path) -> None:
    primary = JsonlPositionStore(tmp_path / "p.jsonl")
    secondary = _CountingSecondary()
    store = DualPositionStore(primary, secondary)
    pos = PolymarketPosition(
        proxy_wallet="0xabc", condition_id="c", outcome_index=0, outcome="Yes",
        size=1, avg_price=0.5, current_value_usdc=1.0,
        slug="s", title="t", event_slug="e",
    )
    store.write_positions([pos])
    assert secondary.calls == [("positions", 1)]


def test_dual_market_fans_out(tmp_path: Path) -> None:
    primary = JsonlMarketStore(tmp_path / "m.jsonl")
    secondary = _CountingSecondary()
    store = DualMarketStore(primary, secondary)
    m = PolymarketMarket(
        gamma_id="1", condition_id="c", slug="s", question="q?", category="",
        end_date="", outcomes=["Yes", "No"], outcome_prices=[0.5, 0.5],
        volume_usdc=0, liquidity_usdc=0, closed=False, active=True,
        last_trade_price=None, best_bid=None, best_ask=None,
    )
    store.write_markets([m])
    assert secondary.calls == [("markets", 1)]


def test_dual_wallet_value_fans_out(tmp_path: Path) -> None:
    primary = JsonlWalletValueStore(tmp_path / "v.jsonl")
    secondary = _CountingSecondary()
    store = DualWalletValueStore(primary, secondary)
    store.write_values([PolymarketWalletValue(proxy_wallet="0xabc", value_usdc=10.0)])
    assert secondary.calls == [("values", 1)]


def test_dual_checkpoint_reads_from_primary(tmp_path: Path) -> None:
    """Read path must NOT touch the secondary (asserts inside _CountingSecondary
    would fire). Write path fans out to both."""
    primary = JsonWalletCheckpointStore(tmp_path / "ckpt.json")
    secondary = _CountingSecondary()
    store = DualWalletCheckpointStore(primary, secondary)

    assert store.get_last_timestamp("0xabc") is None  # would raise if secondary hit
    store.set_last_timestamp("0xabc", 1234)
    assert secondary.calls == [("checkpoint:0xabc", 1234)]
    assert store.get_last_timestamp("0xabc") == 1234


def test_dual_market_link_reads_from_primary(tmp_path: Path) -> None:
    primary = JsonlMarketLinkStore(tmp_path / "links.jsonl")
    secondary = _CountingSecondary()
    store = DualMarketLinkStore(primary, secondary)

    # load_links must not hit secondary.
    assert store.load_links() == []
    written = store.upsert_links([_link("K1", "0xp1", "pending", "auto")])
    assert written == 1
    assert secondary.calls == [("links", 1)]


def _wallet_bet(wallet: str, cond: str) -> PolymarketWalletBet:
    return PolymarketWalletBet(
        proxy_wallet=wallet,
        condition_id=cond,
        outcome_index=0,
        outcome="Yes",
        event_slug="e",
        category="sports",
        title="t",
        status="won",
        entry_price=0.5,
        cost_usdc=10.0,
        net_size=20.0,
        realized_pnl_usdc=0.0,
        total_pnl_usdc=10.0,
        clv=None,
        last_trade_ts=1000,
        computed_at="2026-07-14T00:00:00Z",
    )


def test_wallet_bet_rewrite_streams_batches(tmp_path: Path) -> None:
    """Batches handed to the rewrite writer land as one coherent file — the
    streaming path enrichment uses so millions of records never sit in RAM."""
    store = JsonlWalletBetStore(tmp_path / "bets.jsonl")
    with store.rewrite() as write_batch:
        assert write_batch([_wallet_bet("0xa", "0xc1"), _wallet_bet("0xa", "0xc2")]) == 2
        assert write_batch([_wallet_bet("0xb", "0xc3")]) == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "bets.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(r["proxy_wallet"], r["condition_id"]) for r in rows] == [
        ("0xa", "0xc1"), ("0xa", "0xc2"), ("0xb", "0xc3"),
    ]


def test_wallet_bet_rewrite_is_atomic_on_failure(tmp_path: Path) -> None:
    """A crash mid-rewrite must leave the previous file untouched (and no
    stray temp file), so a failed enrichment can't corrupt the bet history."""
    path = tmp_path / "bets.jsonl"
    store = JsonlWalletBetStore(path)
    store.write_bets([_wallet_bet("0xold", "0xc0")])
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError), store.rewrite() as write_batch:
        write_batch([_wallet_bet("0xnew", "0xc9")])
        raise RuntimeError("simulated crash mid-rewrite")

    assert path.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.tmp"))


def test_wallet_bet_write_bets_round_trips_via_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "bets.jsonl"
    store = JsonlWalletBetStore(path)
    assert store.write_bets([_wallet_bet("0xa", "0xc1")]) == 1
    # A second call fully replaces the previous pass (overwrite semantics).
    assert store.write_bets([_wallet_bet("0xb", "0xc2")]) == 1
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["proxy_wallet"] for r in rows] == ["0xb"]


def test_dual_wallet_bet_rewrite_fans_out(tmp_path: Path) -> None:
    primary = JsonlWalletBetStore(tmp_path / "primary.jsonl")
    secondary = JsonlWalletBetStore(tmp_path / "secondary.jsonl")
    dual = DualWalletBetStore(primary, secondary)
    with dual.rewrite() as write_batch:
        assert write_batch([_wallet_bet("0xa", "0xc1")]) == 1
        assert write_batch([_wallet_bet("0xb", "0xc2")]) == 1
    for name in ("primary.jsonl", "secondary.jsonl"):
        rows = [
            json.loads(line)
            for line in (tmp_path / name).read_text(encoding="utf-8").splitlines()
        ]
        assert [r["proxy_wallet"] for r in rows] == ["0xa", "0xb"]
