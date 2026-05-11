"""
Kalshi leaderboard data collection.

Kalshi does NOT expose a public leaderboard REST API. The leaderboard at
kalshi.com/leaderboard is a React SPA protected by AWS WAF.

Strategy: use Playwright to launch a headless browser, intercept all XHR/fetch
responses, and parse any that look like leaderboard or profile data. This is
more reliable than DOM scraping because JSON API responses are a stable contract.

Prerequisites:
    pip install playwright && playwright install chromium

Two data sources are available:

  1. Playwright network interception (preferred): scrape_leaderboard_with_playwright()
     and scrape_profile_with_playwright() intercept internal Kalshi API calls
     and return structured snapshots.

  2. Watchlist file (manual fallback): a JSONL file at KALSHI_WATCHLIST_PATH
     that you populate by hand. Each line is a JSON object with at least
     {"username": "..."} and optional stats.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("marketsignalos.leaderboard")

# Endpoints we discover during scraping — persisted for future direct HTTP calls.
_DISCOVERED_ENDPOINTS_FILE = "kalshi_discovered_endpoints.json"


@dataclass
class ProfileSnapshot:
    """A point-in-time snapshot of a Kalshi account's performance."""
    username: str
    scraped_at: str
    source: str  # "leaderboard_scrape" | "profile_scrape" | "manual"
    rank: int | None = None
    profit_dollars: float | None = None
    roi_pct: float | None = None
    win_rate: float | None = None
    resolved_trades: int | None = None
    open_trades: int | None = None
    categories: list[str] = field(default_factory=list)
    # raw fragment from intercepted API response — useful for debugging + forward compat
    raw_fragment: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Path helpers
# ──────────────────────────────────────────────────────────────────────────────

def _data_dir() -> Path:
    configured = os.getenv("INGESTOR_DATA_DIR")
    if configured:
        return Path(configured)
    # This file: services/ingestor/src/marketsignalos_ingestor/leaderboard_scraper.py
    # parents[4] is the repo root.
    return Path(__file__).resolve().parents[4] / "services" / "ingestor" / "data"


def watchlist_path() -> Path:
    configured = os.getenv("KALSHI_WATCHLIST_PATH")
    if configured:
        return Path(configured)
    return _data_dir() / "kalshi_leaderboard_watchlist.jsonl"


def profile_snapshots_path() -> Path:
    configured = os.getenv("KALSHI_PROFILE_SNAPSHOTS_PATH")
    if configured:
        return Path(configured)
    return _data_dir() / "kalshi_profile_snapshots.jsonl"


def _discovered_endpoints_path() -> Path:
    return _data_dir() / _DISCOVERED_ENDPOINTS_FILE


# ──────────────────────────────────────────────────────────────────────────────
# Legacy watchlist (manual / backward-compat)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    username: str
    rank: int | None = None
    total_profit_dollars: float | None = None
    win_rate: float | None = None
    resolved_trades: int | None = None
    category: str | None = None
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def load_watchlist(path: Path | None = None) -> list[LeaderboardEntry]:
    target = path or watchlist_path()
    if not target.exists():
        return []
    entries: list[LeaderboardEntry] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            entries.append(
                LeaderboardEntry(
                    username=str(obj["username"]),
                    rank=obj.get("rank"),
                    total_profit_dollars=obj.get("total_profit_dollars"),
                    win_rate=obj.get("win_rate"),
                    resolved_trades=obj.get("resolved_trades"),
                    category=obj.get("category"),
                    fetched_at=obj.get("fetched_at", ""),
                )
            )
        except (KeyError, json.JSONDecodeError) as exc:
            log.warning("watchlist parse error line=%r error=%s", line[:80], exc)
    return entries


def append_to_watchlist(entry: LeaderboardEntry, path: Path | None = None) -> None:
    target = path or watchlist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry), default=str) + "\n")
    log.info("watchlist_append username=%s rank=%s", entry.username, entry.rank)


# ──────────────────────────────────────────────────────────────────────────────
# Profile snapshot persistence
# ──────────────────────────────────────────────────────────────────────────────

def append_profile_snapshots(snapshots: list[ProfileSnapshot], path: Path | None = None) -> int:
    """Append snapshots to the JSONL store. Returns count written."""
    if not snapshots:
        return 0
    target = path or profile_snapshots_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("a", encoding="utf-8") as fh:
        for snap in snapshots:
            fh.write(json.dumps(asdict(snap), default=str) + "\n")
            written += 1
    log.info("profile_snapshots_written count=%d path=%s", written, target)
    return written


def load_profile_snapshots(path: Path | None = None) -> list[ProfileSnapshot]:
    """Load all stored profile snapshots."""
    target = path or profile_snapshots_path()
    if not target.exists():
        return []
    snapshots: list[ProfileSnapshot] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            snapshots.append(
                ProfileSnapshot(
                    username=str(obj["username"]),
                    scraped_at=str(obj["scraped_at"]),
                    source=str(obj.get("source", "unknown")),
                    rank=obj.get("rank"),
                    profit_dollars=obj.get("profit_dollars"),
                    roi_pct=obj.get("roi_pct"),
                    win_rate=obj.get("win_rate"),
                    resolved_trades=obj.get("resolved_trades"),
                    open_trades=obj.get("open_trades"),
                    categories=list(obj.get("categories") or []),
                    raw_fragment=dict(obj.get("raw_fragment") or {}),
                )
            )
        except (KeyError, json.JSONDecodeError) as exc:
            log.warning("snapshot parse error line=%r error=%s", line[:80], exc)
    return snapshots


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint discovery persistence
# ──────────────────────────────────────────────────────────────────────────────

def save_discovered_endpoints(endpoints: dict[str, str]) -> None:
    path = _discovered_endpoints_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    existing.update(endpoints)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    log.info("discovered_endpoints saved count=%d", len(existing))


def load_discovered_endpoints() -> dict[str, str]:
    path = _discovered_endpoints_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except json.JSONDecodeError:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Response parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

_MONEY_RE = re.compile(r"^\$?([\d,]+(?:\.\d+)?)$")


def _to_float(val: object) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("%", "").replace("$", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_leaderboard_row(entry: dict[str, Any], rank: int) -> ProfileSnapshot | None:
    """Parse one row from an intercepted leaderboard API response."""
    # Kalshi's internal fields vary — handle multiple candidate key names.
    username = (
        entry.get("username")
        or entry.get("user_name")
        or entry.get("handle")
        or entry.get("name")
    )
    if not username:
        return None

    profit = (
        _to_float(entry.get("profit"))
        or _to_float(entry.get("total_profit"))
        or _to_float(entry.get("profit_cents", None) and entry["profit_cents"] / 100)
    )
    win_rate = _to_float(entry.get("win_rate") or entry.get("win_percentage"))
    if win_rate is not None and win_rate > 1.5:
        win_rate = win_rate / 100.0  # percent → fraction

    roi = _to_float(entry.get("roi") or entry.get("roi_pct") or entry.get("return_on_investment"))
    if roi is not None and abs(roi) > 2.0:
        roi = roi / 100.0

    resolved = (
        entry.get("resolved_trades")
        or entry.get("num_resolved")
        or entry.get("trades_count")
        or entry.get("total_trades")
    )

    categories_raw = entry.get("categories") or entry.get("markets_categories") or []
    categories = [str(c) for c in categories_raw] if isinstance(categories_raw, list) else []

    return ProfileSnapshot(
        username=str(username),
        scraped_at=datetime.now(timezone.utc).isoformat(),
        source="leaderboard_scrape",
        rank=rank,
        profit_dollars=profit,
        roi_pct=roi,
        win_rate=win_rate,
        resolved_trades=int(resolved) if resolved is not None else None,
        categories=categories,
        raw_fragment={k: v for k, v in entry.items() if not isinstance(v, (dict, list))},
    )


def _extract_snapshots_from_response(
    url: str, body: Any
) -> list[ProfileSnapshot]:
    """Try to extract profile snapshots from any intercepted API response body."""
    snapshots: list[ProfileSnapshot] = []

    if not isinstance(body, dict):
        return snapshots

    # Try top-level list fields that could be leaderboard rows.
    for key in ("users", "leaderboard", "leaders", "members", "traders", "rows", "entries"):
        rows = body.get(key)
        if isinstance(rows, list):
            for i, row in enumerate(rows, start=1):
                if isinstance(row, dict):
                    snap = _parse_leaderboard_row(row, rank=i)
                    if snap:
                        snapshots.append(snap)
            if snapshots:
                log.info(
                    "response_parse url=%s key=%s parsed=%d",
                    url, key, len(snapshots),
                )
                return snapshots

    # Single user profile response.
    if any(k in body for k in ("username", "user_name", "handle")):
        snap = _parse_leaderboard_row(body, rank=0)
        if snap:
            snap.source = "profile_scrape"
            snapshots.append(snap)

    return snapshots


# ──────────────────────────────────────────────────────────────────────────────
# Playwright scrapers
# ──────────────────────────────────────────────────────────────────────────────

def scrape_leaderboard_with_playwright(
    url: str = "https://kalshi.com/social/leaderboard",
    *,
    headless: bool = True,
    timeout_ms: int = 30_000,
    persist: bool = True,
) -> list[ProfileSnapshot]:
    """
    Load kalshi.com/social/leaderboard in a headless browser, click each
    category tab (Profit, Volume, Prediction), intercept all API responses,
    and return structured ProfileSnapshot objects.

    Requires: pip install playwright && playwright install chromium

    Set persist=True (default) to write snapshots + discovered endpoints to disk.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415  # type: ignore
    except ImportError:
        log.error(
            "playwright not installed — "
            "run: pip install playwright && playwright install chromium"
        )
        return []

    # Category tab labels to click through on the leaderboard.
    # Each tab fires a fresh API request with different sort/filter params.
    _LEADERBOARD_TABS = ["Profit", "Volume", "Prediction"]

    intercepted: list[tuple[str, Any, str]] = []  # (url, body, category)
    discovered_urls: dict[str, str] = {}

    snapshots: list[ProfileSnapshot] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        active_category: list[str] = ["default"]

        def _handle_response(response: Any) -> None:
            resp_url_lower = response.url.lower()
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            if not any(kw in resp_url_lower for kw in ("trade-api", "api.kalshi", "leaderboard", "social", "user")):
                return
            try:
                body = response.json()
                intercepted.append((response.url, body, active_category[0]))
                log.debug("intercepted url=%s category=%s", response.url, active_category[0])
            except Exception:
                pass

        page.on("response", _handle_response)

        try:
            log.info("playwright navigating to %s", url)
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
        except Exception as exc:
            log.warning("playwright initial navigation warning: %s", exc)

        # Click through each category tab to capture all leaderboard segments.
        for tab_label in _LEADERBOARD_TABS:
            active_category[0] = tab_label.lower()
            try:
                # Try role-based click first (most reliable), fall back to text.
                tab = page.get_by_role("tab", name=tab_label)
                if not tab.count():
                    tab = page.get_by_text(tab_label, exact=True)
                if tab.count():
                    tab.first.click()
                    page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    log.info("playwright tab_click tab=%s", tab_label)
                else:
                    log.warning("playwright tab not found tab=%s", tab_label)
            except Exception as exc:
                log.warning("playwright tab_click failed tab=%s error=%s", tab_label, exc)

        browser.close()

    # Parse intercepted responses — tag each snapshot with its leaderboard category.
    for resp_url, body, category in intercepted:
        parsed = _extract_snapshots_from_response(resp_url, body)
        if parsed:
            for snap in parsed:
                if category not in snap.categories:
                    snap.categories = [category] + snap.categories
            snapshots.extend(parsed)
            if "leaderboard" in resp_url.lower() or "social" in resp_url.lower():
                discovered_urls[f"leaderboard_{category}"] = resp_url
            elif "user" in resp_url.lower():
                discovered_urls["user_profile"] = resp_url

    # Deduplicate: keep the highest-rank snapshot per username per category.
    seen: dict[tuple[str, str], ProfileSnapshot] = {}
    for snap in snapshots:
        cat = snap.categories[0] if snap.categories else "default"
        key = (snap.username, cat)
        existing = seen.get(key)
        if existing is None or (snap.rank is not None and (existing.rank is None or snap.rank < existing.rank)):
            seen[key] = snap
    snapshots = list(seen.values())

    if not snapshots:
        log.warning(
            "playwright captured %d responses but extracted 0 snapshots — "
            "Kalshi may have changed their API or WAF is blocking",
            len(intercepted),
        )
    else:
        log.info("playwright extracted snapshot_count=%d", len(snapshots))

    if persist:
        if discovered_urls:
            save_discovered_endpoints(discovered_urls)
        if snapshots:
            append_profile_snapshots(snapshots)

    return snapshots


def scrape_profiles_with_playwright(
    usernames: list[str],
    *,
    base_url: str = "https://kalshi.com/profile",
    headless: bool = True,
    timeout_ms: int = 15_000,
    persist: bool = True,
) -> list[ProfileSnapshot]:
    """
    Visit individual profile pages and intercept per-user API responses.
    Useful for building history on a known watchlist.
    """
    if not usernames:
        return []

    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415  # type: ignore
    except ImportError:
        log.error(
            "playwright not installed — "
            "run: pip install playwright && playwright install chromium"
        )
        return []

    all_snapshots: list[ProfileSnapshot] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        for username in usernames:
            intercepted_for_user: list[tuple[str, Any]] = []

            def _handle(response: Any, _u: str = username) -> None:
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    return
                if _u.lower() not in response.url.lower() and "user" not in response.url.lower():
                    return
                try:
                    body = response.json()
                    intercepted_for_user.append((response.url, body))
                except Exception:
                    pass

            page = context.new_page()
            page.on("response", _handle)
            try:
                profile_url = f"{base_url}/{username}"
                log.info("playwright scraping profile username=%s", username)
                page.goto(profile_url, timeout=timeout_ms, wait_until="networkidle")
            except Exception as exc:
                log.warning("profile scrape failed username=%s error=%s", username, exc)
            finally:
                page.close()

            for resp_url, body in intercepted_for_user:
                parsed = _extract_snapshots_from_response(resp_url, body)
                for snap in parsed:
                    # Ensure username is correct even if API response uses different key.
                    snap.username = username
                    snap.source = "profile_scrape"
                all_snapshots.extend(parsed)

        browser.close()

    log.info("profile_scrape total_snapshots=%d usernames=%d", len(all_snapshots), len(usernames))

    if persist and all_snapshots:
        append_profile_snapshots(all_snapshots)

    return all_snapshots
