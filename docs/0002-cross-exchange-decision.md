# ADR 0002 — Polymarket as primary skill source; Kalshi as tail target

## Correction (2026-05-23)

The original framing of this ADR — "cross-exchange price dislocation as the headline signal" — was wrong for our operator. The operator is US-based and **cannot bet on Polymarket at all**, which makes a two-legged dislocation signal useless: there is no second leg to take. Polymarket is the *source of intelligence*; Kalshi is the *only venue where the operator can act*.

The corrected product is what alternative #2 below described and rejected: **a Polymarket-only skilled-bets feed, with the equivalent Kalshi market shown alongside each row as the actionable tail target**. The rejection reasoning ("doesn't produce a tradeable spread by itself") was the wrong objective — we don't want a spread, we want a single defensible bet to place on Kalshi.

Concretely:
- `/signals/skilled-bets` (already implemented) is the headline endpoint and surfaces the Kalshi mirror per row.
- `/signals/cross-exchange`, the `CrossExchangePanel`, and the `min_dislocation_pct` / `cheaper_exchange` / `recommended_action` framing have been removed.
- The matcher (`market_matcher.py` → `market_links.jsonl`) and Kalshi market fetch remain — they back the per-row Kalshi mirror column. Only the dislocation-signal join layer was removed.

The original decision and reasoning below are preserved for historical context. Read alternative #2's rejection as the load-bearing mistake.

---

## Decision (original — superseded by correction above)

The primary signal for MarketSignalOS is **cross-exchange price dislocation**: skilled Polymarket wallets' active positions priced against equivalent Kalshi markets. Polymarket is the source of skill scoring; Kalshi is the comparison side.

## Context

The original v1 plan computed `skill_likelihood` from Kalshi data alone, relying on a Playwright scrape of `kalshi.com/social/leaderboard` to identify skilled accounts.

Kalshi exposes leaderboard ranks (total profit, volume, prediction accuracy) but **hides per-user bet history on social profiles**. There is no public way to recover the trades that produced a given user's ranking, so we cannot:

- Compute a true binomial-test skill score (requires per-bet outcomes)
- Identify which markets a skilled user is currently active in
- Detect irregular flow attributable to a specific user

Polymarket, in contrast, settles every trade on Polygon. The Data API (`data-api.polymarket.com/activity?user=<addr>`) returns a wallet's complete trade and redemption history with no auth required. Combined with Gamma's resolved-market metadata, this lets us compute a real on-chain win rate per wallet.

## Alternatives considered

1. **Kalshi-only with manual watchlist annotation.** Operators would hand-curate a list of suspected skilled users; we'd track only what their leaderboard rank says. Rejected: not statistically defensible and not scalable.
2. **Polymarket-only signal.** Compute skill on Polymarket and surface skilled wallets' positions directly. Rejected as the headline signal: this is useful but doesn't produce a tradeable spread by itself.
3. **Build Kalshi skill from first-party fills.** Use the API account's own fills via `/portfolio/fills`. Rejected: this scores *our own* account, not other users — fundamentally a different product.
4. **Cross-exchange signal (chosen).** Use Polymarket for skill, Kalshi for the second price. A 4¢ spread between exchanges with a 90%-skill wallet long the Polymarket side is a defensible tradeable signal.

## Consequences

### Architectural

- A second ingestion pipeline lives at `services/polymarket-ingestor/`, parallel to `services/ingestor/` (Kalshi).
- A new join layer (`apps/api/src/marketsignalos_api/services/cross_exchange.py`) glues the two sides at request time.
- A title-similarity matcher (`market_matcher.py`) produces `market_links.jsonl` mapping Kalshi tickers to Polymarket condition_ids. The matcher is conservative — mid-confidence pairs go to a manual review queue; rejections are sticky.

### Product

- The headline dashboard panel is now `CrossExchangePanel`, ranked by `opportunity_score = |dislocation_pct| × skill_likelihood × log1p(position_value_usdc)`.
- The existing Kalshi pipeline is **not removed**. Its `/signals/leaderboard` and `/signals/orderflow` endpoints remain useful for analytics on our own fill data. They are demoted from headline to secondary sidebar panels.
- The Kalshi profile scraper (`leaderboard_scraper.py`) is retained but no longer drives skill scoring; it produces context for users who want to cross-reference a Kalshi public name with cross-exchange opportunities (manual lookup, not part of the automated pipeline).

### Operational

- The pipeline ships JSONL-first. Postgres support is stubbed; tracked as Phase 7.5.
- No new auth surface — every Polymarket endpoint we use is public.
- One new CI job (`polymarket` in `.github/workflows/ci.yml`) runs lint + type-check + tests for the new package.
- The `all` mode is gated by `INGEST_POLYMARKET=1` so a deploy can ship the binary without immediately triggering wallet backfill traffic.

### Non-goals

- We do not attempt to identify Polymarket wallet owners or correlate them to Kalshi accounts. The cross-exchange signal does not need that information.
- We do not place trades. Both endpoints are read-only.
- We do not chase "anomalous wallets" with naming — `insider_like_score` was never extended to Polymarket and won't be. The score we publish is `skill_likelihood`, full stop.

## Status

Superseded in part by the 2026-05-23 correction at the top of this file. Cross-exchange dislocation as the headline signal is dead; the Polymarket-only skilled-bets feed (with a Kalshi mirror column) is live. The matcher, Kalshi market fetch, and `market_links.jsonl` remain in active use as the join key for the Kalshi mirror.

Implemented through Phase 6 (dashboard surface). Phase 7 wires CI + docs + the `INGEST_POLYMARKET` flag. Phase 7.5 (Postgres) and Phase 8 (matcher tuning, position pagination) are tracked in `polymarket-pipeline.md` under "Known limitations".
