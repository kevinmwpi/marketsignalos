from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket.matching_eval import (
    CaseFileError,
    CaseLabel,
    CaseMarket,
    Decision,
    MatchCase,
    TfidfReplayMatcher,
    evaluate,
    load_cases,
    main,
    make_case_id,
    render_markdown,
    wilson_interval,
    write_cases_atomic,
    write_report,
)


def make_case(
    kalshi_id: str, poly_id: str, *, decision: str, same_event: bool | None,
    band: str = "pending", near_miss: tuple[str, ...] = (), confidence: float = 0.5,
) -> MatchCase:
    def market(exchange: str, market_id: str, title: str) -> CaseMarket:
        return CaseMarket(
            exchange=exchange, market_id=market_id, title=title, end_date="2025-09-17",
            rules=f"rules for {market_id}", rules_status="ok", rules_fields=("rules",),
            rules_fetched_at="2026-09-28T00:00:00Z",
        )

    label = None if same_event is None else CaseLabel(
        same_event=same_event, reason="test label", labeled_by="manual",
        labeled_at="2026-09-28T00:00:00Z", origin="sampled",
    )
    return MatchCase(
        case_id=make_case_id(kalshi_id, poly_id),
        kalshi=market("kalshi", kalshi_id, f"Kalshi {kalshi_id}"),
        polymarket=market("polymarket", poly_id, f"Poly {poly_id}"),
        tfidf_confidence=confidence, tfidf_decision=decision, band=band,
        near_miss=near_miss, label=label,
    )


# ── Schema round trip and validation ─────────────────────────────────────────

def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    cases = [
        make_case("K1", "0xAA", decision="approved", same_event=True, band="auto",
                  near_miss=("numeric_diff",), confidence=0.81),
        make_case("K2", "0xbb", decision="dropped", same_event=False, band="prefilter_miss"),
    ]
    write_cases_atomic(path, cases)
    assert load_cases(path) == cases
    # Condition ids are case-insensitive hex; case ids normalize them.
    assert cases[0].case_id == "K1|0xaa"


def _row(**changes: Any) -> dict[str, Any]:
    row = make_case("K1", "0xaa", decision="pending", same_event=True).to_dict()
    for dotted, value in changes.items():
        target = row
        *parents, leaf = dotted.split("__")
        for key in parents:
            target = target[key]
        target[leaf] = value
    return row


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"case_id": "K9|0xaa"}, "case_id does not match"),
        ({"label": None}, "must be labeled"),
        ({"label__reason": ""}, "non-empty one-line reason"),
        ({"label__reason": "two\nlines"}, "single line"),
        ({"label__reason": "x" * 241}, "exceeds"),
        ({"label__same_event": "yes"}, "true or false"),
        ({"label__origin": "model"}, "label.origin"),
        ({"band": "middle"}, "'band'"),
        ({"tfidf_decision": "maybe"}, "tfidf_decision"),
        ({"tfidf_confidence": 1.5}, "tfidf_confidence"),
        ({"tfidf_confidence": True}, "tfidf_confidence"),
        ({"near_miss": ["typo"]}, "near_miss"),
        ({"kalshi__rules": ""}, "rules_status is 'ok'"),
        ({"polymarket__exchange": "kalshi"}, "exchange"),
    ],
)
def test_invalid_rows_reject_the_whole_file_with_a_line_number(
    tmp_path: Path, changes: dict[str, Any], message: str,
) -> None:
    good = make_case("K0", "0x00", decision="pending", same_event=False).to_dict()
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(good) + "\n" + json.dumps(_row(**changes)) + "\n")
    with pytest.raises(CaseFileError, match=message) as info:
        load_cases(path)
    assert "cases.jsonl:2" in str(info.value)


def test_duplicate_case_ids_and_bad_json_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    row = json.dumps(_row())
    path.write_text(row + "\n" + row + "\n")
    with pytest.raises(CaseFileError, match="duplicate case_id"):
        load_cases(path)
    path.write_text(row + "\n{not json\n")
    with pytest.raises(CaseFileError, match="invalid JSON"):
        load_cases(path)


def test_queue_files_may_hold_unlabeled_rows(tmp_path: Path) -> None:
    path = tmp_path / "to_label.jsonl"
    write_cases_atomic(path, [make_case("K1", "0xaa", decision="pending", same_event=None)])
    [case] = load_cases(path, require_labels=False)
    assert case.label is None
    with pytest.raises(CaseFileError, match="must be labeled"):
        load_cases(path)


def test_unavailable_rules_are_valid_when_marked(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    row = _row(kalshi__rules="", kalshi__rules_status="unavailable")
    path.write_text(json.dumps(row) + "\n")
    [case] = load_cases(path)
    assert case.kalshi.rules_status == "unavailable"


def test_missing_file_is_an_empty_eval(tmp_path: Path) -> None:
    assert load_cases(tmp_path / "absent.jsonl") == []


# ── Scoring ──────────────────────────────────────────────────────────────────

def test_wilson_interval_known_values() -> None:
    assert wilson_interval(0, 0) is None
    low, high = wilson_interval(8, 10) or (0.0, 0.0)
    assert (round(low, 4), round(high, 4)) == (0.4902, 0.9433)
    low, high = wilson_interval(10, 10) or (0.0, 0.0)
    assert high == 1.0 and round(low, 4) == 0.7225
    low, high = wilson_interval(0, 10) or (0.0, 0.0)
    assert low == 0.0 and round(high, 4) == 0.2775


def _mixed_cases() -> list[MatchCase]:
    spec = (
        [("approved", True, "auto")] * 3 + [("approved", False, "auto")]
        + [("pending", True, "pending")] * 2 + [("pending", False, "pending")]
        + [("dropped", True, "below")] + [("dropped", False, "below")] * 2
    )
    return [
        make_case(f"K{i}", f"0x{i:02x}", decision=d, same_event=t, band=b,
                  near_miss=("sibling_bracket",) if i == 3 else ())
        for i, (d, t, b) in enumerate(spec)
    ]


def test_tfidf_replay_matcher_reports_the_recorded_decision() -> None:
    matcher = TfidfReplayMatcher()
    decisions = {
        "approved": Decision.APPROVE, "pending": Decision.PENDING, "dropped": Decision.REJECT,
    }
    for recorded, expected in decisions.items():
        case = make_case("K", "0x1", decision=recorded, same_event=True)
        assert matcher.decide(case) is expected


def test_evaluate_counts_every_outcome_exactly() -> None:
    report = evaluate(_mixed_cases(), TfidfReplayMatcher())
    m = report["metrics"]

    assert m["confusion"] == {
        "true_positive": 3, "false_positive": 1, "true_negative": 2, "false_negative": 1,
        "pending_positive": 2, "pending_negative": 1,
    }
    assert m["cases"] == 10 and m["positives"] == 6 and m["negatives"] == 4
    assert m["precision"] == pytest.approx(0.75)
    assert m["recall"] == pytest.approx(0.5)
    assert m["recall_reaching_review"] == pytest.approx(5 / 6)
    assert m["abstention_rate"] == pytest.approx(0.3)
    assert m["reject_precision"] == pytest.approx(2 / 3)
    assert m["precision_ci95"] == pytest.approx(list(wilson_interval(3, 4) or ()))

    wrong = {w["case_id"]: w["kind"] for w in report["wrong_decisions"]}
    assert wrong == {"K3|0x03": "false_positive", "K7|0x07": "false_negative"}
    assert len(report["pending"]) == 3
    assert set(report["by_band"]) == {"auto", "pending", "below"}
    assert report["by_band"]["auto"]["precision"] == pytest.approx(0.75)
    assert report["by_near_miss"]["sibling_bracket"]["confusion"]["false_positive"] == 1
    assert any("approvals" in w for w in report["warnings"])


def test_no_approvals_leaves_precision_undefined_not_perfect() -> None:
    cases = [make_case("K1", "0x1", decision="pending", same_event=True)]
    report = evaluate(cases, TfidfReplayMatcher())
    assert report["metrics"]["precision"] is None
    assert report["metrics"]["precision_ci95"] is None
    assert "n/a" in render_markdown(report)


def test_evaluate_refuses_unlabeled_cases() -> None:
    with pytest.raises(CaseFileError, match="labeled"):
        evaluate([make_case("K1", "0x1", decision="pending", same_event=None)],
                 TfidfReplayMatcher())


def test_report_files_and_case_file_hash(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    write_cases_atomic(cases_path, _mixed_cases())
    report = evaluate(load_cases(cases_path), TfidfReplayMatcher(), cases_path=cases_path)
    json_path, md_path = write_report(report, tmp_path / "reports", "baseline")

    assert report["cases_file"]["sha256"] == hashlib.sha256(cases_path.read_bytes()).hexdigest()
    assert json.loads(json_path.read_text())["metrics"]["approved"] == 4
    markdown = md_path.read_text()
    assert "Every wrong decision" in markdown
    wrong_section = markdown.split("## Every wrong decision")[1].split("## Pending")[0]
    assert "K3|0x03" in wrong_section and "K7|0x07" in wrong_section
    assert "K6|0x06" not in wrong_section  # a pending negative is not a wrong decision
    assert report["matcher"]["defaults_at_build"]["auto_approve_threshold"] == 0.75


def test_cli_run_writes_the_baseline(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    write_cases_atomic(cases_path, _mixed_cases())
    reports = tmp_path / "reports"
    code = main(["run", "--cases", str(cases_path), "--reports-dir", str(reports),
                 "--name", "tfidf-baseline"])
    assert code == 0
    assert (reports / "tfidf-baseline.json").exists()
    assert (reports / "tfidf-baseline.md").exists()


def test_cli_run_with_no_cases_fails(tmp_path: Path) -> None:
    code = main(["run", "--cases", str(tmp_path / "none.jsonl"),
                 "--reports-dir", str(tmp_path)])
    assert code == 1


def test_cli_reports_expected_errors_in_one_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    code = main(["sample", "--data-dir", str(tmp_path), "--cases", str(tmp_path / "c.jsonl"),
                 "--queue", str(tmp_path / "q.jsonl")])
    assert code == 1
    assert "need kalshi_markets.jsonl" in caplog.text

    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n")
    assert main(["run", "--cases", str(bad), "--reports-dir", str(tmp_path)]) == 1
    assert "bad.jsonl:1: invalid JSON" in caplog.text


def test_write_is_atomic_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cases.jsonl"
    write_cases_atomic(path, _mixed_cases()[:1])
    before = path.read_bytes()

    class Boom(RuntimeError):
        pass

    original: Callable[[MatchCase], dict[str, Any]] = MatchCase.to_dict

    def explode(self: MatchCase) -> dict[str, Any]:
        if self.case_id.startswith("K1|"):
            raise Boom
        return original(self)

    monkeypatch.setattr(MatchCase, "to_dict", explode)
    with pytest.raises(Boom):
        write_cases_atomic(path, _mixed_cases()[:2])
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]  # no stray temporary file
