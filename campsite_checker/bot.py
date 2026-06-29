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


def _parse_add_remove_args(args: list) -> tuple[str | None, int | None]:
    """Parse '[provider] <campground_id>' arguments.

    Returns (provider, campground_id) or (None, None) on error.
    """
    if len(args) == 1:
        try:
            return "RecreationDotGov", int(args[0])
        except ValueError:
            return None, None
    elif len(args) == 2:
        provider = args[0]
        if provider not in PROVIDER_MAP:
            return None, None
        try:
            return provider, int(args[1])
        except ValueError:
            return None, None
    return None, None


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
            "/add <i>[provider]</i> <i>&lt;campground_id&gt;</i> — Add a campground\n"
            "/remove <i>[provider]</i> <i>&lt;campground_id&gt;</i> — Remove a campground\n"
            "/alert <i>&lt;campground_id&gt;</i> — Toggle alerts for a campground\n"
            "/status — Show checker status\n\n"
            "Provider defaults to RecreationDotGov if omitted.\n"
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
            resolved = _lookup_campground_names(missing)
            names.update(resolved)
            # Backfill resolved names as YAML comments for next time.
            for (prov, cid), rname in resolved.items():
                try:
                    yaml_editor.update_campground_comment(config_path, prov, cid, rname)
                except Exception:
                    pass

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

    @bot.message_handler(commands=["add"])
    def cmd_add(message):
        if not _authorized(message, state):
            return
        args = message.text.split()[1:]
        provider, campground_id = _parse_add_remove_args(args)
        if provider is None:
            bot.send_message(
                message.chat.id,
                "Usage: /add [provider] &lt;campground_id&gt;\n"
                f"Providers: {', '.join(PROVIDER_MAP)}",
                parse_mode="HTML",
            )
            return

        with state.lock:
            for e in state.entries:
                if (
                    e.get("provider", "RecreationDotGov") == provider
                    and e.get("campground_id") == campground_id
                ):
                    bot.send_message(
                        message.chat.id,
                        f"Already monitoring {provider} campground {campground_id}.",
                    )
                    return

            new_entry = {"campground_id": campground_id, "provider": provider}
            state.entries.append(new_entry)
            cs = state.raw_config.setdefault("campsites", {})
            cs.setdefault(provider, []).append({"campground_id": campground_id})
            config_path = state.config_path

        # Network I/O and file I/O outside the lock
        resolved = _lookup_campground_names([new_entry])
        name = resolved.get((provider, campground_id))

        try:
            yaml_editor.append_campground(config_path, provider, campground_id, name=name)
        except Exception as exc:
            logger.warning("Failed to write YAML: %s", exc)

        label = f"{name} ({campground_id})" if name else str(campground_id)
        bot.send_message(
            message.chat.id,
            f"Added {provider} campground {label}.\nWill be included in the next scan.",
        )

    @bot.message_handler(commands=["remove"])
    def cmd_remove(message):
        if not _authorized(message, state):
            return
        args = message.text.split()[1:]
        provider, campground_id = _parse_add_remove_args(args)
        if provider is None:
            bot.send_message(
                message.chat.id,
                "Usage: /remove [provider] &lt;campground_id&gt;\n"
                f"Providers: {', '.join(PROVIDER_MAP)}",
                parse_mode="HTML",
            )
            return

        with state.lock:
            original_len = len(state.entries)
            state.entries[:] = [
                e
                for e in state.entries
                if not (
                    e.get("provider", "RecreationDotGov") == provider
                    and e.get("campground_id") == campground_id
                )
            ]
            if len(state.entries) == original_len:
                bot.send_message(
                    message.chat.id,
                    f"Not found: {provider} campground {campground_id}.",
                )
                return

            prov_list = state.raw_config.get("campsites", {}).get(provider, [])
            state.raw_config["campsites"][provider] = [
                e for e in prov_list if e.get("campground_id") != campground_id
            ]
            config_path = state.config_path

        # File I/O outside the lock
        try:
            yaml_editor.remove_campground(config_path, provider, campground_id)
        except Exception as exc:
            logger.warning("Failed to write YAML: %s", exc)

        bot.send_message(
            message.chat.id,
            f"Removed {provider} campground {campground_id}.",
        )

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
        args = message.text.split()[1:]

        # /alert — show alert status for all campgrounds
        if len(args) == 0:
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
            lines.append("\n<i>/alert &lt;campground_id&gt; — toggle alerts</i>")
            bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")
            return

        # /alert <campground_id> — toggle alerts for that campground
        if len(args) == 1:
            try:
                campground_id = int(args[0])
            except ValueError:
                bot.send_message(message.chat.id, "Invalid campground ID.")
                return

            with state.lock:
                entry = next(
                    (e for e in state.entries if e.get("campground_id") == campground_id),
                    None,
                )
                if entry is None:
                    bot.send_message(message.chat.id, f"Campground {campground_id} not found.")
                    return

                current = entry.get("alert", False)
                new_val = not current
                entry["alert"] = new_val

                provider = entry.get("provider", "RecreationDotGov")
                config_path = state.config_path

            # File I/O outside the lock
            try:
                yaml_editor.update_alert_field(config_path, provider, campground_id, new_val)
            except Exception as exc:
                logger.warning("Failed to write YAML: %s", exc)

            status = "🟢 ON" if new_val else "🔴 OFF"
            bot.send_message(
                message.chat.id,
                f"Alerts for campground <code>{campground_id}</code>: {status}",
                parse_mode="HTML",
            )
            return

        bot.send_message(message.chat.id, "Usage: /alert [campground_id]")


def create_bot(token: str, state: ConfigState) -> telebot.TeleBot:
    """Create and configure the Telegram bot."""
    bot = telebot.TeleBot(token, threaded=True)
    _register_commands(bot, state)
    bot.set_my_commands(
        [
            telebot.types.BotCommand("list", "Show monitored campgrounds"),
            telebot.types.BotCommand("add", "Add a campground to monitor"),
            telebot.types.BotCommand("remove", "Remove a monitored campground"),
            telebot.types.BotCommand("alert", "Toggle alerts for a campground"),
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
