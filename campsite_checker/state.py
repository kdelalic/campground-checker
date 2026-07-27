"""On-disk persistence for Telegram dedup keys.

Keeps already-alerted availability from re-alerting across restarts. The keys
themselves are defined in `results.py`; this module only stores them.
"""

import json
import logging
import os
from datetime import date
from pathlib import Path

from .results import NotificationKey

logger = logging.getLogger(__name__)

SENT_KEYS_FILE = Path(os.environ.get("SENT_KEYS_PATH", ".campsite_sent_keys.json"))


def load_sent_keys(path: Path = SENT_KEYS_FILE) -> set[NotificationKey]:
    """Load previously sent keys from disk, pruning past booking dates.

    Keys are kept for any future booking date so a restart cannot re-alert
    availability that was already notified anywhere in the search window.
    """
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        today = date.today()
        keys = set()
        for provider, ident, cid, d in data:
            dt = date.fromisoformat(d)
            if dt >= today:
                keys.add((provider, ident, cid, dt))
        return keys
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.warning(
            "Discarding unreadable sent-key state at %s; previously alerted "
            "availability may be re-alerted once",
            path,
        )
        return set()


def save_sent_keys(path: Path, keys: set[NotificationKey]) -> bool:
    """Atomically save changed sent keys to disk, pruning past booking dates."""
    today = date.today()
    data = [[provider, ident, cid, d.isoformat()] for provider, ident, cid, d in keys if d >= today]
    data.sort(key=lambda row: (row[0], row[1], str(row[2]), row[3]))
    serialized = json.dumps(data, separators=(",", ":"))
    try:
        if path.read_text() == serialized:
            return False
    except (FileNotFoundError, OSError):
        pass
    try:
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(serialized)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning("Could not persist sent-key state to %s: %s", path, exc)
        return False
    return True
