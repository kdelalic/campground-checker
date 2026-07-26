import html
import json
import logging
import os
import time
import urllib.error
import urllib.request

from camply.containers import AvailableCampsite

from .results import (
    NotificationKey,
    ProcessedAvailability,
    process_filtered_results,
    process_results,
)

logger = logging.getLogger(__name__)


def get_telegram_creds(args, config: dict) -> tuple[str | None, str | None]:
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


def send_telegram(token: str, chat_id: str, text: str, max_retries: int = 3) -> None:
    """Send a message via the Telegram Bot API (HTML parse mode).

    Retries on rate-limiting (HTTP 429) and transient network errors.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
    ).encode()

    for attempt in range(max_retries):
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries - 1:
                delay = 2**attempt
                logger.warning("Telegram rate-limited, retrying in %ds...", delay)
                time.sleep(delay)
                continue
            body = exc.read().decode("utf-8", errors="replace")
            logger.warning("Telegram notification failed: %s", exc)
            logger.debug("Response body: %s", body)
            return  # Non-retryable HTTP error
        except Exception as exc:
            if attempt < max_retries - 1:
                delay = 2**attempt
                logger.warning("Telegram send failed, retrying in %ds: %s", delay, exc)
                time.sleep(delay)
                continue
            logger.warning("Telegram notification failed after %d attempts: %s", max_retries, exc)


_MAX_TG_LEN = 4096


def build_telegram_message(
    entries_with_results: list[tuple[dict, list[AvailableCampsite]]],
    day_filter: set[int] | None,
) -> list[str]:
    """Format Telegram HTML messages for found availability.

    Returns a list of messages, each within Telegram's 4096-character limit.
    Campground sections are kept intact; a new message is started whenever
    adding the next section would exceed the limit.
    """
    processed = [
        process_results(entry, results, day_filter) for entry, results in entries_with_results
    ]
    return build_processed_telegram_message(processed)


def build_processed_telegram_message(
    availabilities: list[ProcessedAvailability],
) -> list[str]:
    """Format normalized availability as Telegram HTML messages."""
    header = "\U0001f3d5 <b>Campsite Availability Found!</b>"
    sections: list[str] = []
    for availability in availabilities:
        if not availability.available:
            continue
        safe_name = html.escape(availability.facility_name)

        lines = [f"\n<b>{safe_name}</b> — {availability.total_sites} open site(s)"]
        for d in sorted(availability.campsite_ids_by_date):
            count = len(availability.campsite_ids_by_date[d])
            lines.append(f"  \U0001f4c5 {d.strftime('%a, %b %-d')}: {count} site(s)")
        if availability.booking_url:
            safe_url = html.escape(availability.booking_url)
            lines.append(f'  \U0001f517 <a href="{safe_url}">Book now</a>')
        sections.append("\n".join(lines))

    if not sections:
        return []

    messages: list[str] = []
    current_parts: list[str] = [header]
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
    entry: dict, results: list[AvailableCampsite], day_filter: set[int] | None
) -> frozenset[NotificationKey]:
    """Return a frozenset of (name, campsite_id, booking_date) for deduplication."""
    return process_results(entry, results, day_filter).notification_keys


def filter_new_availability(
    availability: ProcessedAvailability,
    prev_keys: set[NotificationKey],
) -> ProcessedAvailability:
    """Return a normalized availability containing only previously unseen results."""
    if not availability.entry.get("alert", False):
        return process_filtered_results(availability.entry, [])
    new_results = [
        result
        for result in availability.campsites
        if (
            availability.facility_name,
            result.campsite_id,
            result.booking_date.date(),
        )
        not in prev_keys
    ]
    return process_filtered_results(availability.entry, new_results)


def filter_new_results(
    entry: dict,
    results: list[AvailableCampsite],
    day_filter: set[int] | None,
    prev_keys: set[NotificationKey],
) -> list[AvailableCampsite]:
    """Return only results whose keys are not in *prev_keys*.

    Returns an empty list if the entry has alert disabled (``alert: false``).
    """
    availability = process_results(entry, results, day_filter)
    return list(filter_new_availability(availability, prev_keys).campsites)
