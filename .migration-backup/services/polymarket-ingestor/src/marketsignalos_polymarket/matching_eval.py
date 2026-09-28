"""
Eval set and scorer for the Polymarket -> Kalshi market matcher.

A *case* is one (Kalshi market, Polymarket market) pair with both titles, end
dates, resolution rules text, the production TF-IDF decision recorded when the
pair was collected, and a human label: ``same_event`` plus a one-line reason.

``same_event`` is true only when a YES on the Kalshi market and a YES on the
Polymarket market resolve identically in every realistic outcome: same
proposition, same threshold or bracket, same deadline, compatible resolution
source. Inverse framings, sibling brackets and different deadlines are false.
docs/llm-judge.md explains why the definition is this strict.

A *matcher* maps a case to APPROVE, REJECT or PENDING (leave for a human). The
scorer reports precision on approvals (the metric that matters: a wrong link
feeds a wrong mirror price downstream), recall, abstention, per-band results
and every wrong decision.

No model is called anywhere in this module.

CLI (run from the repository root)::

    python -m marketsignalos_polymarket.matching_eval verify-fields
    python -m marketsignalos_polymarket.matching_eval seed
    python -m marketsignalos_polymarket.matching_eval sample --size 50
    python -m marketsignalos_polymarket.matching_eval label
    python -m marketsignalos_polymarket.matching_eval run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import httpx

from .market_rules import RulesFieldError

log = logging.getLogger("marketsignalos.polymarket.matching_eval")

SCHEMA_VERSION = 1
DEFAULT_EVAL_DIR = Path("evals") / "market_matching"
DEFAULT_CASES = DEFAULT_EVAL_DIR / "cases.jsonl"
DEFAULT_QUEUE = DEFAULT_EVAL_DIR / "to_label.jsonl"
DEFAULT_REPORTS = DEFAULT_EVAL_DIR / "reports"

# Where the production matcher placed the pair when it was collected.
BANDS = frozenset({"auto", "pending", "below", "prefilter_miss"})
# Independent flags for pairs built to test known false-positive shapes.
NEAR_MISS_KINDS = frozenset({"numeric_diff", "sibling_bracket", "date_gap"})
TFIDF_DECISIONS = frozenset({"approved", "pending", "dropped"})
ORIGINS = frozenset({"market_links_manual", "sampled"})
RULES_STATUSES = frozenset({"ok", "unavailable"})
MAX_REASON_CHARS = 240


class CaseFileError(ValueError):
    """A case file failed validation. The whole file is rejected."""


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def make_case_id(kalshi_ticker: str, condition_id: str) -> str:
    return f"{kalshi_ticker}|{condition_id.lower()}"


# ── Schema ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CaseMarket:
    exchange: str  # "kalshi" | "polymarket"
    market_id: str  # Kalshi ticker | Polymarket condition id
    title: str
    end_date: str
    rules: str
    rules_status: str  # "ok" | "unavailable"
    rules_fields: tuple[str, ...]
    rules_fetched_at: str
    secondary_id: str = ""  # Kalshi event_ticker | Polymarket slug
    subtitle: str = ""  # Kalshi bracket text; brackets share titles, so this matters

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange, "market_id": self.market_id, "title": self.title,
            "subtitle": self.subtitle, "secondary_id": self.secondary_id,
            "end_date": self.end_date, "rules": self.rules, "rules_status": self.rules_status,
            "rules_fields": list(self.rules_fields), "rules_fetched_at": self.rules_fetched_at,
        }


@dataclass(frozen=True, slots=True)
class CaseLabel:
    same_event: bool
    reason: str
    labeled_by: str
    labeled_at: str
    origin: str  # "market_links_manual" | "sampled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "same_event": self.same_event, "reason": self.reason, "labeled_by": self.labeled_by,
            "labeled_at": self.labeled_at, "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class MatchCase:
    case_id: str
    kalshi: CaseMarket
    polymarket: CaseMarket
    tfidf_confidence: float
    tfidf_decision: str  # "approved" | "pending" | "dropped"
    band: str
    label: CaseLabel | None  # None only in the unlabeled queue
    near_miss: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "case_id": self.case_id,
            "kalshi": self.kalshi.to_dict(), "polymarket": self.polymarket.to_dict(),
            "tfidf_confidence": self.tfidf_confidence, "tfidf_decision": self.tfidf_decision,
            "band": self.band, "near_miss": list(self.near_miss),
            "label": self.label.to_dict() if self.label else None,
        }

    def with_label(self, label: CaseLabel) -> MatchCase:
        return MatchCase(
            case_id=self.case_id, kalshi=self.kalshi, polymarket=self.polymarket,
            tfidf_confidence=self.tfidf_confidence, tfidf_decision=self.tfidf_decision,
            band=self.band, label=label, near_miss=self.near_miss,
            schema_version=self.schema_version,
        )


# ── Parsing and validation ───────────────────────────────────────────────────

def _req_str(obj: dict[str, Any], key: str, where: str, *, allow_empty: bool = False) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise CaseFileError(f"{where}: '{key}' must be a string")
    if not allow_empty and not value.strip():
        raise CaseFileError(f"{where}: '{key}' must not be empty")
    return value


def _parse_market(obj: Any, exchange: str, where: str) -> CaseMarket:
    if not isinstance(obj, dict):
        raise CaseFileError(f"{where}: '{exchange}' must be an object")
    where = f"{where}.{exchange}"
    if obj.get("exchange") != exchange:
        raise CaseFileError(f"{where}: 'exchange' must be '{exchange}'")
    status = _req_str(obj, "rules_status", where)
    if status not in RULES_STATUSES:
        raise CaseFileError(f"{where}: 'rules_status' must be one of {sorted(RULES_STATUSES)}")
    rules = _req_str(obj, "rules", where, allow_empty=True)
    if status == "ok" and not rules.strip():
        raise CaseFileError(f"{where}: rules_status is 'ok' but 'rules' is empty")
    fields = obj.get("rules_fields")
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise CaseFileError(f"{where}: 'rules_fields' must be a list of strings")
    return CaseMarket(
        exchange=exchange,
        market_id=_req_str(obj, "market_id", where),
        title=_req_str(obj, "title", where),
        end_date=_req_str(obj, "end_date", where, allow_empty=True),
        rules=rules,
        rules_status=status,
        rules_fields=tuple(fields),
        rules_fetched_at=_req_str(obj, "rules_fetched_at", where, allow_empty=True),
        secondary_id=_req_str(obj, "secondary_id", where, allow_empty=True),
        subtitle=_req_str(obj, "subtitle", where, allow_empty=True),
    )


def _parse_label(obj: Any, where: str) -> CaseLabel:
    if not isinstance(obj, dict):
        raise CaseFileError(f"{where}: 'label' must be an object")
    same_event = obj.get("same_event")
    if not isinstance(same_event, bool):
        raise CaseFileError(f"{where}: 'label.same_event' must be true or false")
    reason = validate_reason(obj.get("reason"), where)
    origin = _req_str(obj, "origin", f"{where}.label")
    if origin not in ORIGINS:
        raise CaseFileError(f"{where}: 'label.origin' must be one of {sorted(ORIGINS)}")
    return CaseLabel(
        same_event=same_event,
        reason=reason,
        labeled_by=_req_str(obj, "labeled_by", f"{where}.label"),
        labeled_at=_req_str(obj, "labeled_at", f"{where}.label"),
        origin=origin,
    )


def validate_reason(value: Any, where: str = "reason") -> str:
    """A label reason is one non-empty line of at most MAX_REASON_CHARS."""
    if not isinstance(value, str) or not value.strip():
        raise CaseFileError(f"{where}: a label needs a non-empty one-line reason")
    if "\n" in value or "\r" in value:
        raise CaseFileError(f"{where}: the reason must be a single line")
    if len(value) > MAX_REASON_CHARS:
        raise CaseFileError(f"{where}: the reason exceeds {MAX_REASON_CHARS} characters")
    return value.strip()


def parse_case(obj: Any, where: str, *, require_label: bool) -> MatchCase:
    if not isinstance(obj, dict):
        raise CaseFileError(f"{where}: each line must be a JSON object")
    if obj.get("schema_version") != SCHEMA_VERSION:
        raise CaseFileError(f"{where}: unsupported schema_version {obj.get('schema_version')!r}")
    kalshi = _parse_market(obj.get("kalshi"), "kalshi", where)
    polymarket = _parse_market(obj.get("polymarket"), "polymarket", where)
    case_id = _req_str(obj, "case_id", where)
    if case_id != make_case_id(kalshi.market_id, polymarket.market_id):
        raise CaseFileError(f"{where}: case_id does not match the two market ids")
    confidence = obj.get("tfidf_confidence")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0):
        raise CaseFileError(f"{where}: 'tfidf_confidence' must be a number in [0, 1]")
    decision = _req_str(obj, "tfidf_decision", where)
    if decision not in TFIDF_DECISIONS:
        raise CaseFileError(f"{where}: 'tfidf_decision' must be one of {sorted(TFIDF_DECISIONS)}")
    band = _req_str(obj, "band", where)
    if band not in BANDS:
        raise CaseFileError(f"{where}: 'band' must be one of {sorted(BANDS)}")
    near_miss = obj.get("near_miss", [])
    if not isinstance(near_miss, list) or any(k not in NEAR_MISS_KINDS for k in near_miss):
        raise CaseFileError(f"{where}: 'near_miss' must list kinds from {sorted(NEAR_MISS_KINDS)}")
    raw_label = obj.get("label")
    if raw_label is None:
        if require_label:
            raise CaseFileError(f"{where}: cases.jsonl rows must be labeled")
        label = None
    else:
        label = _parse_label(raw_label, where)
    return MatchCase(
        case_id=case_id, kalshi=kalshi, polymarket=polymarket,
        tfidf_confidence=float(confidence), tfidf_decision=decision, band=band,
        label=label, near_miss=tuple(near_miss),
    )


def load_cases(path: Path, *, require_labels: bool = True) -> list[MatchCase]:
    """Load and validate a case file. Any invalid line rejects the whole file.

    Silently skipping a bad row would change the denominator of every metric
    without anyone noticing, so this fails closed.
    """
    if not path.exists():
        return []
    cases: list[MatchCase] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            where = f"{path.name}:{number}"
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CaseFileError(f"{where}: invalid JSON ({exc.msg})") from exc
            case = parse_case(obj, where, require_label=require_labels)
            if case.case_id in seen:
                raise CaseFileError(f"{where}: duplicate case_id {case.case_id}")
            seen.add(case.case_id)
            cases.append(case)
    return cases


def write_cases_atomic(path: Path, cases: Sequence[MatchCase]) -> None:
    """Rewrite a case file through a temporary file so a crash never leaves half a line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for case in cases:
                handle.write(json.dumps(case.to_dict(), ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


# ── Matchers ─────────────────────────────────────────────────────────────────

class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    PENDING = "pending"


class Matcher(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def decide(self, case: MatchCase) -> Decision: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TfidfReplayMatcher:
    """The current production matcher, replayed from each case's recorded decision.

    TF-IDF weights depend on the whole market corpus at match time, so a single
    pair cannot be re-scored in isolation. Each case therefore stores the
    decision production made (after category/date filtering, thresholds and the
    top-N-per-Kalshi cut), and this matcher reports it back.
    """

    name: str = "tfidf"
    version: str = "market_matcher@2026-08-23"

    def decide(self, case: MatchCase) -> Decision:
        if case.tfidf_decision == "approved":
            return Decision.APPROVE
        if case.tfidf_decision == "pending":
            return Decision.PENDING
        return Decision.REJECT

    def describe(self) -> dict[str, Any]:
        from .market_matcher import MatchConfig

        config = MatchConfig()
        return {
            "name": self.name, "version": self.version,
            "decision_source": "recorded production decision per case",
            "defaults_at_build": {
                "auto_approve_threshold": config.auto_approve_threshold,
                "review_threshold": config.review_threshold,
                "date_window_days": config.date_window_days,
                "max_per_kalshi": config.max_per_kalshi,
            },
        }


# ── Scoring ──────────────────────────────────────────────────────────────────

def wilson_interval(successes: int, trials: int, z: float = 1.959964) -> tuple[float, float] | None:
    """95% Wilson score interval; well-behaved at 0/n and n/n, unlike the normal approximation."""
    if trials == 0:
        return None
    p = successes / trials
    denominator = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    # The bounds are exactly 0 and 1 at the extremes; avoid 1e-17 float residue.
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == trials else min(1.0, centre + half)
    return (low, high)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case: MatchCase
    decision: Decision

    @property
    def truth(self) -> bool:
        assert self.case.label is not None
        return self.case.label.same_event

    @property
    def kind(self) -> str:
        if self.decision is Decision.PENDING:
            return "pending_positive" if self.truth else "pending_negative"
        if self.decision is Decision.APPROVE:
            return "true_positive" if self.truth else "false_positive"
        return "false_negative" if self.truth else "true_negative"


def _metrics(outcomes: Sequence[CaseOutcome]) -> dict[str, Any]:
    kinds = Counter(o.kind for o in outcomes)
    tp, fp = kinds["true_positive"], kinds["false_positive"]
    tn, fn = kinds["true_negative"], kinds["false_negative"]
    pending_pos, pending_neg = kinds["pending_positive"], kinds["pending_negative"]
    positives = tp + fn + pending_pos
    interval = wilson_interval(tp, tp + fp)
    return {
        "cases": len(outcomes),
        "positives": positives,
        "negatives": len(outcomes) - positives,
        "confusion": {
            "true_positive": tp, "false_positive": fp, "true_negative": tn,
            "false_negative": fn, "pending_positive": pending_pos,
            "pending_negative": pending_neg,
        },
        "approved": tp + fp,
        # Precision on automatic approvals. None means the matcher approved nothing.
        "precision": _ratio(tp, tp + fp),
        "precision_ci95": list(interval) if interval else None,
        # True matches linked automatically. Pending counts as not linked.
        "recall": _ratio(tp, positives),
        # True matches that at least reach a human reviewer (approved or pending).
        "recall_reaching_review": _ratio(tp + pending_pos, positives),
        "abstention_rate": _ratio(pending_pos + pending_neg, len(outcomes)),
        # Of the pairs the matcher rejected, how many were truly different.
        "reject_precision": _ratio(tn, tn + fn),
    }


def _case_summary(outcome: CaseOutcome) -> dict[str, Any]:
    case = outcome.case
    assert case.label is not None
    return {
        "case_id": case.case_id, "kind": outcome.kind, "decision": outcome.decision.value,
        "same_event": case.label.same_event, "label_reason": case.label.reason,
        "band": case.band, "near_miss": list(case.near_miss),
        "tfidf_confidence": case.tfidf_confidence,
        "kalshi_title": case.kalshi.title, "kalshi_subtitle": case.kalshi.subtitle,
        "kalshi_end_date": case.kalshi.end_date,
        "polymarket_title": case.polymarket.title, "polymarket_end_date": case.polymarket.end_date,
    }


def evaluate(
    cases: Sequence[MatchCase], matcher: Matcher, *, cases_path: Path | None = None,
) -> dict[str, Any]:
    """Score ``matcher`` on labeled ``cases``. Returns a JSON-serializable report."""
    if any(case.label is None for case in cases):
        raise CaseFileError("evaluate() requires every case to be labeled")
    outcomes = [CaseOutcome(case, matcher.decide(case)) for case in cases]

    by_band = {
        band: _metrics([o for o in outcomes if o.case.band == band])
        for band in sorted({o.case.band for o in outcomes})
    }
    by_near_miss = {
        kind: _metrics([o for o in outcomes if kind in o.case.near_miss])
        for kind in sorted({k for o in outcomes for k in o.case.near_miss})
    }
    by_origin = {
        origin: _metrics([o for o in outcomes if o.case.label and o.case.label.origin == origin])
        for origin in sorted({o.case.label.origin for o in outcomes if o.case.label})
    }
    metrics = _metrics(outcomes)
    warnings: list[str] = []
    if metrics["approved"] < 20:
        warnings.append(
            f"Only {metrics['approved']} automatic approvals: one extra wrong approval moves "
            "precision substantially. Read the confidence interval, not the point estimate."
        )
    if metrics["positives"] == 0:
        warnings.append("No same_event=true cases: recall is undefined.")
    unavailable = sum(
        1 for case in cases
        if "unavailable" in (case.kalshi.rules_status, case.polymarket.rules_status)
    )
    if unavailable:
        warnings.append(f"{unavailable} case(s) have rules text unavailable on one side.")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utcnow_iso(),
        "matcher": matcher.describe(),
        "cases_file": {
            "path": str(cases_path) if cases_path else None,
            "sha256": file_sha256(cases_path) if cases_path else None,
            "cases": len(cases),
        },
        "metrics": metrics,
        "by_band": by_band,
        "by_near_miss": by_near_miss,
        "by_label_origin": by_origin,
        "wrong_decisions": [
            _case_summary(o) for o in outcomes if o.kind in {"false_positive", "false_negative"}
        ],
        "pending": [_case_summary(o) for o in outcomes if o.decision is Decision.PENDING],
        "warnings": warnings,
    }


# ── Report rendering ─────────────────────────────────────────────────────────

def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _ci(value: list[float] | None) -> str:
    return "n/a" if not value else f"{value[0]:.1%}–{value[1]:.1%}"


def _metrics_row(name: str, m: dict[str, Any]) -> str:
    return (
        f"| {name} | {m['cases']} | {m['positives']} | {m['approved']} | {_pct(m['precision'])} "
        f"| {_ci(m['precision_ci95'])} | {_pct(m['recall'])} | {_pct(m['abstention_rate'])} |"
    )


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    c = m["confusion"]
    lines = [
        f"# Market matcher eval: {report['matcher']['name']}",
        "",
        (f"Generated {report['generated_at']}. Cases: {report['cases_file']['cases']} "
         f"(`{report['cases_file']['path']}`, sha256 `{report['cases_file']['sha256']}`)."),
        "",
        "## Headline",
        "",
        (f"- **Precision on automatic approvals: {_pct(m['precision'])}** "
         f"(95% CI {_ci(m['precision_ci95'])}, {m['approved']} approvals)"),
        (f"- Recall (auto-linked): {_pct(m['recall'])}; "
         f"reaching a reviewer: {_pct(m['recall_reaching_review'])}"),
        f"- Left pending for a human: {_pct(m['abstention_rate'])}",
        f"- Rejections that were truly different: {_pct(m['reject_precision'])}",
        "",
        "| | same_event=true | same_event=false |",
        "|---|---:|---:|",
        f"| approved | {c['true_positive']} | {c['false_positive']} |",
        f"| pending | {c['pending_positive']} | {c['pending_negative']} |",
        f"| rejected / dropped | {c['false_negative']} | {c['true_negative']} |",
        "",
    ]
    if report["warnings"]:
        lines += ["## Warnings", ""] + [f"- {w}" for w in report["warnings"]] + [""]
    header = [
        "| Slice | Cases | True | Approved | Precision | 95% CI | Recall | Pending |",
        "|---|---:|---:|---:|---:|---|---:|---:|",
    ]
    for title, key in (("By band", "by_band"), ("By near-miss kind", "by_near_miss"),
                       ("By label origin", "by_label_origin")):
        if report[key]:
            lines += [f"## {title}", ""] + header
            lines += [_metrics_row(name, sl) for name, sl in report[key].items()] + [""]
    lines += ["## Every wrong decision", ""]
    if not report["wrong_decisions"]:
        lines += ["None.", ""]
    for w in report["wrong_decisions"]:
        lines += [
            f"- **{w['kind']}** `{w['case_id']}` (band {w['band']}, tfidf {w['tfidf_confidence']})",
            f"  - Kalshi: {_escape(w['kalshi_title'])}"
            + (f" — {_escape(w['kalshi_subtitle'])}" if w["kalshi_subtitle"] else "")
            + f" (ends {w['kalshi_end_date']})",
            f"  - Polymarket: {_escape(w['polymarket_title'])} (ends {w['polymarket_end_date']})",
            f"  - Label: same_event={str(w['same_event']).lower()} — {_escape(w['label_reason'])}",
        ]
    lines += ["", f"## Pending ({len(report['pending'])})", ""]
    lines += [
        f"- `{p['case_id']}` same_event={str(p['same_event']).lower()} tfidf {p['tfidf_confidence']}"
        for p in report["pending"]
    ]
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], reports_dir: Path, name: str) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{name}.json"
    md_path = reports_dir / f"{name}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


# ── CLI ──────────────────────────────────────────────────────────────────────

MATCHERS: dict[str, type[TfidfReplayMatcher]] = {"tfidf": TfidfReplayMatcher}


def _default_data_dir() -> Path:
    from .runner import _data_dir

    return _data_dir()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m marketsignalos_polymarket.matching_eval")
    sub = parser.add_subparsers(dest="command", required=True)

    vf = sub.add_parser("verify-fields", help="Fetch one market per exchange and show rules fields")
    vf.add_argument("--data-dir", type=Path, default=None)
    vf.add_argument("--kalshi-ticker", default=None)
    vf.add_argument("--polymarket-condition", default=None)

    seed = sub.add_parser("seed", help="Import every manual decision from market_links.jsonl")
    seed.add_argument("--data-dir", type=Path, default=None)
    seed.add_argument("--cases", type=Path, default=DEFAULT_CASES)

    sample = sub.add_parser("sample", help="Queue unlabeled candidate pairs for hand labeling")
    sample.add_argument("--data-dir", type=Path, default=None)
    sample.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    sample.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    sample.add_argument("--size", type=int, default=50)
    sample.add_argument("--seed", type=int, default=20260928)

    label = sub.add_parser("label", help="Label queued pairs interactively")
    label.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    label.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)

    run = sub.add_parser("run", help="Score a matcher against the labeled cases")
    run.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    run.add_argument("--matcher", choices=sorted(MATCHERS), default="tfidf")
    run.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    run.add_argument("--name", default="tfidf-baseline")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    """Expected failures (missing inputs, a bad case file, an unexpected payload,
    the network) print one line and exit 1. A TF-IDF parity AssertionError is a
    bug, not user error, so it keeps its full traceback."""
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return _dispatch(args)
    except (ValueError, RulesFieldError) as exc:
        log.error("%s", exc)
    except httpx.HTTPError as exc:
        log.error("network error while fetching market rules: %s", exc)
    return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "run":
        cases = load_cases(args.cases)
        if not cases:
            log.error("no labeled cases in %s — run seed/sample/label first", args.cases)
            return 1
        report = evaluate(cases, MATCHERS[args.matcher](), cases_path=args.cases)
        _, md_path = write_report(report, args.reports_dir, args.name)
        m = report["metrics"]
        log.info(
            "eval matcher=%s cases=%d approved=%d precision=%s recall=%s report=%s",
            args.matcher, m["cases"], m["approved"], _pct(m["precision"]), _pct(m["recall"]),
            md_path,
        )
        return 0

    from . import matching_cases

    data_dir: Path = getattr(args, "data_dir", None) or _default_data_dir()
    if args.command == "verify-fields":
        return matching_cases.verify_fields(
            data_dir, kalshi_ticker=args.kalshi_ticker,
            polymarket_condition=args.polymarket_condition,
        )
    if args.command == "seed":
        matching_cases.seed_from_market_links(data_dir, args.cases)
        return 0
    if args.command == "sample":
        matching_cases.sample_candidates(
            data_dir, cases_path=args.cases, queue_path=args.queue,
            size=args.size, seed=args.seed,
        )
        return 0
    if args.command == "label":
        matching_cases.label_queue(args.queue, args.cases)
        return 0
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())


__all__ = [
    "CaseFileError", "CaseLabel", "CaseMarket", "Decision", "MatchCase", "Matcher",
    "TfidfReplayMatcher", "evaluate", "load_cases", "make_case_id", "render_markdown",
    "validate_reason", "wilson_interval", "write_cases_atomic", "write_report",
]
