import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import date
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from camply.containers import AvailableCampsite

from .results import filter_results, get_booking_url


def get_telegram_creds(
    args, config: dict
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve token and chat_id with priority: CLI args > env vars > YAML config."""
    tg_cfg = config.get("telegram") or {}
    token = (
        getattr(args, "telegram_token", None)
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or tg_cfg.get("bot_token")
    )
    chat_id = (
        getattr(args, "telegram_chat_id", None)
        or os.environ.get("TELEGRAM_CHAT_ID")
        or tg_cfg.get("chat_id")
    )
    return token, str(chat_id) if chat_id is not None else None


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Send a message via the Telegram Bot API (HTML parse mode)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        print(f"  [WARNING] Telegram notification failed: {exc}", file=sys.stderr)


def build_telegram_message(
    entries_with_results: List[Tuple[dict, List[AvailableCampsite]]],
    day_filter: Optional[Set[int]],
) -> str:
    """Format a Telegram HTML message for found availability."""
    parts = ["\U0001f3d5 <b>Campsite Availability Found!</b>"]
    for entry, results in entries_with_results:
        results = filter_results(results, day_filter)
        if not results:
            continue
        name = entry.get("name", "Unnamed")
        by_date: Dict[date, Set] = defaultdict(set)
        for r in results:
            by_date[r.booking_date.date()].add(r.campsite_id)
        total = sum(len(s) for s in by_date.values())
        url = get_booking_url(results)

        lines = [f"\n<b>{name}</b> \u2014 {total} open site(s)"]
        for d in sorted(by_date):
            count = len(by_date[d])
            lines.append(f"  \U0001f4c5 {d.strftime('%a, %b %-d')}: {count} site(s)")
        if url:
            lines.append(f'  \U0001f517 <a href="{url}">Book now</a>')
        parts.append("\n".join(lines))
    return "\n".join(parts)


def result_keys(
    entry: dict, results: List[AvailableCampsite], day_filter: Optional[Set[int]]
) -> FrozenSet[Tuple[str, int, date]]:
    """Return a frozenset of (name, campsite_id, booking_date) for deduplication."""
    filtered = filter_results(results, day_filter)
    name = entry.get("name", "Unnamed")
    return frozenset((name, r.campsite_id, r.booking_date.date()) for r in filtered)
