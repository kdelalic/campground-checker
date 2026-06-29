"""Tests for campsite_checker.yaml_editor."""

import pytest

from campsite_checker.yaml_editor import (
    append_campground,
    parse_yaml_comments,
    remove_campground,
    update_alert_field,
    update_campground_comment,
)

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


class TestAppendCampground:
    def test_add_to_existing_provider(self, config_file):
        append_campground(config_file, "RecreationDotGov", 99999, name="New Camp")
        with open(config_file) as f:
            content = f.read()
        assert "99999" in content
        assert "New Camp" in content

    def test_add_to_new_provider(self, config_file):
        append_campground(config_file, "Yellowstone", 55555)
        with open(config_file) as f:
            content = f.read()
        assert "55555" in content
        assert "Yellowstone" in content

    def test_preserves_existing_entries(self, config_file):
        append_campground(config_file, "RecreationDotGov", 99999)
        with open(config_file) as f:
            content = f.read()
        assert "12345" in content
        assert "67890" in content


class TestRemoveCampground:
    def test_remove_existing(self, config_file):
        remove_campground(config_file, "RecreationDotGov", 12345)
        with open(config_file) as f:
            content = f.read()
        assert "12345" not in content
        assert "67890" in content

    def test_remove_nonexistent_is_noop(self, config_file):
        remove_campground(config_file, "RecreationDotGov", 99999)
        with open(config_file) as f:
            content = f.read()
        assert "12345" in content
        assert "67890" in content


class TestUpdateAlertField:
    def test_set_alert_true(self, config_file):
        update_alert_field(config_file, "RecreationDotGov", 67890, True)
        with open(config_file) as f:
            content = f.read()
        # 67890 should now have alert: true
        lines = content.split("\n")
        found = False
        for line in lines:
            if "67890" in line:
                found = True
        assert found
        # Check that alert: true appears after 67890
        assert "alert: true" in content

    def test_set_alert_false(self, config_file):
        update_alert_field(config_file, "RecreationDotGov", 12345, False)
        with open(config_file) as f:
            content = f.read()
        assert "alert: false" in content


class TestUpdateCampgroundComment:
    def test_sets_comment_when_none(self, config_file):
        update_campground_comment(config_file, "RecreationDotGov", 67890, "Madison")
        with open(config_file) as f:
            content = f.read()
        assert "Madison" in content

    def test_does_not_overwrite_existing_comment(self, config_file):
        update_campground_comment(config_file, "RecreationDotGov", 12345, "Should Not Set")
        with open(config_file) as f:
            content = f.read()
        assert "Should Not Set" not in content
        assert "Upper Pines" in content


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
