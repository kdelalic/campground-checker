import logging
import re
import threading
from typing import Dict, List, Optional, Tuple

import telebot

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


def _append_to_yaml(
    config_path: str, provider: str, campground_id: int, name: Optional[str] = None
) -> None:
    """Append a campground entry to the YAML file, preserving comments."""
    with open(config_path) as f:
        lines = f.readlines()

    comment = f" # {name}" if name else ""
    new_line = f"    - campground_id: {campground_id}{comment}\n"

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


def _lookup_campground_names(
    entries: List[dict],
) -> Dict[Tuple[str, int], str]:
    """Look up campground names from camply providers.

    Returns dict mapping (provider, campground_id) -> facility name.
    """
    names: Dict[Tuple[str, int], str] = {}
    # Group IDs by provider
    by_provider: Dict[str, List[int]] = {}
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


def _update_yaml_comment(
    config_path: str, provider: str, campground_id: int, name: str
) -> None:
    """Add an inline comment to an existing campground entry in the YAML file."""
    with open(config_path) as f:
        lines = f.readlines()

    in_provider = False
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if re.match(r"^  \S+:\s*$", stripped):
            in_provider = stripped.strip().rstrip(":") == provider
        elif in_provider and re.match(
            rf"^\s+- campground_id:\s+{campground_id}\s*$", stripped
        ):
            # Line has no comment yet — append one
            lines[i] = stripped + f" # {name}\n"
            break
    else:
        return  # not found

    with open(config_path, "w") as f:
        f.writelines(lines)


def _update_alert_sites_yaml(
    config_path: str, provider: str, campground_id: int, alert_sites: list
) -> None:
    """Update or remove the alert_sites field for a campground entry in the YAML file."""
    with open(config_path) as f:
        lines = f.readlines()

    in_provider = False
    entry_idx = None
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if re.match(r"^  \S+:\s*$", stripped):
            in_provider = stripped.strip().rstrip(":") == provider
        elif in_provider and re.match(
            rf"^\s+- campground_id:\s+{campground_id}\b", stripped
        ):
            entry_idx = i
            break

    if entry_idx is None:
        return

    # Check if there's already an alert_sites line right after the entry
    alert_line_idx = None
    if entry_idx + 1 < len(lines):
        next_line = lines[entry_idx + 1].rstrip()
        if re.match(r"^\s+alert_sites:", next_line):
            alert_line_idx = entry_idx + 1

    if alert_sites:
        new_line = f"      alert_sites: {alert_sites}\n"
        if alert_line_idx is not None:
            lines[alert_line_idx] = new_line
        else:
            lines.insert(entry_idx + 1, new_line)
    elif alert_line_idx is not None:
        del lines[alert_line_idx]

    with open(config_path, "w") as f:
        f.writelines(lines)


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
            "/alert <i>&lt;campground_id&gt;</i> <i>[campsite_id]</i> — Manage alert sites\n"
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

        # Find entries missing names and look them up from the provider API.
        missing = [
            e
            for e in entries
            if e.get("campground_id")
            and (e.get("provider", "RecreationDotGov"), e["campground_id"])
            not in names
        ]
        if missing:
            resolved = _lookup_campground_names(missing)
            names.update(resolved)
            # Backfill resolved names as YAML comments for next time.
            for (prov, cid), rname in resolved.items():
                try:
                    _update_yaml_comment(config_path, prov, cid, rname)
                except Exception:
                    pass

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

            # Look up the human-readable name from the provider API.
            resolved = _lookup_campground_names([new_entry])
            name = resolved.get((provider, campground_id))

            try:
                _append_to_yaml(
                    state.config_path, provider, campground_id, name=name
                )
            except Exception as exc:
                logger.warning("Failed to write YAML: %s", exc)

        label = f"{name} ({campground_id})" if name else str(campground_id)
        bot.send_message(
            message.chat.id,
            f"Added {provider} campground {label}.\n"
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

    @bot.message_handler(commands=["alert"])
    def cmd_alert(message):
        if not _authorized(message, state):
            return
        args = message.text.split()[1:]

        if len(args) == 0:
            bot.send_message(
                message.chat.id,
                "Usage:\n"
                "/alert <i>&lt;campground_id&gt;</i> — list alert sites\n"
                "/alert <i>&lt;campground_id&gt;</i> <i>&lt;campsite_id&gt;</i> — toggle alert\n"
                "/alert clear <i>&lt;campground_id&gt;</i> — clear all alerts",
                parse_mode="HTML",
            )
            return

        # /alert clear <campground_id>
        if args[0] == "clear" and len(args) == 2:
            try:
                campground_id = int(args[1])
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
                entry.pop("alert_sites", None)
                provider = entry.get("provider", "RecreationDotGov")
                try:
                    _update_alert_sites_yaml(state.config_path, provider, campground_id, [])
                except Exception as exc:
                    logger.warning("Failed to write YAML: %s", exc)

            bot.send_message(message.chat.id, f"Cleared all alert sites for campground {campground_id}.")
            return

        # /alert <campground_id> — list current alert sites
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
                current = entry.get("alert_sites", [])

            if current:
                sites_str = ", ".join(str(s) for s in current)
                bot.send_message(
                    message.chat.id,
                    f"Alert sites for campground {campground_id}: {sites_str}",
                )
            else:
                bot.send_message(
                    message.chat.id,
                    f"No alert sites set for campground {campground_id} (all sites trigger alerts).",
                )
            return

        # /alert <campground_id> <campsite_id> — toggle
        if len(args) == 2:
            try:
                campground_id = int(args[0])
                campsite_id = int(args[1])
            except ValueError:
                bot.send_message(message.chat.id, "Invalid ID(s). Both must be integers.")
                return

            with state.lock:
                entry = next(
                    (e for e in state.entries if e.get("campground_id") == campground_id),
                    None,
                )
                if entry is None:
                    bot.send_message(message.chat.id, f"Campground {campground_id} not found.")
                    return

                current = entry.get("alert_sites", [])
                if campsite_id in current:
                    current.remove(campsite_id)
                    action = "Removed"
                else:
                    current.append(campsite_id)
                    action = "Added"

                if current:
                    entry["alert_sites"] = current
                else:
                    entry.pop("alert_sites", None)

                provider = entry.get("provider", "RecreationDotGov")
                try:
                    _update_alert_sites_yaml(state.config_path, provider, campground_id, current)
                except Exception as exc:
                    logger.warning("Failed to write YAML: %s", exc)

            sites_str = ", ".join(str(s) for s in current) if current else "none (all sites trigger alerts)"
            bot.send_message(
                message.chat.id,
                f"{action} site {campsite_id} for campground {campground_id}.\n"
                f"Current alert sites: {sites_str}",
            )
            return

        bot.send_message(message.chat.id, "Invalid usage. Send /alert for help.")


def create_bot(token: str, state: ConfigState) -> telebot.TeleBot:
    """Create and configure the Telegram bot."""
    bot = telebot.TeleBot(token, threaded=True)
    _register_commands(bot, state)
    bot.set_my_commands(
        [
            telebot.types.BotCommand("list", "Show monitored campgrounds"),
            telebot.types.BotCommand("add", "Add a campground to monitor"),
            telebot.types.BotCommand("remove", "Remove a monitored campground"),
            telebot.types.BotCommand("alert", "Manage site-specific alerts"),
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
