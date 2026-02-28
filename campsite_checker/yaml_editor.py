"""Round-trip YAML editing for campsite config files using ruamel.yaml.

Preserves comments, formatting, and ordering when modifying entries.
"""

import logging

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

logger = logging.getLogger(__name__)


def _load(path: str):
    """Load YAML with round-trip preservation. Returns (data, yaml_instance)."""
    yml = YAML(typ="rt")
    yml.preserve_quotes = True
    # Match the original file's indentation style:
    #   campsites:
    #     RecreationDotGov:           (mapping indent 2)
    #       - campground_id: 123     (sequence indent 4, dash offset 2)
    #         alert: true
    yml.best_map_representer = True
    yml.indent(mapping=2, sequence=4, offset=2)
    with open(path) as f:
        data = yml.load(f)
    return data, yml


def _save(path: str, data, yml):
    """Save YAML back to disk preserving comments and formatting."""
    with open(path, "w") as f:
        yml.dump(data, f)


def _find_entry(data, provider: str, campground_id: int):
    """Find a campground entry. Returns (items_list, index) or (items_list, None)."""
    items = data.get("campsites", {}).get(provider)
    if not items:
        return None, None
    for i, item in enumerate(items):
        if item.get("campground_id") == campground_id:
            return items, i
    return items, None


def _get_eol_comment(item, key: str) -> str | None:
    """Extract end-of-line comment text for a key in a CommentedMap."""
    try:
        tokens = item.ca.items[key]
        eol = tokens[2]
        if eol is not None:
            text = eol.value if hasattr(eol, "value") else str(eol)
            text = text.strip()
            if text.startswith("#"):
                text = text[1:].strip()
            return text if text else None
    except (AttributeError, KeyError, IndexError, TypeError):
        pass
    return None


def append_campground(
    path: str, provider: str, campground_id: int, name: str | None = None
) -> None:
    """Append a campground entry to the YAML config file."""
    data, yml = _load(path)
    campsites = data.setdefault("campsites", {})
    if provider not in campsites:
        campsites[provider] = []

    entry = CommentedMap({"campground_id": campground_id})
    if name:
        entry.yaml_add_eol_comment(name, key="campground_id")

    campsites[provider].append(entry)
    _save(path, data, yml)


def remove_campground(path: str, provider: str, campground_id: int) -> None:
    """Remove a campground entry from the YAML config file."""
    data, yml = _load(path)
    items, idx = _find_entry(data, provider, campground_id)
    if items is not None and idx is not None:
        del items[idx]
        _save(path, data, yml)


def update_campground_comment(
    path: str, provider: str, campground_id: int, name: str
) -> None:
    """Set the inline comment on a campground entry (only if none exists)."""
    data, yml = _load(path)
    items, idx = _find_entry(data, provider, campground_id)
    if items is not None and idx is not None:
        item = items[idx]
        if not _get_eol_comment(item, "campground_id"):
            item.yaml_add_eol_comment(name, key="campground_id")
            _save(path, data, yml)


def update_alert_field(
    path: str, provider: str, campground_id: int, alert: bool
) -> None:
    """Update the alert field for a campground entry."""
    data, yml = _load(path)
    items, idx = _find_entry(data, provider, campground_id)
    if items is not None and idx is not None:
        items[idx]["alert"] = alert
        _save(path, data, yml)


def parse_yaml_comments(path: str) -> dict[tuple[str, int], str]:
    """Extract inline comments from campground entries.

    Returns dict mapping (provider, campground_id) -> comment string.
    """
    names: dict[tuple[str, int], str] = {}
    try:
        data, _ = _load(path)
        campsites = data.get("campsites", {})
        if not isinstance(campsites, dict):
            return names
        for provider, items in campsites.items():
            if not isinstance(items, list):
                continue
            for item in items:
                cid = item.get("campground_id")
                if cid is None:
                    continue
                comment = _get_eol_comment(item, "campground_id")
                if comment:
                    names[(str(provider), int(cid))] = comment
    except Exception:
        pass
    return names
