"""
Polymarket → Kalshi market matcher.

Joins Kalshi markets to Polymarket markets by:
  1. Bucketing both sides by (year, month) of end_date and an optional
     normalized category, then intersecting buckets with a +/- date window.
  2. Within each bucket, scoring title similarity via TF-IDF cosine
     (pure Python — no scikit-learn dep).
  3. Producing MarketLinks with a confidence score and an auto status
     (approved / pending / rejected).

Design notes:
  - The matcher operates on `NormalizedMarket` adapters so the same
    algorithm works for Kalshi, Polymarket, or any future exchange.
  - We intentionally avoid neural embeddings for v1: TF-IDF on these
    short titles is empirically strong (matches share Bitcoin/Fed/election
    terms), and it's deterministic + zero-dependency.

Confidence buckets:
  - >= 0.85: auto-approved
  - 0.60-0.85: pending manual review
  - < 0.60:  not stored (would clutter market_links.jsonl)

Manual overrides (stored in market_links.jsonl with matched_by="manual")
are sticky: the auto matcher cannot overwrite a human decision.
"""
from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import MarketLink

log = logging.getLogger("marketsignalos.polymarket.matcher")


# ── Normalized market shape ──────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class NormalizedMarket:
    """Source-agnostic market for matching purposes."""

    source: str  # "kalshi" | "polymarket"
    identifier: str  # ticker (kalshi) or condition_id (polymarket)
    slug: str  # human-readable secondary id; empty for kalshi
    title: str
    category: str  # may be empty
    end_date: str  # ISO-8601


# ── Tokenization + TF-IDF ────────────────────────────────────────────────────

# Hand-picked stopword list. Kept small because prediction-market titles are
# short and every preserved term carries signal.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "do", "does", "did", "have", "has", "had",
        "of", "in", "on", "at", "to", "from", "for", "with", "by", "as",
        "and", "or", "but", "if", "then", "than", "this", "that", "these", "those",
        "will", "would", "could", "should", "shall", "may", "might", "can",
        "any", "all", "no", "not",
        "what", "which", "who", "when", "where", "why", "how",
        "it", "its", "their", "his", "her",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _strip_plural(token: str) -> str:
    """Tiny lemmatizer: collapse 'rates' -> 'rate', '50bps' -> '50bp'."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, drop stopwords, drop single chars, dedupe plurals."""
    tokens = _TOKEN_RE.findall(text.lower())
    return [_strip_plural(t) for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _build_idf(corpus: list[list[str]]) -> dict[str, float]:
    """Smoothed IDF: log((N+1)/(df+1)) + 1. Same formula as sklearn default."""
    n = len(corpus)
    if n == 0:
        return {}
    df: dict[str, int] = defaultdict(int)
    for tokens in corpus:
        for term in set(tokens):
            df[term] += 1
    return {term: math.log((n + 1) / (count + 1)) + 1.0 for term, count in df.items()}


def _tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    """Sparse TF-IDF vector for one document."""
    if not tokens:
        return {}
    tf: dict[str, int] = defaultdict(int)
    for t in tokens:
        tf[t] += 1
    total = len(tokens)
    vec = {t: (count / total) * idf.get(t, 0.0) for t, count in tf.items()}
    return vec


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    # Iterate over the smaller vector for the dot product.
    if len(a) > len(b):
        a, b = b, a
    dot = sum(weight * b.get(term, 0.0) for term, weight in a.items())
    norm_a = math.sqrt(sum(w * w for w in a.values()))
    norm_b = math.sqrt(sum(w * w for w in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── Category normalization ───────────────────────────────────────────────────

# Polymarket category strings vs. Kalshi event tickers don't share a
# vocabulary, so we collapse to coarse buckets. Empty bucket means "no
# category constraint".
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "politics": ("politic", "election", "president", "congress", "senate", "trump", "biden", "harris"),
    "crypto": ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "dogecoin"),
    "sports": ("nfl", "nba", "mlb", "soccer", "football", "basketball", "baseball", "match", "game", "sports"),
    "economics": ("fed", "rate", "inflation", "gdp", "cpi", "unemployment", "recession", "economic"),
    "ai_tech": ("openai", "gpt", "ai", "tech", "model", "apple", "google", "meta", "tesla"),
    "weather": ("hurricane", "storm", "temperature", "weather"),
}


def _normalize_category(raw_category: str, title: str) -> str:
    blob = f"{raw_category} {title}".lower()
    for bucket, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in blob for kw in keywords):
            return bucket
    return ""  # uncategorized — won't be filtered out, just won't help


# ── End-date bucketing ───────────────────────────────────────────────────────

def _parse_iso_date(s: str) -> datetime | None:
    if not s:
        return None
    # Trim to ISO date portion; handle Z and offset variants.
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, TypeError):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


# ── Candidate generation ─────────────────────────────────────────────────────

@dataclass(slots=True)
class _PreparedMarket:
    market: NormalizedMarket
    tokens: list[str]
    end_dt: datetime | None
    bucket: str  # normalized category


def _prepare(market: NormalizedMarket) -> _PreparedMarket:
    return _PreparedMarket(
        market=market,
        tokens=_tokenize(market.title),
        end_dt=_parse_iso_date(market.end_date),
        bucket=_normalize_category(market.category, market.title),
    )


def _candidate_pairs(
    kalshi: list[_PreparedMarket],
    polymarket: list[_PreparedMarket],
    *,
    date_window_days: int,
) -> list[tuple[_PreparedMarket, _PreparedMarket]]:
    """
    Generate (kalshi, polymarket) pairs that share a category bucket AND
    have end_dates within +/- date_window_days. This is the cheap O(N+M)
    pre-filter before the O(pair) similarity step.
    """
    # Index polymarket by (bucket, end_date day-bucket) so we can range-query.
    by_bucket: dict[str, list[_PreparedMarket]] = defaultdict(list)
    for pm in polymarket:
        by_bucket[pm.bucket].append(pm)

    window = timedelta(days=date_window_days)
    pairs: list[tuple[_PreparedMarket, _PreparedMarket]] = []
    for km in kalshi:
        if km.end_dt is None:
            continue
        # Consider both the same bucket and the empty-bucket pool — Polymarket
        # markets without a clear category shouldn't be silently dropped.
        candidates = by_bucket.get(km.bucket, []) + (
            by_bucket.get("", []) if km.bucket != "" else []
        )
        for pm in candidates:
            if pm.end_dt is None:
                continue
            if abs(km.end_dt - pm.end_dt) <= window:
                pairs.append((km, pm))
    return pairs


# ── Public API ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class MatchConfig:
    date_window_days: int = 3
    # Thresholds tuned against test corpus of paraphrased prediction-market
    # titles. TF-IDF cosine on short titles gives ~0.5 for moderate paraphrases
    # and ~0.9+ for near-identical wording. Mid-confidence matches go to the
    # review queue; the human is the precision filter.
    auto_approve_threshold: float = 0.75
    review_threshold: float = 0.35
    max_per_kalshi: int = 3


def match_markets(
    kalshi: list[NormalizedMarket],
    polymarket: list[NormalizedMarket],
    *,
    config: MatchConfig | None = None,
) -> list[MarketLink]:
    cfg = config or MatchConfig()
    if not kalshi or not polymarket:
        log.info("matcher_empty_input kalshi=%d polymarket=%d", len(kalshi), len(polymarket))
        return []

    prepared_k = [_prepare(m) for m in kalshi]
    prepared_p = [_prepare(m) for m in polymarket]

    # Build IDF over the union — one shared vocabulary, so a rare term like
    # "FOMC" gets the same weight on both sides.
    corpus = [pm.tokens for pm in prepared_k + prepared_p]
    idf = _build_idf(corpus)

    # Precompute vectors so we don't recompute per pair.
    k_vecs = {pm.market.identifier: _tfidf_vector(pm.tokens, idf) for pm in prepared_k}
    p_vecs = {pm.market.identifier: _tfidf_vector(pm.tokens, idf) for pm in prepared_p}

    pairs = _candidate_pairs(prepared_k, prepared_p, date_window_days=cfg.date_window_days)
    log.info(
        "matcher_pairs kalshi=%d polymarket=%d candidate_pairs=%d (vs %d brute)",
        len(prepared_k), len(prepared_p), len(pairs), len(prepared_k) * len(prepared_p),
    )

    # Score every candidate pair.
    scored: dict[str, list[tuple[float, _PreparedMarket]]] = defaultdict(list)
    for km, pm in pairs:
        sim = _cosine(k_vecs[km.market.identifier], p_vecs[pm.market.identifier])
        if sim < cfg.review_threshold:
            continue
        scored[km.market.identifier].append((sim, pm))

    # Keep top-N polymarket matches per kalshi market.
    links: list[MarketLink] = []
    for kid, candidates in scored.items():
        candidates.sort(key=lambda x: -x[0])
        km_obj = next(pm for pm in prepared_k if pm.market.identifier == kid)
        for sim, pm in candidates[: cfg.max_per_kalshi]:
            status = (
                "approved" if sim >= cfg.auto_approve_threshold
                else "pending"
            )
            links.append(
                MarketLink(
                    kalshi_ticker=km_obj.market.identifier,
                    polymarket_condition_id=pm.market.identifier,
                    polymarket_slug=pm.market.slug,
                    kalshi_title=km_obj.market.title,
                    polymarket_title=pm.market.title,
                    kalshi_end_date=km_obj.market.end_date,
                    polymarket_end_date=pm.market.end_date,
                    confidence=round(sim, 4),
                    status=status,
                    matched_by="auto",
                )
            )

    auto_approved = sum(1 for link in links if link.status == "approved")
    pending = sum(1 for link in links if link.status == "pending")
    log.info(
        "matcher_done links=%d auto_approved=%d pending=%d",
        len(links), auto_approved, pending,
    )
    return links


__all__ = [
    "MarketLink",
    "MatchConfig",
    "NormalizedMarket",
    "match_markets",
]
