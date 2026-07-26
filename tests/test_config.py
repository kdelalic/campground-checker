"""Tests for campsite_checker.config."""

import argparse
from datetime import date, timedelta

import pytest

from campsite_checker.config import (
    compute_date_range,
    expand_search_tasks,
    load_config,
    parse_args,
    parse_day_names,
    resolve_day_filter,
    resolve_entry_day_filter,
)


class TestParseArgs:
    def test_search_tuning_defaults(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["check_campsites.py"])

        args = parse_args()

        assert args.workers == 4
        assert args.batch_size == 4
        assert args.search_delay == 0.0

    def test_search_tuning_overrides(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            [
                "check_campsites.py",
                "--workers",
                "6",
                "--batch-size",
                "2",
                "--search-delay",
                "0.5",
            ],
        )

        args = parse_args()

        assert args.workers == 6
        assert args.batch_size == 2
        assert args.search_delay == 0.5


# ── parse_day_names ──────────────────────────────────────────────────────────


class TestParseDayNames:
    def test_single_valid(self):
        assert parse_day_names(["Saturday"]) == {5}

    def test_multiple_valid(self):
        assert parse_day_names(["Monday", "Friday"]) == {0, 4}

    def test_case_insensitive(self):
        assert parse_day_names(["saturday", "SUNDAY"]) == {5, 6}

    def test_empty_list_returns_none(self):
        assert parse_day_names([]) is None

    def test_invalid_name_exits(self):
        with pytest.raises(SystemExit):
            parse_day_names(["Funday"])

    def test_none_input_returns_none(self):
        assert parse_day_names(None) is None


# ── resolve_day_filter ──────────────────────────────────────────────────────


def _args(**kwargs):
    """Create a mock argparse.Namespace with sensible defaults."""
    defaults = dict(all_days=False, day=None, start=None, end=None)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestResolveDayFilter:
    def test_all_days_returns_none(self):
        assert resolve_day_filter(_args(all_days=True)) is None

    def test_day_flag(self):
        assert resolve_day_filter(_args(day=["Saturday"])) == {5}

    def test_day_multiple(self):
        assert resolve_day_filter(_args(day=["Friday", "Saturday"])) == {4, 5}

    def test_default_sunday(self):
        assert resolve_day_filter(_args()) == {6}

    def test_config_default_days(self):
        config = {"_default_day_filter": {0}}  # Monday
        assert resolve_day_filter(_args(), config=config) == {0}

    def test_day_flag_overrides_config(self):
        config = {"_default_day_filter": {0}}
        assert resolve_day_filter(_args(day=["Saturday"]), config=config) == {5}

    def test_all_days_overrides_everything(self):
        config = {"_default_day_filter": {0}}
        assert resolve_day_filter(_args(all_days=True), config=config) is None


# ── resolve_entry_day_filter ────────────────────────────────────────────────


class TestResolveEntryDayFilter:
    def test_entry_filter_overrides_global(self):
        entry = {"_day_filter": {4}}
        assert resolve_entry_day_filter(entry, {5}) == {4}

    def test_falls_back_to_global(self):
        entry = {"_day_filter": None}
        assert resolve_entry_day_filter(entry, {5}) == {5}

    def test_entry_filter_none_and_global_none(self):
        entry = {"_day_filter": None}
        assert resolve_entry_day_filter(entry, None) is None


# ── expand_search_tasks ─────────────────────────────────────────────────────


class TestExpandSearchTasks:
    def test_simple_entries_no_criteria(self):
        entries = [
            {"campground_id": 1, "_day_filter": None, "_criteria": None},
            {"campground_id": 2, "_day_filter": None, "_criteria": None},
        ]
        tasks = expand_search_tasks(entries, {5})
        assert len(tasks) == 2
        assert tasks[0][0] == 0  # original index
        assert tasks[1][0] == 1

    def test_entry_with_criteria_expands(self):
        entries = [
            {
                "campground_id": 1,
                "_day_filter": None,
                "_criteria": [
                    {"_day_filter": {5}, "nights": 2},
                    {"_day_filter": {6}, "nights": 3},
                ],
            },
        ]
        tasks = expand_search_tasks(entries, None)
        assert len(tasks) == 2
        # First criterion uses its own day filter
        assert tasks[0][2] == {5}
        assert tasks[0][1]["nights"] == 2
        # Second criterion uses its own day filter
        assert tasks[1][2] == {6}
        assert tasks[1][1]["nights"] == 3

    def test_criteria_falls_back_to_global(self):
        entries = [
            {
                "campground_id": 1,
                "_day_filter": None,
                "_criteria": [
                    {"_day_filter": None, "nights": 2},
                ],
            },
        ]
        tasks = expand_search_tasks(entries, {5})
        assert len(tasks) == 1
        assert tasks[0][2] == {5}

    def test_mixed_criteria_and_simple(self):
        entries = [
            {
                "campground_id": 1,
                "_day_filter": None,
                "_criteria": [
                    {"_day_filter": {5}, "nights": 2},
                    {"_day_filter": {6}, "nights": 3},
                ],
            },
            {"campground_id": 2, "_day_filter": None, "_criteria": None},
        ]
        tasks = expand_search_tasks(entries, None)
        assert len(tasks) == 3
        assert tasks[0][0] == 0  # first entry, first criterion
        assert tasks[1][0] == 0  # first entry, second criterion
        assert tasks[2][0] == 1  # second entry


# ── compute_date_range ──────────────────────────────────────────────────────


class TestComputeDateRange:
    def test_explicit_dates(self):
        args = _args(start="2026-06-01", end="2026-08-31")
        start, end = compute_date_range(args)
        assert start.strftime("%Y-%m-%d") == "2026-06-01"
        assert end.strftime("%Y-%m-%d") == "2026-08-31"

    def test_invalid_start(self):
        args = _args(start="not-a-date", end="2026-08-31")
        with pytest.raises(SystemExit):
            compute_date_range(args)

    def test_invalid_end(self):
        args = _args(start="2026-06-01", end="bad")
        with pytest.raises(SystemExit):
            compute_date_range(args)

    def test_end_before_start(self):
        args = _args(start="2026-08-31", end="2026-06-01")
        with pytest.raises(SystemExit):
            compute_date_range(args)

    def test_default_end_is_about_6_months(self):
        args = _args()
        start, end = compute_date_range(args)
        today = date.today()
        assert start.date() == today
        expected_end = today + timedelta(days=181)
        assert end.date() == expected_end


# ── load_config ─────────────────────────────────────────────────────────────


DICT_CONFIG = """
campsites:
  RecreationDotGov:
    - campground_id: 12345
      alert: true
    - campground_id: 67890
      enabled: false
"""

LIST_CONFIG = """
campsites:
  - campground_id: 12345
    provider: Yellowstone
    alert: true
"""

EMPTY_CONFIG = """
campsites: {}
"""

NO_CAMPSITES_KEY = """
defaults:
  nights: 2
"""


class TestLoadConfig:
    def test_dict_format(self, tmp_path):
        path = tmp_path / "campsites.yaml"
        path.write_text(DICT_CONFIG)
        entries, config = load_config(str(path))
        assert len(entries) == 1  # one disabled entry filtered out
        assert entries[0]["campground_id"] == 12345
        assert entries[0]["provider"] == "RecreationDotGov"
        assert entries[0]["alert"] is True

    def test_list_format(self, tmp_path):
        path = tmp_path / "campsites.yaml"
        path.write_text(LIST_CONFIG)
        entries, config = load_config(str(path))
        assert len(entries) == 1
        assert entries[0]["provider"] == "Yellowstone"

    def test_disabled_entries_filtered(self, tmp_path):
        path = tmp_path / "campsites.yaml"
        path.write_text(DICT_CONFIG)
        entries, _ = load_config(str(path))
        assert all(e.get("campground_id") != 67890 for e in entries)

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            load_config(str(tmp_path / "nonexistent.yaml"))

    def test_invalid_yaml_exits(self, tmp_path):
        path = tmp_path / "campsites.yaml"
        path.write_text("campsites: [invalid yaml ][")
        with pytest.raises(SystemExit):
            load_config(str(path))

    def test_no_campsites_key_exits(self, tmp_path):
        path = tmp_path / "campsites.yaml"
        path.write_text(NO_CAMPSITES_KEY)
        with pytest.raises(SystemExit):
            load_config(str(path))

    def test_empty_entries_exits(self, tmp_path):
        path = tmp_path / "campsites.yaml"
        path.write_text(EMPTY_CONFIG)
        with pytest.raises(SystemExit):
            load_config(str(path))

    def test_defaults_nights_applied(self, tmp_path):
        config_text = """
defaults:
  nights: 3
campsites:
  RecreationDotGov:
    - campground_id: 12345
"""
        path = tmp_path / "campsites.yaml"
        path.write_text(config_text)
        entries, _ = load_config(str(path))
        assert entries[0]["nights"] == 3

    def test_unknown_provider_exits(self, tmp_path):
        config_text = """
campsites:
  FakeProvider:
    - campground_id: 1
"""
        path = tmp_path / "campsites.yaml"
        path.write_text(config_text)
        with pytest.raises(SystemExit):
            load_config(str(path))

    def test_criteria_parsed(self, tmp_path):
        config_text = """
campsites:
  RecreationDotGov:
    - campground_id: 12345
      criteria:
        - days: [Saturday]
          nights: 2
        - days: [Sunday]
          nights: 3
"""
        path = tmp_path / "campsites.yaml"
        path.write_text(config_text)
        entries, _ = load_config(str(path))
        assert entries[0]["_criteria"] is not None
        assert len(entries[0]["_criteria"]) == 2
        assert entries[0]["_criteria"][0]["_day_filter"] == {5}
        assert entries[0]["_criteria"][0]["nights"] == 2

    def test_criteria_and_days_mutually_exclusive(self, tmp_path):
        config_text = """
campsites:
  RecreationDotGov:
    - campground_id: 12345
      criteria:
        - days: [Saturday]
      days: [Sunday]
"""
        path = tmp_path / "campsites.yaml"
        path.write_text(config_text)
        with pytest.raises(SystemExit):
            load_config(str(path))
