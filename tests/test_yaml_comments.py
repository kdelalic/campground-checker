"""Tests for campsite_checker.yaml_comments (read-only comment parsing)."""

import pytest

from campsite_checker.yaml_comments import parse_yaml_comments

SAMPLE_YAML = """\
campsites:
  RecreationDotGov:
    - campground_id: 12345  # Upper Pines
      alert: true
    - campground_id: 67890
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "campsites.yaml"
    path.write_text(SAMPLE_YAML)
    return str(path)


class TestParseYamlComments:
    def test_extracts_comments(self, config_file):
        names = parse_yaml_comments(config_file)
        assert names.get(("RecreationDotGov", 12345)) == "Upper Pines"

    def test_no_comment_returns_empty(self, config_file):
        names = parse_yaml_comments(config_file)
        assert ("RecreationDotGov", 67890) not in names

    def test_handles_missing_file(self):
        names = parse_yaml_comments("/nonexistent/path.yaml")
        assert names == {}

    def test_handles_non_numeric_campground_id(self, tmp_path):
        path = tmp_path / "campsites.yaml"
        path.write_text(
            "campsites:\n"
            "  RecreationDotGov:\n"
            "    - campground_id: abc  # Weird\n"
            "    - campground_id: 111  # Kept\n"
        )
        names = parse_yaml_comments(str(path))
        assert names.get(("RecreationDotGov", 111)) == "Kept"
