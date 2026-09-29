from __future__ import annotations

from typing import Any

import pytest

from marketsignalos_polymarket.market_rules import (
    MarketNotFound,
    RulesFieldError,
    fetch_kalshi_market,
    fetch_polymarket_market,
)

KALSHI = "https://kalshi.test/v2"
GAMMA = "https://gamma.test"


def _kalshi_market(**overrides: Any) -> dict[str, Any]:
    market = {
        "ticker": "KXFED-25SEP-T4.25",
        "event_ticker": "KXFED-25SEP",
        "title": "Fed rate after September 2025 meeting?",
        "subtitle": "",
        "yes_sub_title": "Above 4.25%",
        "expiration_time": "2025-09-24T14:00:00Z",
        "close_time": "2025-09-17T18:00:00Z",
        "rules_primary": "If the upper bound is above 4.25% after the meeting, resolves Yes.",
        "rules_secondary": "Source: FOMC statement.",
    }
    market.update(overrides)
    return market


def test_kalshi_rules_join_primary_and_secondary_and_keep_bracket() -> None:
    calls: list[str] = []

    def get(url: str, params: dict[str, str]) -> Any:
        calls.append(url)
        return {"market": _kalshi_market()}

    fetched = fetch_kalshi_market("KXFED-25SEP-T4.25", get_json=get, base_url=KALSHI)

    assert fetched is not None
    assert calls == [f"{KALSHI}/markets/KXFED-25SEP-T4.25"]
    assert fetched.rules == (
        "If the upper bound is above 4.25% after the meeting, resolves Yes.\n\n"
        "Source: FOMC statement."
    )
    assert fetched.rules_fields == ("rules_primary", "rules_secondary")
    assert fetched.subtitle == "Above 4.25%"
    assert fetched.secondary_id == "KXFED-25SEP"
    # Same precedence as the production normalizer: expiration before close.
    assert fetched.end_date == "2025-09-24T14:00:00Z"
    assert "rules_primary" in fetched.payload_keys


def test_kalshi_present_but_empty_secondary_is_not_an_error() -> None:
    fetched = fetch_kalshi_market(
        "T", get_json=lambda u, p: {"market": _kalshi_market(rules_secondary="")},
        base_url=KALSHI,
    )
    assert fetched is not None
    assert fetched.rules.startswith("If the upper bound")
    assert fetched.rules_fields == ("rules_primary", "rules_secondary")


def test_kalshi_missing_rules_keys_fails_loudly_with_the_keys_seen() -> None:
    market = _kalshi_market()
    del market["rules_primary"], market["rules_secondary"]
    with pytest.raises(RulesFieldError, match="keys seen.*yes_sub_title"):
        fetch_kalshi_market("T", get_json=lambda u, p: {"market": market}, base_url=KALSHI)


def test_kalshi_unwrapped_payload_fails_loudly() -> None:
    with pytest.raises(RulesFieldError, match="expected"):
        fetch_kalshi_market("T", get_json=lambda u, p: _kalshi_market(), base_url=KALSHI)


def test_kalshi_404_means_not_found() -> None:
    def get(url: str, params: dict[str, str]) -> Any:
        raise MarketNotFound(url)

    assert fetch_kalshi_market("GONE", get_json=get, base_url=KALSHI) is None


def _gamma_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "conditionId": "0xABC123",
        "question": "Fed decreases rates by 25 bps after September 2025 meeting?",
        "slug": "fed-25bps-sep-2025",
        "endDate": "2025-09-17T00:00:00Z",
        "description": "Resolves Yes if the FOMC lowers the target range by 25 bps.",
        "resolutionSource": "https://www.federalreserve.gov",
    }
    row.update(overrides)
    return row


def test_polymarket_tries_open_then_closed_and_matches_case_insensitively() -> None:
    seen: list[str] = []

    def get(url: str, params: dict[str, str]) -> Any:
        seen.append(params["closed"])
        assert url == f"{GAMMA}/markets"
        assert params["condition_ids"] == "0xabc123"
        return [] if params["closed"] == "false" else [_gamma_row()]

    fetched = fetch_polymarket_market("0xabc123", get_json=get, base_url=GAMMA)

    assert seen == ["false", "true"]
    assert fetched is not None
    assert fetched.market_id == "0xabc123"
    assert fetched.rules == (
        "Resolves Yes if the FOMC lowers the target range by 25 bps.\n\n"
        "Resolution source: https://www.federalreserve.gov"
    )
    assert fetched.rules_fields == ("description", "resolutionSource")
    assert fetched.secondary_id == "fed-25bps-sep-2025"


def test_polymarket_ignores_unrelated_rows_and_reports_not_returned() -> None:
    fetched = fetch_polymarket_market(
        "0xabc123", get_json=lambda u, p: [_gamma_row(conditionId="0xother")], base_url=GAMMA,
    )
    assert fetched is None


def test_polymarket_row_without_description_fails_loudly() -> None:
    row = _gamma_row()
    del row["description"]
    with pytest.raises(RulesFieldError, match="no 'description'"):
        fetch_polymarket_market("0xabc123", get_json=lambda u, p: [row], base_url=GAMMA)


def test_polymarket_non_list_payload_fails_loudly() -> None:
    with pytest.raises(RulesFieldError, match="expected a list"):
        fetch_polymarket_market("0xabc123", get_json=lambda u, p: {"error": "x"}, base_url=GAMMA)


def test_polymarket_empty_resolution_source_is_omitted() -> None:
    fetched = fetch_polymarket_market(
        "0xabc123", get_json=lambda u, p: [_gamma_row(resolutionSource="")], base_url=GAMMA,
    )
    assert fetched is not None
    assert "Resolution source" not in fetched.rules
