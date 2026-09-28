from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketsignalos_polymarket import matching_cases
from marketsignalos_polymarket.kalshi_markets_fetch import KalshiMarket, write_kalshi_markets_jsonl
from marketsignalos_polymarket.market_matcher import NormalizedMarket, _cosine, match_markets
from marketsignalos_polymarket.market_rules import MarketNotFound
from marketsignalos_polymarket.matching_cases import (
    Candidate,
    build_candidate_pool,
    label_queue,
    sample_candidates,
    seed_from_market_links,
    select_sample,
    stratum_quotas,
    verify_fields,
)
from marketsignalos_polymarket.matching_eval import (
    CaseLabel,
    load_cases,
    make_case_id,
    write_cases_atomic,
)
from marketsignalos_polymarket.models import MarketLink, PolymarketMarket
from marketsignalos_polymarket.storage import JsonlMarketLinkStore

# ── Fixture markets ──────────────────────────────────────────────────────────
# Kalshi brackets of one event share a generic title and differ only in
# yes_sub_title, which the production matcher never reads. That is the trap.

def _kalshi(ticker: str, event: str, title: str, expiry: str, category: str,
            bracket: str = "") -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker, event_ticker=event, title=title, subtitle="", yes_sub_title=bracket,
        category=category, status="open", expiration_time=expiry, close_time=expiry,
        yes_bid=40, yes_ask=42, last_price=41,
    )


def _poly(cid: str, question: str, end: str, category: str = "") -> PolymarketMarket:
    return PolymarketMarket(
        gamma_id=cid, condition_id=cid, slug=cid.replace("0x", "slug-"), question=question,
        category=category, end_date=end, outcomes=["Yes", "No"], outcome_prices=[0.4, 0.6],
        volume_usdc=1000.0, liquidity_usdc=500.0, closed=False, active=True,
        last_trade_price=0.4, best_bid=0.39, best_ask=0.41,
    )


KALSHI = [
    _kalshi("KXFED-25SEP-T25", "KXFED-25SEP", "Fed rate cut at September 2025 meeting",
            "2025-09-17T18:00:00Z", "Economics", "Cut of 25 bps"),
    _kalshi("KXFED-25SEP-T50", "KXFED-25SEP", "Fed rate cut at September 2025 meeting",
            "2025-09-17T18:00:00Z", "Economics", "Cut of 50 bps"),
    _kalshi("KXBTC-25DEC-100K", "KXBTC-25DEC", "Bitcoin above 100k on December 31 2025",
            "2025-12-31T23:59:00Z", "Crypto"),
    _kalshi("KXBTC-25DEC-120K", "KXBTC-25DEC", "Bitcoin above 120k on December 31 2025",
            "2025-12-31T23:59:00Z", "Crypto"),
    _kalshi("KXSENATE-26", "KXSENATE-26", "Democrats win the Senate in 2026",
            "2026-11-03T12:00:00Z", "Politics"),
    _kalshi("KXCPI-25AUG", "KXCPI-25AUG", "CPI above 3 percent in August 2025",
            "2025-09-11T12:00:00Z", "Economics"),
    _kalshi("KXMVE-PARLAY1", "KXMVE", "Fed rate cut at September 2025 meeting",
            "2025-09-17T18:00:00Z", "Economics"),
]
POLY = [
    _poly("0xfed1", "Fed rate cut of 25 bps at September 2025 meeting?",
          "2025-09-17T00:00:00Z", "Economics"),
    _poly("0xbtc1", "Bitcoin above 100k on December 31, 2025?", "2025-12-31T12:00:00Z", "Crypto"),
    # Same proposition, but an end date the 3-day window cannot reach.
    _poly("0xsen1", "Democrats win the Senate in 2026?", "2026-12-31T00:00:00Z", "Politics"),
    # Same proposition, end dates 1.5 days apart: inside the window, flagged as a gap.
    _poly("0xcpi1", "CPI above 3 percent in August 2025?", "2025-09-13T00:00:00Z", "Economics"),
    _poly("0xoil1", "Will oil close above 90 dollars in September 2025?", "2025-09-18T00:00:00Z"),
]


def _write_inputs(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    write_kalshi_markets_jsonl(KALSHI, data_dir / "kalshi_markets.jsonl")
    with (data_dir / "polymarket_markets.jsonl").open("w", encoding="utf-8") as handle:
        for market in POLY:
            handle.write(json.dumps(asdict(market)) + "\n")


class FakeExchanges:
    """Stands in for both public APIs; records every request."""

    def __init__(self, *, missing: frozenset[str] = frozenset(),
                 fail_with: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.missing = missing
        self.fail_with = fail_with

    def __call__(self, url: str, params: dict[str, str]) -> Any:
        self.calls.append((url, dict(params)))
        if self.fail_with is not None:
            raise self.fail_with
        if "/markets/" in url:
            ticker = url.rsplit("/", 1)[1]
            if ticker in self.missing:
                raise MarketNotFound(url)
            return {"market": {
                "ticker": ticker, "title": "live title", "yes_sub_title": "live bracket",
                "event_ticker": "LIVE", "expiration_time": "2025-01-01T00:00:00Z",
                "rules_primary": f"Kalshi rules for {ticker}. " + "detail " * 120,
                "rules_secondary": "",
            }}
        cid = params["condition_ids"]
        if cid in self.missing:
            return []
        return [{
            "conditionId": cid, "question": "live question", "slug": "live-slug",
            "endDate": "2025-01-01T00:00:00Z",
            "description": f"Polymarket rules for {cid}.", "resolutionSource": "",
        }]


def _pool() -> list[Candidate]:
    from marketsignalos_polymarket.runner import _kalshi_to_normalized, _polymarket_to_normalized

    kalshi_raw = [m for m in KALSHI if not m.ticker.startswith("KXMVE")]
    return build_candidate_pool(
        kalshi_raw,
        [_kalshi_to_normalized(m) for m in kalshi_raw],
        [_polymarket_to_normalized(m) for m in POLY],
    ).candidates


def _by_key(pool: list[Candidate]) -> dict[str, Candidate]:
    return {c.key: c for c in pool}


# ── Candidate pool ───────────────────────────────────────────────────────────

def test_pool_matches_production_decisions_exactly() -> None:
    from marketsignalos_polymarket.runner import _kalshi_to_normalized, _polymarket_to_normalized

    kalshi_raw = [m for m in KALSHI if not m.ticker.startswith("KXMVE")]
    kalshi = [_kalshi_to_normalized(m) for m in kalshi_raw]
    poly = [_polymarket_to_normalized(m) for m in POLY]
    pool = build_candidate_pool(kalshi_raw, kalshi, poly)
    production = {
        make_case_id(link.kalshi_ticker, link.polymarket_condition_id): link
        for link in match_markets(kalshi, poly)
    }

    assert pool.production_links == len(production) > 0
    assert pool.parity_checked == len(production)
    linked = {c.key: c for c in pool.candidates if c.decision != "dropped"}
    assert set(linked) == set(production)
    for key, candidate in linked.items():
        assert candidate.decision == production[key].status
        assert candidate.confidence == production[key].confidence


def test_pool_flags_the_false_positive_shapes() -> None:
    pool = _by_key(_pool())

    # Two brackets of one event, identical titles, both paired with a 25 bps market.
    assert "sibling_bracket" in pool["KXFED-25SEP-T25|0xfed1"].near_miss
    assert "sibling_bracket" in pool["KXFED-25SEP-T50|0xfed1"].near_miss
    # 120k vs 100k: titles differ only in a number.
    assert "numeric_diff" in pool["KXBTC-25DEC-120K|0xbtc1"].near_miss
    # Same proposition, end dates 1.5 days apart.
    assert "date_gap" in pool["KXCPI-25AUG|0xcpi1"].near_miss


def test_pool_finds_true_matches_the_prefilter_drops() -> None:
    miss = _by_key(_pool())["KXSENATE-26|0xsen1"]
    assert miss.band == "prefilter_miss"
    assert miss.decision == "dropped"
    assert miss.confidence >= matching_cases.PREFILTER_MISS_MIN


def test_parlays_never_enter_the_pool(tmp_path: Path) -> None:
    _write_inputs(tmp_path)
    kalshi_raw, _, _ = matching_cases._load_production_inputs(tmp_path)
    assert all(not m.ticker.startswith("KXMVE") for m in kalshi_raw)


def test_parity_break_stops_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    # Skew only the sampler's copy; production match_markets keeps the real one.
    monkeypatch.setattr(
        "marketsignalos_polymarket.matching_cases._cosine",
        lambda a, b: max(0.0, _cosine(a, b) - 0.01),
    )
    with pytest.raises(AssertionError, match="parity broken"):
        _pool()


# ── Stratified selection ─────────────────────────────────────────────────────

def test_quotas_sum_to_the_requested_size() -> None:
    assert stratum_quotas(50) == {
        "near_miss": 12, "auto": 10, "pending": 14, "below": 7, "prefilter_miss": 7,
    }
    for size in (1, 7, 13, 50, 101):
        assert sum(stratum_quotas(size).values()) == size


def _synthetic(n: int, *, band: str = "pending", event: str | None = None,
               bucket: str = "economics") -> list[Candidate]:
    out = []
    for i in range(n):
        k = NormalizedMarket("kalshi", f"K{i}", "", f"title {i}", "", "2025-09-17")
        p = NormalizedMarket("polymarket", f"0x{i:04x}", "", f"title {i}", "", "2025-09-17")
        out.append(Candidate(
            kalshi=k, polymarket=p, confidence=0.5, decision="pending", band=band,
            near_miss=(), bucket=bucket, kalshi_event=event or f"E{i}",
        ))
    return out


def test_selection_is_deterministic_and_respects_exclusions() -> None:
    pool = _synthetic(30)
    first, _ = select_sample(pool, size=10, seed=7, exclude=set())
    again, _ = select_sample(pool, size=10, seed=7, exclude=set())
    assert [c.key for c in first] == [c.key for c in again]

    excluded = {c.key for c in first}
    later, _ = select_sample(pool, size=10, seed=7, exclude=excluded)
    assert not excluded & {c.key for c in later}


def test_one_kalshi_event_cannot_dominate_the_sample() -> None:
    picked, _ = select_sample(_synthetic(6, event="KXFED-25SEP"), size=6, seed=1, exclude=set())
    assert len(picked) == matching_cases.MAX_PER_KALSHI_EVENT


def test_short_strata_spill_into_the_review_band() -> None:
    picked, stats = select_sample(_synthetic(10), size=8, seed=3, exclude=set())
    assert len(picked) == 8
    assert stats["filled"]["pending"] == 8
    assert stats["shortfall"] == 0


# ── sample_candidates end to end ─────────────────────────────────────────────

def test_sample_queues_unlabeled_pairs_with_rules_and_a_manifest(tmp_path: Path) -> None:
    data, evals = tmp_path / "data", tmp_path / "evals"
    _write_inputs(data)
    fake = FakeExchanges()
    result = sample_candidates(
        data, cases_path=evals / "cases.jsonl", queue_path=evals / "to_label.jsonl",
        size=6, seed=11, get_json=fake,
    )

    queue = load_cases(evals / "to_label.jsonl", require_labels=False)
    assert len(queue) == result.queued > 0
    assert all(case.label is None for case in queue)  # the sampler never labels
    assert all(case.polymarket.rules_status == "ok" for case in queue)
    fed = [c for c in queue if c.kalshi.market_id.startswith("KXFED")]
    for case in fed:
        # Stored bracket text wins over the live fetch: it is what production had.
        assert case.kalshi.subtitle in {"Cut of 25 bps", "Cut of 50 bps"}
        assert case.kalshi.secondary_id == "KXFED-25SEP"

    fetched = [url for url, _ in fake.calls if "/markets/" in url]
    assert len(fetched) == len(set(fetched))  # each market fetched once

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["seed"] == 11
    assert manifest["pool"]["tfidf_parity_checked"] == manifest["pool"]["production_links"]
    assert set(manifest["inputs"]) == {"kalshi_markets.jsonl", "polymarket_markets.jsonl"}

    again = sample_candidates(
        data, cases_path=evals / "cases.jsonl", queue_path=evals / "to_label.jsonl",
        size=6, seed=11, get_json=FakeExchanges(),
    )
    ids = [c.case_id for c in load_cases(evals / "to_label.jsonl", require_labels=False)]
    assert len(ids) == len(set(ids)) == result.queued + again.queued


def test_sample_writes_nothing_when_the_network_fails(tmp_path: Path) -> None:
    data, queue = tmp_path / "data", tmp_path / "evals" / "to_label.jsonl"
    _write_inputs(data)
    with pytest.raises(httpx.ConnectError):
        sample_candidates(
            data, cases_path=tmp_path / "evals" / "cases.jsonl", queue_path=queue, size=6,
            get_json=FakeExchanges(fail_with=httpx.ConnectError("offline")),
        )
    assert not queue.exists()


def test_sample_requires_both_market_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="kalshi_markets.jsonl"):
        sample_candidates(tmp_path, cases_path=tmp_path / "c.jsonl",
                          queue_path=tmp_path / "q.jsonl", get_json=FakeExchanges())


# ── Seeding from manual decisions ────────────────────────────────────────────

def _link(ticker: str, cid: str, *, status: str, matched_by: str, confidence: float) -> MarketLink:
    return MarketLink(
        kalshi_ticker=ticker, polymarket_condition_id=cid, polymarket_slug=f"slug-{cid}",
        kalshi_title=f"Kalshi {ticker}", polymarket_title=f"Poly {cid}",
        kalshi_end_date="2025-09-17T18:00:00Z", polymarket_end_date="2025-09-17T00:00:00Z",
        confidence=confidence, status=status, matched_by=matched_by,
        matched_at="2026-06-01T12:00:00Z",
    )


def _write_links(data: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    JsonlMarketLinkStore(data / "market_links.jsonl").upsert_links([
        _link("KA", "0xa", status="approved", matched_by="manual", confidence=0.62),
        _link("KB", "0xb", status="rejected", matched_by="manual", confidence=0.41),
        _link("KC", "0xc", status="pending", matched_by="auto", confidence=0.50),
        _link("KD", "0xd", status="approved", matched_by="auto", confidence=0.91),
    ])


def test_seed_imports_only_human_decisions_without_inventing_reasons(tmp_path: Path) -> None:
    data, cases_path = tmp_path / "data", tmp_path / "cases.jsonl"
    _write_links(data)
    result = seed_from_market_links(data, cases_path, get_json=FakeExchanges())

    assert (result.manual_decisions, result.added) == (2, 2)
    cases = {c.case_id: c for c in load_cases(cases_path)}
    assert set(cases) == {"KA|0xa", "KB|0xb"}
    approved, rejected = cases["KA|0xa"], cases["KB|0xb"]
    assert approved.label is not None and rejected.label is not None
    assert approved.label.same_event is True and rejected.label.same_event is False
    assert "no reason was recorded" in approved.label.reason
    assert approved.label.origin == "market_links_manual"
    assert approved.label.labeled_at == "2026-06-01T12:00:00Z"
    # Both were pending auto links before the human decided.
    assert approved.tfidf_decision == "pending" and approved.band == "pending"
    assert approved.kalshi.rules.startswith("Kalshi rules for KA")
    # Stored titles are what production matched on; live titles do not replace them.
    assert approved.kalshi.title == "Kalshi KA"


def test_seed_is_idempotent_and_reports_conflicts(tmp_path: Path) -> None:
    data, cases_path = tmp_path / "data", tmp_path / "cases.jsonl"
    _write_links(data)
    seed_from_market_links(data, cases_path, get_json=FakeExchanges())

    cases = load_cases(cases_path)
    flipped = [
        c.with_label(CaseLabel(
            same_event=False, reason="re-reviewed: different deadline", labeled_by="manual",
            labeled_at="2026-09-28T00:00:00Z", origin="sampled",
        )) if c.case_id == "KA|0xa" else c
        for c in cases
    ]
    write_cases_atomic(cases_path, flipped)

    again = seed_from_market_links(data, cases_path, get_json=FakeExchanges())
    assert again.added == 0 and again.already_present == 2
    assert again.conflicts == ["KA|0xa"]
    kept = {c.case_id: c for c in load_cases(cases_path)}["KA|0xa"]
    assert kept.label is not None and kept.label.same_event is False  # never overwritten


def test_seed_marks_rules_unavailable_when_a_market_is_gone(tmp_path: Path) -> None:
    data, cases_path = tmp_path / "data", tmp_path / "cases.jsonl"
    _write_links(data)
    result = seed_from_market_links(
        data, cases_path, get_json=FakeExchanges(missing=frozenset({"KA", "0xa"})),
    )
    assert result.rules_unavailable == 1
    case = {c.case_id: c for c in load_cases(cases_path)}["KA|0xa"]
    assert case.kalshi.rules_status == "unavailable"
    assert case.polymarket.rules_status == "unavailable"


# ── Interactive labeling ─────────────────────────────────────────────────────

def _scripted(*answers: str) -> Any:
    replies: Iterator[str] = iter(answers)

    def read(prompt: str) -> str:
        try:
            return next(replies)
        except StopIteration as exc:
            raise EOFError from exc

    return read


def _queue(tmp_path: Path) -> tuple[Path, Path]:
    data, evals = tmp_path / "data", tmp_path / "evals"
    _write_inputs(data)
    queue, cases = evals / "to_label.jsonl", evals / "cases.jsonl"
    sample_candidates(data, cases_path=cases, queue_path=queue, size=3, seed=5,
                      get_json=FakeExchanges())
    return queue, cases


def test_labeling_requires_a_reason_and_moves_cases_out_of_the_queue(tmp_path: Path) -> None:
    queue, cases = _queue(tmp_path)
    queued = load_cases(queue, require_labels=False)
    assert len(queued) == 3
    output: list[str] = []

    labeled = label_queue(
        queue, cases,
        read=_scripted("y", "", "Same proposition, threshold and date", "s",
                       "n", "Sibling bracket: 50 bps vs 25 bps"),
        write=output.append,
    )

    assert labeled == 2
    stored = load_cases(cases)
    assert [c.case_id for c in stored] == [queued[0].case_id, queued[2].case_id]
    assert stored[0].label is not None and stored[0].label.same_event is True
    assert stored[1].label is not None and stored[1].label.same_event is False
    assert all(c.label is not None and c.label.origin == "sampled" for c in stored)
    assert [c.case_id for c in load_cases(queue, require_labels=False)] == [queued[1].case_id]
    assert any("non-empty" in line for line in output)  # empty reason was refused


def test_labeling_is_resumable_and_quit_changes_nothing(tmp_path: Path) -> None:
    queue, cases = _queue(tmp_path)
    before = queue.read_bytes()
    output: list[str] = []

    assert label_queue(queue, cases, read=_scripted("r", "q"), write=output.append) == 0
    assert queue.read_bytes() == before and not cases.exists()
    first, full = "\n".join(output).split("--- 1/")[1:]
    assert " …" in first  # long rules are truncated on first display
    assert " …" not in full and len(full) > len(first)  # [r] shows them in full

    assert label_queue(queue, cases, read=_scripted("y", "ok"), write=output.append) == 1
    assert label_queue(queue, cases, read=_scripted(), write=output.append) == 0  # EOF


# ── Field verification ───────────────────────────────────────────────────────

def test_verify_fields_reports_the_rules_keys(tmp_path: Path) -> None:
    output: list[str] = []
    code = verify_fields(tmp_path, kalshi_ticker="KXFED-25SEP-T25",
                         polymarket_condition="0xfed1", get_json=FakeExchanges(),
                         write=output.append)
    assert code == 0
    assert any("rules from ['rules_primary', 'rules_secondary']" in line for line in output)
    assert any("rules from ['description', 'resolutionSource']" in line for line in output)


def test_verify_fields_fails_when_a_market_is_missing(tmp_path: Path) -> None:
    code = verify_fields(tmp_path, kalshi_ticker="GONE", polymarket_condition="0xfed1",
                         get_json=FakeExchanges(missing=frozenset({"GONE"})),
                         write=lambda line: None)
    assert code == 1
