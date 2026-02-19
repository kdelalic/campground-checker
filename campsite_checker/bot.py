import logging
import re
import threading
from typing import List, Optional, Tuple

import telebot

from .providers import PROVIDER_MAP

logger = logging.getLogger(__name__)


class ConfigState:
    """Thread-safe mutable config shared between the check loop and bot commands."""

    def __init__(
        self,
        entries: List[dict],
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


def _parse_add_remove_args(args: list) -> Tuple[Optional[str], Optional[int]]:
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


def _append_to_yaml(config_path: str, provider: str, campground_id: int) -> None:
    """Append a campground entry to the YAML file, preserving comments."""
    with open(config_path) as f:
        lines = f.readlines()

    new_line = f"    - campground_id: {campground_id}\n"

    # Find the provider section and its last entry
    in_provider = False
    last_entry_idx = None
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        # Provider header like "  RecreationDotGov:"
        if re.match(r"^  \S+:$", stripped):
            if stripped == f"  {provider}:":
                in_provider = True
            elif in_provider:
                # Hit the next provider section, stop
                break
        elif in_provider and stripped.startswith("    - campground_id:"):
            last_entry_idx = i

    if last_entry_idx is not None:
        lines.insert(last_entry_idx + 1, new_line)
    else:
        # Provider section doesn't exist yet — append at end of file
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"\n  {provider}:\n")
        lines.append(new_line)

    with open(config_path, "w") as f:
        f.writelines(lines)


def _remove_from_yaml(config_path: str, provider: str, campground_id: int) -> None:
    """Remove a campground entry from the YAML file, preserving comments."""
    with open(config_path) as f:
        lines = f.readlines()

    in_provider = False
    remove_idx = None
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if re.match(r"^  \S+:$", stripped):
            if stripped == f"  {provider}:":
                in_provider = True
            elif in_provider:
                break
        elif in_provider and re.match(
            rf"^\s+- campground_id:\s+{campground_id}\b", stripped
        ):
            remove_idx = i
            break

    if remove_idx is not None:
        del lines[remove_idx]
        with open(config_path, "w") as f:
            f.writelines(lines)


def _parse_yaml_comments(config_path: str) -> dict:
    """Parse inline comments from the YAML file to get human-readable names.

    Returns dict mapping (provider, campground_id) -> comment string.
    """
    names = {}
    current_provider = None
    try:
        with open(config_path) as f:
            for line in f:
                stripped = line.rstrip()
                # Provider header like "  RecreationDotGov:"
                if re.match(r"^  \S+:\s*$", stripped):
                    current_provider = stripped.strip().rstrip(":")
                elif current_provider and "#" in line:
                    m = re.match(r"\s+- campground_id:\s+(\d+)\s+#\s*(.+)", stripped)
                    if m:
                        cid = int(m.group(1))
                        comment = m.group(2).strip()
                        names[(current_provider, cid)] = comment
    except Exception:
        pass
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

        names = _parse_yaml_comments(config_path)

        by_provider: dict = {}
        for e in entries:
            prov = e.get("provider", "RecreationDotGov")
            by_provider.setdefault(prov, []).append(e)

        lines = [f"<b>Monitored Campgrounds ({len(entries)})</b>"]
        for prov, items in by_provider.items():
            lines.append(f"\n<b>{prov}</b> ({len(items)})")
            for item in items:
                cid = item.get("campground_id")
                if cid:
                    name = names.get((prov, cid))
                    if name:
                        lines.append(f"  • {name} ({cid})")
                    else:
                        lines.append(f"  • {cid}")
                else:
                    ra = item.get("recreation_area", "?")
                    lines.append(f"  • rec area {ra}")
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
            try:
                _append_to_yaml(state.config_path, provider, campground_id)
            except Exception as exc:
                logger.warning("Failed to write YAML: %s", exc)

        bot.send_message(
            message.chat.id,
            f"Added {provider} campground {campground_id}.\n"
            "Will be included in the next scan.",
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
            try:
                _remove_from_yaml(state.config_path, provider, campground_id)
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


def create_bot(token: str, state: ConfigState) -> telebot.TeleBot:
    """Create and configure the Telegram bot."""
    bot = telebot.TeleBot(token, threaded=True)
    _register_commands(bot, state)
    bot.set_my_commands(
        [
            telebot.types.BotCommand("list", "Show monitored campgrounds"),
            telebot.types.BotCommand("add", "Add a campground to monitor"),
            telebot.types.BotCommand("remove", "Remove a monitored campground"),
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
