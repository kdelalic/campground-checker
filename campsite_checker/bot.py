import logging
import threading

import telebot

from . import yaml_editor
from .providers import PROVIDER_MAP

# Maps provider name to the camply provider class used for metadata lookups.
_CAMPLY_PROVIDER_CLASS = {}
try:
    from camply.providers.recreation_dot_gov.recdotgov_camps import (
        RecreationDotGov as _RecDotGovProvider,
    )

    _CAMPLY_PROVIDER_CLASS["RecreationDotGov"] = _RecDotGovProvider
except ImportError:
    pass
try:
    from camply.providers.usedirect.variations import (
        ReserveCalifornia as _ReserveCAProvider,
    )

    _CAMPLY_PROVIDER_CLASS["ReserveCalifornia"] = _ReserveCAProvider
except ImportError:
    pass

logger = logging.getLogger(__name__)


class ConfigState:
    """Thread-safe mutable config shared between the check loop and bot commands."""

    def __init__(
        self,
        entries: list[dict],
        raw_config: dict,
        config_path: str,
        chat_id: str,
    ):
        self.entries = entries
        self.raw_config = raw_config
        self.config_path = config_path
        self.chat_id = chat_id
        self.lock = threading.Lock()


def _authorized(message, state: ConfigState) -> bool:
    """Check if the message is from the authorized chat."""
    return str(message.chat.id) == state.chat_id


def _lookup_campground_names(
    entries: list[dict],
) -> dict[tuple[str, int], str]:
    """Look up campground names from camply providers.

    Returns dict mapping (provider, campground_id) -> facility name.
    """
    names: dict[tuple[str, int], str] = {}
    # Group IDs by provider
    by_provider: dict[str, list[int]] = {}
    for e in entries:
        cid = e.get("campground_id")
        if not cid:
            continue
        prov = e.get("provider", "RecreationDotGov")
        by_provider.setdefault(prov, []).append(cid)

    for prov, ids in by_provider.items():
        provider_cls = _CAMPLY_PROVIDER_CLASS.get(prov)
        if not provider_cls:
            continue
        try:
            provider = provider_cls()
            facilities = provider.find_campgrounds(campground_id=ids)
            for f in facilities:
                if f.recreation_area:
                    label = f"{f.recreation_area} - {f.facility_name}"
                else:
                    label = f.facility_name
                names[(prov, f.facility_id)] = label
        except Exception as exc:
            logger.debug("Name lookup failed for %s: %s", prov, exc)
    return names


def _register_commands(bot: telebot.TeleBot, state: ConfigState) -> None:
    """Register all bot command handlers."""

    @bot.message_handler(commands=["help", "start"])
    def cmd_help(message):
        if not _authorized(message, state):
            return
        text = (
            "<b>Campsite Checker Bot</b>\n\n"
            "/list — Show monitored campgrounds\n"
            "/alert — Show which campgrounds have alerts on\n"
            "/status — Show checker status\n\n"
            "<i>Config is git-managed: edit campsites.yaml and push to change "
            "which campgrounds are monitored or alerted on.</i>\n"
            f"Valid providers: {', '.join(PROVIDER_MAP)}"
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML")

    @bot.message_handler(commands=["list"])
    def cmd_list(message):
        if not _authorized(message, state):
            return
        with state.lock:
            entries = list(state.entries)
            config_path = state.config_path
        if not entries:
            bot.send_message(message.chat.id, "No campgrounds being monitored.")
            return

        names = yaml_editor.parse_yaml_comments(config_path)

        # Find entries missing names and look them up from the provider API.
        missing = [
            e
            for e in entries
            if e.get("campground_id")
            and (e.get("provider", "RecreationDotGov"), e["campground_id"]) not in names
        ]
        if missing:
            # Names are resolved live rather than cached back into the YAML:
            # campsites.yaml is git-managed and mounted read-only.
            names.update(_lookup_campground_names(missing))

        by_provider: dict = {}
        for e in entries:
            prov = e.get("provider", "RecreationDotGov")
            by_provider.setdefault(prov, []).append(e)

        lines = [f"🏕️ <b>Monitored Campgrounds ({len(entries)})</b>"]
        for prov, items in by_provider.items():
            lines.append(f"\n🌲 <b>{prov} ({len(items)})</b>")
            for item in items:
                cid = item.get("campground_id")
                if cid:
                    name = names.get((prov, cid))
                    if name:
                        lines.append(f"  • <b>{name}</b> <code>{cid}</code>")
                    else:
                        lines.append(f"  • <code>{cid}</code>")
                else:
                    ra = item.get("recreation_area", "?")
                    lines.append(f"  • rec area <code>{ra}</code>")
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")

    @bot.message_handler(commands=["status"])
    def cmd_status(message):
        if not _authorized(message, state):
            return
        with state.lock:
            count = len(state.entries)
        bot.send_message(
            message.chat.id,
            f"Monitoring {count} campground(s). Checker is running.",
        )

    @bot.message_handler(commands=["alert"])
    def cmd_alert(message):
        if not _authorized(message, state):
            return
        with state.lock:
            entries = list(state.entries)
            config_path = state.config_path
        names = yaml_editor.parse_yaml_comments(config_path)

        by_provider: dict = {}
        for e in entries:
            prov = e.get("provider", "RecreationDotGov")
            by_provider.setdefault(prov, []).append(e)

        lines = ["🔔 <b>Alert Status</b>"]
        for prov, items in by_provider.items():
            lines.append(f"\n🌲 <b>{prov}</b>")
            for e in items:
                cid = e.get("campground_id", "?")
                enabled = e.get("alert", False)
                status = "🟢 ON" if enabled else "🔴 OFF"
                name = names.get((prov, cid))
                label = f"<b>{name}</b> <code>{cid}</code>" if name else f"<code>{cid}</code>"
                lines.append(f"  • {label} — {status}")
        lines.append("\n<i>Set alert: true in campsites.yaml and push to change these.</i>")
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")


def create_bot(token: str, state: ConfigState) -> telebot.TeleBot:
    """Create and configure the Telegram bot."""
    bot = telebot.TeleBot(token, threaded=True)
    _register_commands(bot, state)
    bot.set_my_commands(
        [
            telebot.types.BotCommand("list", "Show monitored campgrounds"),
            telebot.types.BotCommand("alert", "Show which campgrounds have alerts on"),
            telebot.types.BotCommand("status", "Show checker status"),
            telebot.types.BotCommand("help", "Show help message"),
        ]
    )
    return bot


def start_bot_polling(bot: telebot.TeleBot) -> threading.Thread:
    """Start bot polling in a daemon thread. Returns the thread."""

    def _poll():
        logger.info("Telegram bot polling started")
        bot.infinity_polling(timeout=20, long_polling_timeout=20)

    thread = threading.Thread(target=_poll, name="telegram-bot", daemon=True)
    thread.start()
    return thread
