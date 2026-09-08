"""Guards that keep ops/grafana/dashboard.json wired to real metrics.

Same failure mode as the alert rules, different artifact: a dashboard panel
querying a metric nobody exports renders an empty graph, which reads as "the
system is quiet" rather than "this panel is broken". On a dashboard whose whole
job is making silent failures visible, a silently broken panel is the worst
possible defect.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import marketsignalos_polymarket.metrics  # noqa: F401
import pytest
from prometheus_client import REGISTRY, generate_latest

import marketsignalos_api.observability.metrics  # noqa: F401

DASHBOARD_PATH = (
    Path(__file__).resolve().parents[3] / "ops" / "grafana" / "dashboard.json"
)

_SUFFIXES_BY_TYPE = {
    "counter": ("", "_total", "_created"),
    "gauge": ("",),
    "histogram": ("", "_bucket", "_count", "_sum", "_created"),
    "summary": ("", "_count", "_sum", "_created"),
}


def _exported_metric_names() -> set[str]:
    names: set[str] = set()
    for line in generate_latest(REGISTRY).decode("utf-8").splitlines():
        if not line.startswith("# TYPE "):
            continue
        _, _, family, metric_type = line.split(" ", 3)
        for suffix in _SUFFIXES_BY_TYPE.get(metric_type, ("",)):
            names.add(f"{family}{suffix}")
    return names


def _dashboard() -> dict[str, Any]:
    payload = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _panels() -> list[dict[str, Any]]:
    return [p for p in _dashboard()["panels"] if p.get("type") != "row"]


def test_dashboard_parses() -> None:
    dashboard = _dashboard()
    assert dashboard["uid"]
    assert dashboard["title"]
    assert _panels(), "dashboard has no panels"


@pytest.mark.parametrize("panel", _panels(), ids=lambda p: str(p["title"]))
def test_every_panel_queries_exported_metrics(panel: dict[str, Any]) -> None:
    exported = _exported_metric_names()
    referenced: set[str] = set()
    for query in panel.get("targets", []):
        referenced.update(re.findall(r"\bmsos_[a-z0-9_]+", query["expr"]))
    assert referenced, f"panel {panel['title']!r} queries no msos_ metric"
    missing = sorted(referenced - exported)
    assert not missing, (
        f"panel {panel['title']!r} queries metrics that nothing exports: {missing}. "
        "The panel would render empty, which reads as a quiet system rather than "
        "a broken graph."
    )


@pytest.mark.parametrize("panel", _panels(), ids=lambda p: str(p["title"]))
def test_every_panel_explains_itself(panel: dict[str, Any]) -> None:
    """A panel without a description is a number nobody can act on at 3am."""
    assert panel.get("description", "").strip(), f"{panel['title']} has no description"


def test_panels_do_not_overlap() -> None:
    """Grafana repacks overlapping panels on load, silently scrambling a layout
    that looked fine in review."""
    placed: list[tuple[str, dict[str, int]]] = []
    for panel in _dashboard()["panels"]:
        grid = panel["gridPos"]
        assert grid["x"] + grid["w"] <= 24, f"{panel['title']} runs off the grid"
        for other_title, other in placed:
            overlaps_x = grid["x"] < other["x"] + other["w"] and other["x"] < grid["x"] + grid["w"]
            overlaps_y = grid["y"] < other["y"] + other["h"] and other["y"] < grid["y"] + grid["h"]
            assert not (overlaps_x and overlaps_y), (
                f"{panel['title']!r} overlaps {other_title!r}"
            )
        placed.append((str(panel["title"]), grid))


def test_alerting_metrics_are_all_visualized() -> None:
    """Every metric an alert fires on should be inspectable on the dashboard.

    When an alert pages, the first move is looking at the graph. A metric that
    can page but has nowhere to look is a gap in the loop, not just a missing
    panel.
    """
    import yaml

    alerts = yaml.safe_load(
        (DASHBOARD_PATH.parent.parent / "prometheus" / "alerts.yml").read_text(
            encoding="utf-8"
        )
    )
    alerted: set[str] = set()
    for group in alerts["groups"]:
        for rule in group["rules"]:
            alerted.update(re.findall(r"\bmsos_[a-z0-9_]+", rule["expr"]))

    charted: set[str] = set()
    for panel in _panels():
        for query in panel.get("targets", []):
            charted.update(re.findall(r"\bmsos_[a-z0-9_]+", query["expr"]))

    def base(name: str) -> str:
        for suffix in ("_bucket", "_count", "_sum", "_created", "_total"):
            if name.endswith(suffix):
                return name[: -len(suffix)]
        return name

    missing = sorted({base(m) for m in alerted} - {base(m) for m in charted})
    assert not missing, (
        f"these metrics can fire an alert but appear on no panel: {missing}"
    )
