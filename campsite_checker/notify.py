import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from typing import FrozenSet, List, Optional, Set, Tuple

from camply.containers import AvailableCampsite

from .results import filter_results, get_facility_name, group_results


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
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"  [WARNING] Telegram notification failed: {exc}", file=sys.stderr)
        print(f"  [DEBUG] Response body: {body}", file=sys.stderr)
        print(f"  [DEBUG] Sent payload: {payload.decode()}", file=sys.stderr)
    except Exception as exc:
        print(f"  [WARNING] Telegram notification failed: {exc}", file=sys.stderr)


_MAX_TG_LEN = 4096


def build_telegram_message(
    entries_with_results: List[Tuple[dict, List[AvailableCampsite]]],
    day_filter: Optional[Set[int]],
) -> List[str]:
    """Format Telegram HTML messages for found availability.

    Returns a list of messages, each within Telegram's 4096-character limit.
    Campground sections are kept intact; a new message is started whenever
    adding the next section would exceed the limit.
    """
    header = "\U0001f3d5 <b>Campsite Availability Found!</b>"

    sections: List[str] = []
    for entry, results in entries_with_results:
        grouped = group_results(results, day_filter)
        if grouped is None:
            continue
        name, by_date, total, url = grouped
        safe_name = html.escape(name)

        lines = [f"\n<b>{safe_name}</b> — {total} open site(s)"]
        for d in sorted(by_date):
            count = len(by_date[d])
            lines.append(f"  \U0001f4c5 {d.strftime('%a, %b %-d')}: {count} site(s)")
        if url:
            safe_url = html.escape(url)
            lines.append(f'  \U0001f517 <a href="{safe_url}">Book now</a>')
        sections.append("\n".join(lines))

    if not sections:
        return []

    messages: List[str] = []
    current_parts: List[str] = [header]
    current_len = len(header)

    for section in sections:
        # Each section is joined to the rest with a single "\n" separator.
        needed = 1 + len(section)
        if len(current_parts) > 1 and current_len + needed > _MAX_TG_LEN:
            messages.append("\n".join(current_parts))
            current_parts = [header, section]
            current_len = len(header) + needed
        else:
            current_parts.append(section)
            current_len += needed

    messages.append("\n".join(current_parts))
    return messages


def result_keys(
    entry: dict, results: List[AvailableCampsite], day_filter: Optional[Set[int]]
) -> FrozenSet[Tuple[str, int, date]]:
    """Return a frozenset of (name, campsite_id, booking_date) for deduplication."""
    filtered = filter_results(results, day_filter)
    name = get_facility_name(filtered) if filtered else "Unknown"
    return frozenset((name, r.campsite_id, r.booking_date.date()) for r in filtered)


def filter_new_results(
    entry: dict,
    results: List[AvailableCampsite],
    day_filter: Optional[Set[int]],
    prev_keys: Set[Tuple[str, int, date]],
) -> List[AvailableCampsite]:
    """Return only results whose keys are not in *prev_keys*."""
    filtered = filter_results(results, day_filter)
    name = get_facility_name(filtered) if filtered else "Unknown"
    return [r for r in filtered if (name, r.campsite_id, r.booking_date.date()) not in prev_keys]
