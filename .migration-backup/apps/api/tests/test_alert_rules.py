"""Guards that keep ops/prometheus/alerts.yml wired to real metrics.

An alert referencing a metric nobody exports is worse than no alert: it looks
like coverage on a dashboard, evaluates to nothing forever, and fails exactly
when you need it. Renaming a collector is an ordinary refactor, so the link
between code and rules has to be checked by CI rather than remembered.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Both metrics modules are imported for their side effect: registering their
# collectors on the default registry, which is what the assertions below check
# the alert rules against.
import marketsignalos_polymarket.metrics  # noqa: F401
import pytest
import yaml
from prometheus_client import REGISTRY, generate_latest

import marketsignalos_api.observability.metrics  # noqa: F401

ALERTS_PATH = Path(__file__).resolve().parents[3] / "ops" / "prometheus" / "alerts.yml"

# Suffixes prometheus_client appends per metric type. A rule may legitimately
# reference any of them (histogram quantiles need _bucket, counters need
# _total), so each has to resolve back to a family that actually exists.
_SUFFIXES_BY_TYPE = {
    "counter": ("", "_total", "_created"),
    "gauge": ("",),
    "histogram": ("", "_bucket", "_count", "_sum", "_created"),
    "summary": ("", "_count", "_sum", "_created"),
}


def _exported_metric_names() -> set[str]:
    """Every name a scrape of /metrics could legally produce."""
    names: set[str] = set()
    for line in generate_latest(REGISTRY).decode("utf-8").splitlines():
        if not line.startswith("# TYPE "):
            continue
        _, _, family, metric_type = line.split(" ", 3)
        for suffix in _SUFFIXES_BY_TYPE.get(metric_type, ("",)):
            names.add(f"{family}{suffix}")
    return names


def _rules() -> list[dict[str, Any]]:
    document = yaml.safe_load(ALERTS_PATH.read_text(encoding="utf-8"))
    return [rule for group in document["groups"] for rule in group["rules"]]


def _referenced_metrics(expr: str) -> set[str]:
    return set(re.findall(r"\bmsos_[a-z0-9_]+", expr))


def test_alerts_file_parses() -> None:
    document = yaml.safe_load(ALERTS_PATH.read_text(encoding="utf-8"))
    assert document["groups"], "no alert groups defined"


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: str(r["alert"]))
def test_every_alert_references_only_exported_metrics(rule: dict[str, Any]) -> None:
    exported = _exported_metric_names()
    missing = sorted(_referenced_metrics(rule["expr"]) - exported)
    assert not missing, (
        f"alert {rule['alert']} references metrics that nothing exports: {missing}. "
        "Either the collector was renamed or the rule has a typo — both make the "
        "alert silently never fire."
    )


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: str(r["alert"]))
def test_every_alert_is_actionable(rule: dict[str, Any]) -> None:
    """Severity, a summary, and a runbook pointer. An alert that fires at 3am
    without saying what to look at is a page you learn to ignore."""
    assert rule.get("labels", {}).get("severity") in {"critical", "warning", "info"}
    annotations = rule.get("annotations", {})
    assert annotations.get("summary"), f"{rule['alert']} has no summary"
    assert annotations.get("runbook"), f"{rule['alert']} has no runbook pointer"


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: str(r["alert"]))
def test_every_alert_has_a_for_duration(rule: dict[str, Any]) -> None:
    """No instantaneous alerts: every rule rides out a single bad scrape."""
    assert rule.get("for"), f"{rule['alert']} would fire on one scrape"


def test_runbook_anchors_resolve() -> None:
    """Each runbook pointer must land on a real heading in the docs page."""
    doc = (
        Path(__file__).resolve().parents[3] / "docs" / "observability.md"
    ).read_text(encoding="utf-8")
    anchors = {
        re.sub(r"[^a-z0-9]", "", line.lstrip("#").strip().lower())
        for line in doc.splitlines()
        if line.startswith("#")
    }
    missing = []
    for rule in _rules():
        pointer = rule["annotations"]["runbook"]
        anchor = pointer.split("#", 1)[1] if "#" in pointer else ""
        if anchor and anchor not in anchors:
            missing.append((rule["alert"], anchor))
    assert not missing, f"runbook anchors with no matching heading: {missing}"


def test_incident_alerts_are_all_present() -> None:
    """The four post-mortems in docs/fix-lessons-learned.md are the reason this
    file exists. Each must keep at least one rule tagged with its date, so
    deleting the coverage is a deliberate act rather than an oversight."""
    tagged = {
        str(rule.get("labels", {}).get("incident"))
        for rule in _rules()
        if rule.get("labels", {}).get("incident")
    }
    assert {"2026-07-07", "2026-07-10", "2026-07-13", "2026-07-14"} <= tagged


def test_no_resolved_bets_alert_cannot_fire_before_enrichment_runs() -> None:
    """The wallets>0 conjunct is the difference between an alert that catches a
    six-week outage and one that fires on every deploy until someone mutes it.
    Pin it so a future simplification cannot quietly drop it."""
    rule = next(r for r in _rules() if r["alert"] == "EnrichmentProducesNoResolvedBets")
    assert "msos_enrichment_wallets > 0" in rule["expr"]


def test_stall_alert_is_gated_on_a_run_being_in_flight() -> None:
    """Without the running conjunct this fires forever on an idle deployment."""
    rule = next(r for r in _rules() if r["alert"] == "PipelineStalled")
    assert "msos_pipeline_running == 1" in rule["expr"]
