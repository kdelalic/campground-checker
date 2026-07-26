import json
import re
from pathlib import Path

from campsite_checker.server import _ScanStatus

DASHBOARD_PATH = Path(__file__).parents[1] / "grafana" / "campground-checker.json"
METRIC_PATTERN = re.compile(r"\bcampsite_checker_[a-zA-Z0-9_:]+\b")


def _dashboard_queries(value):
    if isinstance(value, dict):
        if isinstance(value.get("expr"), str):
            yield value["expr"]
        for child in value.values():
            yield from _dashboard_queries(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dashboard_queries(child)


def test_dashboard_references_exported_metrics():
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    referenced_metrics = {
        metric
        for query in _dashboard_queries(dashboard)
        for metric in METRIC_PATTERN.findall(query)
    }
    exported_metrics = set(
        re.findall(
            r"^# TYPE (campsite_checker_[a-zA-Z0-9_:]+) ",
            _ScanStatus().to_prometheus(),
            re.MULTILINE,
        )
    )

    assert referenced_metrics
    assert referenced_metrics <= exported_metrics


def test_dashboard_uses_canonical_identity_and_datasource():
    dashboard = json.loads(DASHBOARD_PATH.read_text())

    assert dashboard["uid"] == "homelab-campground-checker"
    assert dashboard["title"] == "Campground Checker"

    datasources = {
        value["datasource"]["uid"]
        for value in dashboard["panels"]
        if isinstance(value.get("datasource"), dict)
    }
    assert datasources == {"prometheus"}
