"""
Build the market-matching eval set: seed from manual decisions, sample
unlabeled candidate pairs for a human, and record hand labels.

Nothing here writes a label on its own. ``seed_from_market_links`` converts
decisions a human already made in ``review-matches``; ``sample_candidates``
queues pairs with ``label: null``; only ``label_queue`` records new labels, one
human keystroke at a time. No model is called.

The sampler reuses the production loaders, normalizers, parlay filter and
matcher, then asserts that its own TF-IDF scores equal the production links'
confidences. If the two ever drift apart, sampling stops instead of building an
eval for a different matcher than the one in production.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import sys
import textwrap
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .kalshi_markets_fetch import KalshiMarket, load_kalshi_markets_jsonl
from .market_matcher import (
    MatchConfig,
    NormalizedMarket,
    _build_idf,
    _candidate_pairs,
    _cosine,
    _prepare,
    _PreparedMarket,
    _tfidf_vector,
    match_markets,
)
from .market_rules import (
    FetchedMarket,
    GetJson,
    fetch_kalshi_market,
    fetch_polymarket_market,
    http_get_json,
)
from .matching_eval import (
    CaseFileError,
    CaseLabel,
    CaseMarket,
    MatchCase,
    load_cases,
    make_case_id,
    validate_reason,
    write_cases_atomic,
)
from .models import MarketLink
from .storage import JsonlMarketLinkStore

log = logging.getLogger("marketsignalos.polymarket.matching_cases")

# Pairs scoring below this are too dissimilar to teach anything about the
# review band; above it and under review_threshold they are near-threshold drops.
BELOW_BAND_FLOOR = 0.20
# Title similarity needed before a pair outside the category/date pre-filter is
# worth a label: the question is whether the pre-filter dropped a true match.
PREFILTER_MISS_MIN = 0.50
# A token appearing in more than this share of markets is too common to block on.
BLOCKING_MAX_DF_SHARE = 0.02
BLOCKING_MIN_SHARED_TOKENS = 2
MAX_BLOCKED_COMPARISONS = 2_000_000
DATE_GAP_DAYS = 1.0
MAX_PER_KALSHI_EVENT = 2

# Share of the sample per stratum. Near misses come first because they are the
# false-positive shapes a precision-first matcher most needs to be tested on.
STRATUM_WEIGHTS: dict[str, float] = {
    "near_miss": 0.24, "auto": 0.20, "pending": 0.28, "below": 0.14, "prefilter_miss": 0.14,
}
# Where unfilled quota goes when a stratum runs out of pairs.
SPILLOVER_ORDER = ("pending", "auto", "near_miss", "below", "prefilter_miss")


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _stdout(text: str) -> None:
    sys.stdout.write(text + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _decision_for(confidence: float, config: MatchConfig) -> str:
    if confidence >= config.auto_approve_threshold:
        return "approved"
    if confidence >= config.review_threshold:
        return "pending"
    return "dropped"


def _band_for(confidence: float, config: MatchConfig) -> str:
    if confidence >= config.auto_approve_threshold:
        return "auto"
    if confidence >= config.review_threshold:
        return "pending"
    return "below"


def _case_market(
    exchange: str, market_id: str, *, title: str, end_date: str, subtitle: str,
    secondary_id: str, fetched: FetchedMarket | None,
) -> CaseMarket:
    """Titles and dates come from the stored records production matched on; rules
    (and any fields the stored record lacks) come from the live fetch."""
    if fetched is None:
        return CaseMarket(
            exchange=exchange, market_id=market_id, title=title, end_date=end_date,
            rules="", rules_status="unavailable", rules_fields=(),
            rules_fetched_at=_utcnow_iso(), secondary_id=secondary_id, subtitle=subtitle,
        )
    return CaseMarket(
        exchange=exchange, market_id=market_id,
        title=title or fetched.title,
        end_date=end_date or fetched.end_date,
        rules=fetched.rules,
        rules_status="ok" if fetched.rules.strip() else "unavailable",
        rules_fields=fetched.rules_fields,
        rules_fetched_at=fetched.fetched_at,
        secondary_id=secondary_id or fetched.secondary_id,
        subtitle=subtitle or fetched.subtitle,
    )


class _RulesCache:
    """Fetch each market's rules once per command, even if it appears in many pairs."""

    def __init__(self, get_json: GetJson) -> None:
        self._get_json = get_json
        self._kalshi: dict[str, FetchedMarket | None] = {}
        self._poly: dict[str, FetchedMarket | None] = {}

    def kalshi(self, ticker: str) -> FetchedMarket | None:
        if ticker not in self._kalshi:
            self._kalshi[ticker] = fetch_kalshi_market(ticker, get_json=self._get_json)
        return self._kalshi[ticker]

    def polymarket(self, condition_id: str) -> FetchedMarket | None:
        key = condition_id.lower()
        if key not in self._poly:
            self._poly[key] = fetch_polymarket_market(condition_id, get_json=self._get_json)
        return self._poly[key]


# ── Seed from manual decisions ───────────────────────────────────────────────

@dataclass(slots=True)
class SeedResult:
    manual_decisions: int = 0
    added: int = 0
    already_present: int = 0
    conflicts: list[str] = field(default_factory=list)
    rules_unavailable: int = 0


def seed_from_market_links(
    data_dir: Path, cases_path: Path, *, get_json: GetJson | None = None,
    config: MatchConfig | None = None,
) -> SeedResult:
    """Append every manual approve/reject in market_links.jsonl to the case file.

    review-matches recorded only y/n, never a reason, so the imported reason says
    exactly that instead of inventing one. A case already in the file is never
    overwritten; if its label disagrees with market_links.jsonl it is reported
    as a conflict for the human to resolve.
    """
    cfg = config or MatchConfig()
    links = JsonlMarketLinkStore(data_dir / "market_links.jsonl").load_links()
    manual = [
        link for link in links
        if link.matched_by == "manual" and link.status in {"approved", "rejected"}
    ]
    existing = load_cases(cases_path)
    by_id = {case.case_id: case for case in existing}
    result = SeedResult(manual_decisions=len(manual))
    rules = _RulesCache(get_json or http_get_json())

    new_cases: list[MatchCase] = []
    for link in sorted(manual, key=lambda x: (x.kalshi_ticker, x.polymarket_condition_id)):
        case_id = make_case_id(link.kalshi_ticker, link.polymarket_condition_id)
        same_event = link.status == "approved"
        if case_id in by_id:
            result.already_present += 1
            recorded = by_id[case_id].label
            if recorded is not None and recorded.same_event != same_event:
                result.conflicts.append(case_id)
            continue
        new_cases.append(_case_from_link(link, same_event, rules, cfg))
        by_id[case_id] = new_cases[-1]

    result.added = len(new_cases)
    result.rules_unavailable = sum(
        1 for case in new_cases
        if "unavailable" in (case.kalshi.rules_status, case.polymarket.rules_status)
    )
    if new_cases:
        write_cases_atomic(cases_path, [*existing, *new_cases])
    for case_id in result.conflicts:
        log.warning("seed conflict: %s is labeled differently in %s; left unchanged",
                    case_id, cases_path)
    log.info(
        "seed manual_decisions=%d added=%d already_present=%d conflicts=%d rules_unavailable=%d",
        result.manual_decisions, result.added, result.already_present,
        len(result.conflicts), result.rules_unavailable,
    )
    return result


def _case_from_link(
    link: MarketLink, same_event: bool, rules: _RulesCache, config: MatchConfig,
) -> MatchCase:
    kalshi_fetch = rules.kalshi(link.kalshi_ticker)
    poly_fetch = rules.polymarket(link.polymarket_condition_id)
    confidence = min(1.0, max(0.0, float(link.confidence)))
    return MatchCase(
        case_id=make_case_id(link.kalshi_ticker, link.polymarket_condition_id),
        kalshi=_case_market(
            "kalshi", link.kalshi_ticker, title=link.kalshi_title,
            end_date=link.kalshi_end_date, subtitle="", secondary_id="", fetched=kalshi_fetch,
        ),
        polymarket=_case_market(
            "polymarket", link.polymarket_condition_id, title=link.polymarket_title,
            end_date=link.polymarket_end_date, subtitle="", secondary_id=link.polymarket_slug,
            fetched=poly_fetch,
        ),
        tfidf_confidence=confidence,
        # review-matches only ever showed pending auto links, so this reconstructs
        # the auto decision the human then overrode.
        tfidf_decision=_decision_for(confidence, config),
        band=_band_for(confidence, config),
        label=CaseLabel(
            same_event=same_event,
            reason=(
                f"Imported review-matches decision ({link.status}); "
                "no reason was recorded at review time."
            ),
            labeled_by="manual",
            labeled_at=link.matched_at,
            origin="market_links_manual",
        ),
    )


# ── Candidate pool ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Candidate:
    kalshi: NormalizedMarket
    polymarket: NormalizedMarket
    confidence: float
    decision: str  # production decision: approved | pending | dropped
    band: str
    near_miss: tuple[str, ...]
    bucket: str
    kalshi_event: str

    @property
    def key(self) -> str:
        return make_case_id(self.kalshi.identifier, self.polymarket.identifier)


@dataclass(slots=True)
class CandidatePool:
    candidates: list[Candidate]
    production_links: int
    parity_checked: int
    blocked_comparisons: int
    blocking_capped: bool


def _load_production_inputs(
    data_dir: Path,
) -> tuple[list[KalshiMarket], list[NormalizedMarket], list[NormalizedMarket]]:
    from .runner import (
        _is_kalshi_parlay,
        _kalshi_to_normalized,
        _load_market_records,
        _polymarket_to_normalized,
    )

    kalshi_raw = [
        m for m in load_kalshi_markets_jsonl(data_dir / "kalshi_markets.jsonl")
        if not _is_kalshi_parlay(m.ticker)
    ]
    poly_raw = _load_market_records(data_dir / "polymarket_markets.jsonl")
    if not kalshi_raw or not poly_raw:
        raise ValueError(
            f"need kalshi_markets.jsonl and polymarket_markets.jsonl in {data_dir}; run "
            "'fetch-kalshi-markets' and 'markets' (or the pipeline with --include-kalshi) first"
        )
    return (
        kalshi_raw,
        [_kalshi_to_normalized(m) for m in kalshi_raw],
        [_polymarket_to_normalized(m) for m in poly_raw],
    )


def _numeric_diff(a: list[str], b: list[str]) -> bool:
    """True when the titles differ only in tokens containing digits (25bp vs 50bp)."""
    diff = set(a) ^ set(b)
    return bool(diff) and all(any(ch.isdigit() for ch in token) for token in diff)


def _blocked_pairs(
    kalshi: list[_PreparedMarket], polymarket: list[_PreparedMarket],
) -> tuple[set[tuple[str, str]], int, bool]:
    """Pairs sharing at least two informative title tokens, ignoring the pre-filter."""
    postings: dict[str, list[int]] = defaultdict(list)
    for index, pm in enumerate(polymarket):
        for token in set(pm.tokens):
            postings[token].append(index)
    max_df = max(50, int(BLOCKING_MAX_DF_SHARE * (len(kalshi) + len(polymarket))))
    pairs: set[tuple[str, str]] = set()
    comparisons = 0
    for km in kalshi:
        shared: dict[int, int] = defaultdict(int)
        for token in set(km.tokens):
            hits = postings.get(token, [])
            if len(hits) > max_df:
                continue
            for index in hits:
                shared[index] += 1
        for index, count in shared.items():
            if count >= BLOCKING_MIN_SHARED_TOKENS:
                pairs.add((km.market.identifier, polymarket[index].market.identifier))
                comparisons += 1
                if comparisons >= MAX_BLOCKED_COMPARISONS:
                    return pairs, comparisons, True
    return pairs, comparisons, False


def build_candidate_pool(
    kalshi_raw: list[KalshiMarket],
    kalshi: list[NormalizedMarket],
    polymarket: list[NormalizedMarket],
    *,
    config: MatchConfig | None = None,
) -> CandidatePool:
    cfg = config or MatchConfig()
    production = {
        make_case_id(link.kalshi_ticker, link.polymarket_condition_id): link
        for link in match_markets(kalshi, polymarket, config=cfg)
    }
    prepared_k = [_prepare(m) for m in kalshi]
    prepared_p = [_prepare(m) for m in polymarket]
    # Same union corpus as match_markets, so the IDF weights are identical.
    idf = _build_idf([pm.tokens for pm in prepared_k + prepared_p])
    k_vec = {pm.market.identifier: _tfidf_vector(pm.tokens, idf) for pm in prepared_k}
    p_vec = {pm.market.identifier: _tfidf_vector(pm.tokens, idf) for pm in prepared_p}
    k_prep = {pm.market.identifier: pm for pm in prepared_k}
    p_prep = {pm.market.identifier: pm for pm in prepared_p}
    event_of = {m.ticker: m.event_ticker or m.ticker for m in kalshi_raw}

    scored: dict[str, tuple[_PreparedMarket, _PreparedMarket, float, bool]] = {}
    for km, pm in _candidate_pairs(prepared_k, prepared_p, date_window_days=cfg.date_window_days):
        sim = _cosine(k_vec[km.market.identifier], p_vec[pm.market.identifier])
        if sim < BELOW_BAND_FLOOR:
            continue
        key = make_case_id(km.market.identifier, pm.market.identifier)
        if key not in scored or sim > scored[key][2]:
            scored[key] = (km, pm, sim, True)

    parity_checked = 0
    for key, link in production.items():
        if key not in scored or abs(round(scored[key][2], 4) - link.confidence) > 1e-9:
            raise AssertionError(
                f"TF-IDF parity broken for {key}: production {link.confidence}, "
                f"sampler {scored[key][2] if key in scored else 'missing'}"
            )
        parity_checked += 1

    blocked, comparisons, capped = _blocked_pairs(prepared_k, prepared_p)
    if capped:
        log.warning("prefilter-miss blocking capped at %d comparisons", comparisons)
    for kalshi_id, poly_id in blocked:
        key = make_case_id(kalshi_id, poly_id)
        if key in scored:
            continue
        sim = _cosine(k_vec[kalshi_id], p_vec[poly_id])
        if sim >= PREFILTER_MISS_MIN:
            scored[key] = (k_prep[kalshi_id], p_prep[poly_id], sim, False)

    # Kalshi brackets of one event share a title; several pairing with the same
    # Polymarket market is the classic sibling-bracket false positive.
    siblings: dict[tuple[str, str], set[str]] = defaultdict(set)
    for km, pm, sim, in_filter in scored.values():
        if in_filter and sim >= cfg.review_threshold:
            siblings[(event_of.get(km.market.identifier, ""), pm.market.identifier)].add(
                km.market.identifier
            )

    candidates: list[Candidate] = []
    for key, (km, pm, sim, in_filter) in scored.items():
        kinds: list[str] = []
        if sim >= cfg.review_threshold:
            if _numeric_diff(km.tokens, pm.tokens):
                kinds.append("numeric_diff")
            event = event_of.get(km.market.identifier, "")
            if in_filter and len(siblings[(event, pm.market.identifier)]) > 1:
                kinds.append("sibling_bracket")
        if (in_filter and sim >= PREFILTER_MISS_MIN and km.end_dt and pm.end_dt
                and abs((km.end_dt - pm.end_dt).total_seconds()) > DATE_GAP_DAYS * 86400):
            kinds.append("date_gap")
        produced = production.get(key)
        candidates.append(Candidate(
            kalshi=km.market, polymarket=pm.market, confidence=round(sim, 4),
            decision=produced.status if produced else "dropped",
            band=_band_for(sim, cfg) if in_filter else "prefilter_miss",
            near_miss=tuple(kinds), bucket=km.bucket or pm.bucket,
            kalshi_event=event_of.get(km.market.identifier, km.market.identifier),
        ))
    candidates.sort(key=lambda c: c.key)
    return CandidatePool(
        candidates=candidates, production_links=len(production),
        parity_checked=parity_checked, blocked_comparisons=comparisons, blocking_capped=capped,
    )


# ── Stratified sampling ──────────────────────────────────────────────────────

def stratum_quotas(size: int) -> dict[str, int]:
    quotas = {name: int(size * weight) for name, weight in STRATUM_WEIGHTS.items()}
    quotas["pending"] += size - sum(quotas.values())
    return quotas


def _in_stratum(candidate: Candidate, stratum: str) -> bool:
    return bool(candidate.near_miss) if stratum == "near_miss" else candidate.band == stratum


def select_sample(
    pool: list[Candidate], *, size: int, seed: int, exclude: set[str],
) -> tuple[list[Candidate], dict[str, Any]]:
    """Deterministic stratified draw: same pool + seed + exclusions -> same sample."""
    rng = random.Random(seed)
    available = [c for c in pool if c.key not in exclude]
    quotas = stratum_quotas(size)
    picked: list[Candidate] = []
    picked_keys: set[str] = set()
    per_event: dict[str, int] = defaultdict(int)
    stats: dict[str, Any] = {
        "quotas": dict(quotas),
        "available": {s: sum(1 for c in available if _in_stratum(c, s)) for s in quotas},
        "filled": {s: 0 for s in quotas},
    }

    def draw(stratum: str, want: int) -> int:
        groups: dict[str, list[Candidate]] = defaultdict(list)
        for c in available:
            if c.key not in picked_keys and _in_stratum(c, stratum):
                groups[c.bucket].append(c)
        for members in groups.values():
            rng.shuffle(members)
        order = sorted(groups)
        got = 0
        while got < want and any(groups[b] for b in order):
            for bucket in order:
                if got >= want or not groups[bucket]:
                    continue
                c = groups[bucket].pop()
                if per_event[c.kalshi_event] >= MAX_PER_KALSHI_EVENT:
                    continue
                picked.append(c)
                picked_keys.add(c.key)
                per_event[c.kalshi_event] += 1
                got += 1
        return got

    shortfall = 0
    for stratum in quotas:
        got = draw(stratum, quotas[stratum])
        stats["filled"][stratum] += got
        shortfall += quotas[stratum] - got
    for stratum in SPILLOVER_ORDER:
        if shortfall <= 0:
            break
        got = draw(stratum, shortfall)
        stats["filled"][stratum] += got
        shortfall -= got
    stats["shortfall"] = shortfall
    return picked, stats


@dataclass(slots=True)
class SampleResult:
    queued: int
    manifest_path: Path
    stats: dict[str, Any]


def sample_candidates(
    data_dir: Path, *, cases_path: Path, queue_path: Path, size: int = 50,
    seed: int = 20260928, get_json: GetJson | None = None, config: MatchConfig | None = None,
) -> SampleResult:
    """Queue ``size`` unlabeled pairs for hand labeling. Never writes a label."""
    if size < 1:
        raise ValueError("size must be positive")
    cfg = config or MatchConfig()
    kalshi_raw, kalshi, polymarket = _load_production_inputs(data_dir)
    pool = build_candidate_pool(kalshi_raw, kalshi, polymarket, config=cfg)
    queued = load_cases(queue_path, require_labels=False)
    exclude = {c.case_id for c in load_cases(cases_path)} | {c.case_id for c in queued}
    picked, stats = select_sample(pool.candidates, size=size, seed=seed, exclude=exclude)

    # Fetch every rules text before writing anything: a network failure midway
    # leaves the queue untouched rather than half-built.
    rules = _RulesCache(get_json or http_get_json())
    raw_by_ticker = {m.ticker: m for m in kalshi_raw}
    new_rows = [_case_from_candidate(c, raw_by_ticker, rules) for c in picked]
    write_cases_atomic(queue_path, [*queued, *new_rows])

    manifest = {
        "schema_version": 1, "generated_at": _utcnow_iso(), "seed": seed, "size": size,
        "queued": len(new_rows), "selection": stats,
        "pool": {
            "candidates": len(pool.candidates), "production_links": pool.production_links,
            "tfidf_parity_checked": pool.parity_checked,
            "blocked_comparisons": pool.blocked_comparisons,
            "blocking_capped": pool.blocking_capped,
        },
        "matcher_config": {
            "auto_approve_threshold": cfg.auto_approve_threshold,
            "review_threshold": cfg.review_threshold,
            "date_window_days": cfg.date_window_days, "max_per_kalshi": cfg.max_per_kalshi,
        },
        "inputs": {
            name: _sha256(data_dir / name)
            for name in ("kalshi_markets.jsonl", "polymarket_markets.jsonl")
        },
    }
    manifest_path = queue_path.with_name(queue_path.stem + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log.info("sample queued=%d shortfall=%d queue=%s", len(new_rows), stats["shortfall"],
             queue_path)
    return SampleResult(queued=len(new_rows), manifest_path=manifest_path, stats=stats)


def _case_from_candidate(
    c: Candidate, raw_by_ticker: dict[str, KalshiMarket], rules: _RulesCache,
) -> MatchCase:
    raw = raw_by_ticker.get(c.kalshi.identifier)
    return MatchCase(
        case_id=c.key,
        kalshi=_case_market(
            "kalshi", c.kalshi.identifier, title=c.kalshi.title, end_date=c.kalshi.end_date,
            subtitle=(raw.yes_sub_title or raw.subtitle) if raw else "",
            secondary_id=raw.event_ticker if raw else "",
            fetched=rules.kalshi(c.kalshi.identifier),
        ),
        polymarket=_case_market(
            "polymarket", c.polymarket.identifier, title=c.polymarket.title,
            end_date=c.polymarket.end_date, subtitle="", secondary_id=c.polymarket.slug,
            fetched=rules.polymarket(c.polymarket.identifier),
        ),
        tfidf_confidence=c.confidence, tfidf_decision=c.decision, band=c.band,
        near_miss=c.near_miss, label=None,
    )


# ── Interactive labeling ─────────────────────────────────────────────────────

_PROMPT = "Same event? YES on both resolves identically [y/n] · [r] full rules · [s]kip · [q]uit: "


def _show_market(write: Callable[[str], None], market: CaseMarket, *, full: bool) -> None:
    ident = market.market_id + (f"  ({market.secondary_id})" if market.secondary_id else "")
    write(f"{market.exchange.upper():<10} {ident}")
    write(f"  Title   : {market.title}")
    if market.subtitle:
        write(f"  Bracket : {market.subtitle}")
    write(f"  Ends    : {market.end_date or '(unknown)'}")
    if market.rules_status != "ok":
        write("  Rules   : (unavailable)")
        return
    text = market.rules if full or len(market.rules) <= 500 else market.rules[:500] + " …"
    for i, line in enumerate(textwrap.wrap(text, 96) or [""]):
        write(("  Rules   : " if i == 0 else "            ") + line)


def label_queue(
    queue_path: Path, cases_path: Path, *,
    read: Callable[[str], str] = input, write: Callable[[str], None] = _stdout,
) -> int:
    """Walk the unlabeled queue; every y/n needs a one-line reason. Resumable.

    Each label is written to cases.jsonl and removed from the queue immediately,
    so quitting or crashing loses at most the pair on screen.
    """
    queue = load_cases(queue_path, require_labels=False)
    cases = load_cases(cases_path)
    known = {c.case_id for c in cases}
    remaining = [c for c in queue if c.label is None and c.case_id not in known]
    labeled = 0
    write(f"{len(remaining)} pair(s) to label. Definition: docs/llm-judge.md#what-same-event-means")
    for index, case in enumerate(list(remaining), 1):
        full = False
        while True:
            write("")
            write(f"--- {index}/{len(remaining)}  band={case.band}  "
                  f"near_miss={','.join(case.near_miss) or '-'}  "
                  f"tfidf={case.tfidf_confidence:.3f} ({case.tfidf_decision}) ---")
            _show_market(write, case.kalshi, full=full)
            _show_market(write, case.polymarket, full=full)
            try:
                choice = read(_PROMPT).strip().lower()
            except (EOFError, KeyboardInterrupt):
                write("\nStopped; progress is saved.")
                return labeled
            if choice == "r":
                full = True
                continue
            break
        if choice == "q":
            break
        if choice not in {"y", "n"}:
            continue  # skip: stays in the queue
        reason = _read_reason(read, write)
        if reason is None:
            write("Stopped; progress is saved.")
            return labeled
        cases.append(case.with_label(CaseLabel(
            same_event=choice == "y", reason=reason, labeled_by="manual",
            labeled_at=_utcnow_iso(), origin="sampled",
        )))
        write_cases_atomic(cases_path, cases)
        queue = [q for q in queue if q.case_id != case.case_id]
        write_cases_atomic(queue_path, queue)
        labeled += 1
    return labeled


def _read_reason(read: Callable[[str], str], write: Callable[[str], None]) -> str | None:
    while True:
        try:
            raw = read("One-line reason: ")
        except (EOFError, KeyboardInterrupt):
            return None
        try:
            return validate_reason(raw)
        except CaseFileError as exc:
            write(f"  {exc}")


# ── Field verification ───────────────────────────────────────────────────────

def verify_fields(
    data_dir: Path, *, kalshi_ticker: str | None = None, polymarket_condition: str | None = None,
    get_json: GetJson | None = None, write: Callable[[str], None] = _stdout,
) -> int:
    """Fetch one live market per exchange and show which rules fields it carries.

    Exits non-zero if either market is missing; a payload without the expected
    rules keys raises RulesFieldError with the keys it did see.
    """
    if kalshi_ticker is None or polymarket_condition is None:
        kalshi_raw, _, polymarket = _load_production_inputs(data_dir)
        kalshi_ticker = kalshi_ticker or kalshi_raw[0].ticker
        polymarket_condition = polymarket_condition or polymarket[0].identifier
    fetch = get_json or http_get_json()
    status = 0
    for label, fetched in (
        ("Kalshi", fetch_kalshi_market(kalshi_ticker, get_json=fetch)),
        ("Polymarket", fetch_polymarket_market(polymarket_condition, get_json=fetch)),
    ):
        if fetched is None:
            write(f"{label}: market not found")
            status = 1
            continue
        write(f"{label} {fetched.market_id}: rules from {list(fetched.rules_fields)}, "
              f"{len(fetched.rules)} chars")
        write(f"  payload keys: {list(fetched.payload_keys)}")
        write(f"  rules start: {fetched.rules[:160]!r}")
    return status
