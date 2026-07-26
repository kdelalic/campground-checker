import json
import re
from pathlib import Path

from campsite_checker.server import _ScanStatus

DASHBOARD_PATH = Path(__file__).parents[1] / "grafana" / "campground-checker.json"
METRIC_PATTERN = re.compile(r"\bcampsite_checker_[a-zA-Z0-9_:]+\b")
PER_CAMPGROUND_METRICS = {
    "campsite_checker_campground_available",
    "campsite_checker_campground_campsites_available",
    "campsite_checker_campground_last_scan_success",
}


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


def test_dashboard_visualizes_per_campground_metrics():
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    queries = list(_dashboard_queries(dashboard))
    referenced_metrics = {metric for query in queries for metric in METRIC_PATTERN.findall(query)}

    assert PER_CAMPGROUND_METRICS <= referenced_metrics

    variables = {variable["name"] for variable in dashboard["templating"]["list"]}
    assert {"provider", "alert"} <= variables

    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    expected_panels = {
        "Availability by Campground",
        "Failed Campground Searches",
        "Campgrounds with Search Errors",
        "Availability Events",
    }
    assert expected_panels <= panels.keys()

    detail_panels = {
        "Availability by Campground",
        "Campgrounds with Search Errors",
        "Availability Events",
    }
    for title in detail_panels:
        panel = panels[title]
        assert all(target["legendFormat"] == "{{name}}" for target in panel["targets"])
        assert all("$provider" in target["expr"] for target in panel["targets"])
        assert all("$alert" in target["expr"] for target in panel["targets"])

    assert panels["Availability by Campground"]["type"] == "table"
    assert panels["Campgrounds with Search Errors"]["type"] == "table"
    assert panels["Availability Events"]["type"] == "state-timeline"
    assert "== 1" in panels["Availability Events"]["targets"][0]["expr"]


def test_dashboard_visualizes_adaptive_provider_throttling():
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    event_panel = panels["Provider Rate-limit Events"]
    cooldown_panel = panels["Adaptive Provider Cooldown"]

    assert event_panel["type"] == "timeseries"
    assert cooldown_panel["type"] == "timeseries"
    assert "campsite_checker_provider_rate_limit_events_total" in event_panel["targets"][0]["expr"]
    assert (
        "campsite_checker_provider_throttle_cooldown_seconds"
        in cooldown_panel["targets"][0]["expr"]
    )
    assert "$provider" in event_panel["targets"][0]["expr"]
    assert "$provider" in cooldown_panel["targets"][0]["expr"]


def test_dashboard_uses_dedicated_alert_scan_timestamp():
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    panel = panels["Last Alert Scan Age"]

    assert (
        panel["targets"][0]["expr"]
        == "time() - max(campsite_checker_last_alert_scan_timestamp_seconds)"
    )
    thresholds = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert thresholds[-1] == {"color": "red", "value": 120}


def test_dashboard_visualizes_background_worker_metrics():
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    assert "Scan Duration" not in panels
    assert panels["Alert Cycle Duration"]["type"] == "timeseries"

    worker = panels["Dashboard Worker"]
    duration = panels["Dashboard Scan Duration"]
    activity = panels["Dashboard Scan Throughput and Errors"]
    assert worker["type"] == "stat"
    assert duration["type"] == "timeseries"
    assert activity["type"] == "timeseries"
    assert "campsite_checker_dashboard_scan_in_progress" in worker["targets"][0]["expr"]
    assert "campsite_checker_last_dashboard_scan_duration_seconds" in duration["targets"][0]["expr"]
    activity_queries = {target["expr"] for target in activity["targets"]}
    assert any("campsite_checker_dashboard_scans_total" in query for query in activity_queries)
    assert any(
        "campsite_checker_dashboard_scan_errors_total" in query for query in activity_queries
    )

    age_panel = panels["Last Dashboard Scan Age"]
    thresholds = age_panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert thresholds[-2:] == [
        {"color": "yellow", "value": 900},
        {"color": "red", "value": 1200},
    ]
