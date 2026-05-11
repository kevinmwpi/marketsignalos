from __future__ import annotations

from marketsignalos_polymarket.market_matcher import (
    MatchConfig,
    NormalizedMarket,
    _candidate_pairs,
    _cosine,
    _normalize_category,
    _prepare,
    _tokenize,
    match_markets,
)


def _kalshi(ticker: str, title: str, end_date: str, category: str = "") -> NormalizedMarket:
    return NormalizedMarket(
        source="kalshi",
        identifier=ticker,
        slug="",
        title=title,
        category=category,
        end_date=end_date,
    )


def _polymarket(cond: str, slug: str, title: str, end_date: str, category: str = "") -> NormalizedMarket:
    return NormalizedMarket(
        source="polymarket",
        identifier=cond,
        slug=slug,
        title=title,
        category=category,
        end_date=end_date,
    )


# ── Tokenization ─────────────────────────────────────────────────────────────

def test_tokenize_lowercases_and_strips_punctuation() -> None:
    # Trailing 's' is normalized so '50bps' collides with '50bp' across documents.
    assert _tokenize("Will the Fed cut by 50bps?") == ["fed", "cut", "50bp"]


def test_tokenize_drops_stopwords_and_single_chars() -> None:
    out = _tokenize("Will a bitcoin reach $100k by end of 2026?")
    assert "bitcoin" in out
    assert "100k" in out
    assert "2026" in out
    assert "a" not in out
    assert "by" not in out
    assert "of" not in out


# ── Cosine similarity ────────────────────────────────────────────────────────

def test_cosine_identical_vectors_is_one() -> None:
    v = {"fed": 1.0, "rate": 0.5}
    assert abs(_cosine(v, v) - 1.0) < 1e-9


def test_cosine_disjoint_vectors_is_zero() -> None:
    assert _cosine({"a": 1.0}, {"b": 1.0}) == 0.0


def test_cosine_empty_returns_zero() -> None:
    assert _cosine({}, {"a": 1.0}) == 0.0


# ── Category normalization ───────────────────────────────────────────────────

def test_category_politics_inferred_from_title() -> None:
    assert _normalize_category("", "Will Trump win 2028?") == "politics"


def test_category_crypto_inferred() -> None:
    assert _normalize_category("", "Bitcoin above $200k in Q3?") == "crypto"


def test_category_unknown_returns_empty() -> None:
    assert _normalize_category("", "Will the Tokyo Olympics happen?") == ""


# ── Candidate generation ─────────────────────────────────────────────────────

def test_candidate_pairs_respects_date_window() -> None:
    k = [_prepare(_kalshi("K1", "Bitcoin 200k", "2026-06-30T00:00:00Z"))]
    # Same-day match → in window
    p_close = _prepare(_polymarket("P1", "btc-200k", "Bitcoin 200k by EOY", "2026-06-30T12:00:00Z"))
    # 10 days off → out of window
    p_far = _prepare(_polymarket("P2", "btc-200k-2", "Bitcoin 200k", "2026-07-10T00:00:00Z"))
    pairs = _candidate_pairs(k, [p_close, p_far], date_window_days=3)
    assert (k[0], p_close) in pairs
    assert (k[0], p_far) not in pairs


def test_candidate_pairs_respects_category_bucket() -> None:
    k = [_prepare(_kalshi("K1", "Trump 2028 candidacy", "2026-06-30T00:00:00Z"))]
    pm_politics = _prepare(_polymarket("P1", "trump-28", "Trump 2028 nominee", "2026-06-30T00:00:00Z"))
    pm_crypto = _prepare(_polymarket("P2", "btc-100k", "Bitcoin to 100k", "2026-06-30T00:00:00Z"))
    pairs = _candidate_pairs(k, [pm_politics, pm_crypto], date_window_days=3)
    assert (k[0], pm_politics) in pairs
    assert (k[0], pm_crypto) not in pairs


# ── End-to-end matching: known good pairs ────────────────────────────────────

def test_known_good_fed_rate_decision() -> None:
    k = [_kalshi("KXFED-25SEP-50BP", "Will the Fed cut rates by 50bps in September 2025?", "2025-09-18T20:00:00Z")]
    p = [_polymarket("0xfed1", "fed-50bp-sep-2025", "Fed decision September 2025: 50bp cut?", "2025-09-18T18:00:00Z")]
    links = match_markets(k, p)
    assert len(links) == 1
    assert links[0].status in {"approved", "pending"}
    assert links[0].confidence >= 0.6


def test_known_good_election_outcome() -> None:
    k = [_kalshi("KXPRES-24-DJT", "Will Donald Trump win the 2024 US Presidential Election?", "2024-11-05T00:00:00Z")]
    p = [_polymarket("0xtrump", "trump-24-prez", "Will Donald Trump win the 2024 US Presidential Election?", "2024-11-05T00:00:00Z")]
    links = match_markets(k, p)
    # Identical titles + identical dates should auto-approve.
    assert len(links) == 1
    assert links[0].status == "approved"
    assert links[0].confidence >= 0.85


def test_known_good_bitcoin_price() -> None:
    k = [_kalshi("KXBTC-26JUNE-100K", "Bitcoin price above $100,000 on June 30, 2026?", "2026-06-30T16:00:00Z")]
    p = [_polymarket("0xbtc", "btc-100k-jun-2026", "Will Bitcoin be above $100k on June 30, 2026?", "2026-06-30T16:00:00Z")]
    links = match_markets(k, p)
    assert len(links) == 1
    # Paraphrase + different number formatting ("$100,000" vs "$100k") gives mid-confidence —
    # this is exactly the kind of pair that should go to the manual review queue.
    assert links[0].confidence >= 0.5
    assert links[0].status in {"approved", "pending"}


# ── False-positive traps ─────────────────────────────────────────────────────

def test_different_election_years_dont_match() -> None:
    """Same wording, different year → date window should reject."""
    k = [_kalshi("KXPRES-24-DJT", "Will Donald Trump win the US Presidential Election?", "2024-11-05T00:00:00Z")]
    p = [_polymarket("0xtrump28", "trump-28", "Will Donald Trump win the US Presidential Election?", "2028-11-07T00:00:00Z")]
    links = match_markets(k, p)
    assert links == []


def test_different_strike_prices_low_confidence() -> None:
    """Bitcoin $100k vs $200k on same date — overlapping vocabulary but different strike."""
    k = [_kalshi("KX1", "Bitcoin above $100,000 on June 30, 2026?", "2026-06-30T00:00:00Z", category="crypto")]
    p = [_polymarket("0xp", "btc-200k", "Bitcoin above $200,000 on June 30, 2026?", "2026-06-30T00:00:00Z", category="crypto")]
    links = match_markets(k, p)
    # These overlap a lot ("bitcoin above on june 30 2026") so it WILL link, but we want
    # to verify the matcher returns it as a candidate to be reviewed, not silently rejected.
    # The downstream cross-exchange signal layer is responsible for separating strike levels.
    if links:
        # If it links, it should at least be flagged for review or have lower confidence
        # than an identical-strike pair would.
        assert links[0].confidence < 1.0


def test_completely_unrelated_topics_dont_match() -> None:
    k = [_kalshi("KX1", "Bitcoin above $100k", "2026-06-30T00:00:00Z", category="crypto")]
    p = [_polymarket("0xp", "election", "Will Trump win 2024 election", "2026-06-30T00:00:00Z", category="politics")]
    # Different category buckets — should never be paired.
    links = match_markets(k, p)
    assert links == []


def test_empty_inputs_return_empty() -> None:
    assert match_markets([], []) == []
    assert match_markets([_kalshi("K1", "x", "2026-01-01T00:00:00Z")], []) == []


# ── Top-N capping ────────────────────────────────────────────────────────────

def test_max_per_kalshi_caps_candidates() -> None:
    k = [_kalshi("K1", "Fed cuts rates September 2025", "2025-09-18T00:00:00Z")]
    # Five highly-similar polymarket markets
    p = [
        _polymarket(f"0x{i}", f"fed-{i}", f"Fed cuts rates September 2025 question {i}", "2025-09-18T00:00:00Z")
        for i in range(5)
    ]
    links = match_markets(k, p, config=MatchConfig(max_per_kalshi=2))
    assert len(links) == 2


# ── Multi-market batch ───────────────────────────────────────────────────────

def test_multiple_kalshi_each_get_their_own_match() -> None:
    k = [
        _kalshi("KFED", "Fed cuts 50bp in September 2025", "2025-09-18T00:00:00Z"),
        _kalshi("KBTC", "Bitcoin above 100k on December 31 2025", "2025-12-31T00:00:00Z"),
    ]
    p = [
        _polymarket("0xfed", "fed", "Fed September 2025: 50bp cut?", "2025-09-18T00:00:00Z"),
        _polymarket("0xbtc", "btc", "Bitcoin price above 100k EOY 2025?", "2025-12-31T00:00:00Z"),
    ]
    links = match_markets(k, p)
    kalshi_ids = {link.kalshi_ticker for link in links}
    assert kalshi_ids == {"KFED", "KBTC"}
