"""Read-only YAML comment parsing for campsite config files using ruamel.yaml.

``campsites.yaml`` is git-managed and mounted read-only in deployments, so
this module only recovers campground names from inline comments; it never
writes the file.
"""

import logging

from ruamel.yaml import YAML

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
                if not comment:
                    continue
                try:
                    names[(str(provider), int(cid))] = comment
                except (TypeError, ValueError):
                    continue  # Non-numeric or list ID: skip just this entry
    except Exception:
        pass
    return names
