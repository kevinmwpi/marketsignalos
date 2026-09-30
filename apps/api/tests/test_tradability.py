from __future__ import annotations

from marketsignalos_api.services.tradability import (
    KalshiMirrorInput,
    MarketTradabilityInput,
    classify_tradability,
    is_actionable,
    resolve_event_slug,
)


def test_resolve_event_slug_prefers_buy_slug() -> None:
    assert (
        resolve_event_slug(
            position_slug="position-slug",
            buy_slug="buy-slug",
            gamma_slug="gamma-slug",
        )
        == "buy-slug"
    )


def test_classify_poly_direct_when_market_active() -> None:
    status, reasons = classify_tradability(
        market=MarketTradabilityInput(active=True, closed=False, category="politics"),
        event_slug="fed-decision",
        title="Fed cuts 50bp",
        mirror=None,
    )
    assert status == "poly_direct"
    assert reasons == []


def test_classify_kalshi_mirror_when_poly_closed() -> None:
    status, reasons = classify_tradability(
        market=MarketTradabilityInput(active=False, closed=True, category="politics"),
        event_slug="fed-decision",
        title="Fed cuts 50bp",
        mirror=KalshiMirrorInput(status="approved", confidence=0.91),
    )
    assert status == "kalshi_mirror"
    assert any("Kalshi match" in reason for reason in reasons)


def test_classify_on_chain_only_without_slug_or_mirror() -> None:
    status, reasons = classify_tradability(
        market=MarketTradabilityInput(active=True, closed=False, category="weather"),
        event_slug="",
        title="UK rainfall above 50mm in March",
        mirror=KalshiMirrorInput(status="pending", confidence=0.42),
    )
    assert status == "on_chain_only"
    assert any("event slug" in reason for reason in reasons)
    assert any("not approved" in reason for reason in reasons)


def test_is_actionable() -> None:
    assert is_actionable("poly_direct")
    assert is_actionable("kalshi_mirror")
    assert not is_actionable("closed")
    assert not is_actionable("on_chain_only")
