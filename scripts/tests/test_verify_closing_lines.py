import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_closing_lines as verifier

GROUPS = {"0xa": "resolved-2024", "0xb": "resolved-2021", "0xc": "resolved-2022"}


def receipt(cid: str, status: str, gap: float | None = None) -> dict[str, Any]:
    return {"condition_id": cid, "status": status, "hours_last_point_before_close": gap,
            "error": ""}


def test_matching_outcomes_verify() -> None:
    problems, report = verifier.verify(
        [receipt("0xa", "ok", 0.4), receipt("0xb", "no_history"), receipt("0xc", "no_history")],
        GROUPS,
    )
    assert problems == []
    assert any("resolved-2024" in line and "ok" in line for line in report)


def test_retryable_2022_market_may_be_fetched_twice() -> None:
    problems, _ = verifier.verify(
        [receipt("0xa", "ok", 0.4), receipt("0xb", "no_history"),
         receipt("0xc", "http_error"), receipt("0xc", "no_history")],
        GROUPS,
    )
    assert problems == []


def test_refetching_a_final_market_fails() -> None:
    problems, _ = verifier.verify(
        [receipt("0xa", "ok", 0.4), receipt("0xa", "ok", 0.4), receipt("0xb", "no_history")],
        GROUPS,
    )
    assert problems == ["refetched a final market: 0xa"]


def test_wrong_outcomes_and_late_closing_lines_fail() -> None:
    problems, _ = verifier.verify(
        [receipt("0xa", "no_history"), receipt("0xb", "ok", 0.1)], GROUPS,
    )
    assert any("expected ok" in p for p in problems)
    assert any("expected no_history" in p for p in problems)
    late, _ = verifier.verify([receipt("0xa", "ok", 5.0)], GROUPS)
    assert late == ["a closing line landed 5.0 h before close"]
