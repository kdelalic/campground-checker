"""Tests for campsite_checker.bot command handlers and authorization."""

from types import SimpleNamespace

from campsite_checker.bot import ConfigState, _authorized, _register_commands


class FakeBot:
    """Duck-typed telebot.TeleBot capturing handlers and sent messages."""

    def __init__(self):
        self.handlers = {}
        self.sent = []

    def message_handler(self, commands):
        def decorator(func):
            for command in commands:
                self.handlers[command] = func
            return func

        return decorator

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text, parse_mode))


def _message(chat_id):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id))


def _state(entries, config_path="/nonexistent/campsites.yaml", chat_id="42"):
    return ConfigState(entries, {}, config_path, chat_id)


def _bot_with(entries, **state_kwargs):
    bot = FakeBot()
    _register_commands(bot, _state(entries, **state_kwargs))
    return bot


class TestAuthorization:
    def test_matching_chat_id_is_authorized(self):
        assert _authorized(_message(42), _state([])) is True

    def test_other_chat_id_is_rejected(self):
        assert _authorized(_message(43), _state([])) is False

    def test_unauthorized_chat_gets_no_reply(self):
        bot = _bot_with([{"campground_id": 1}])
        for command in ("help", "list", "status", "alert"):
            bot.handlers[command](_message(999))
        assert bot.sent == []

    def test_authorized_chat_gets_replies(self):
        bot = _bot_with([{"campground_id": 1}])
        bot.handlers["status"](_message(42))
        assert len(bot.sent) == 1
        assert "Monitoring 1 campground(s)" in bot.sent[0][1]


class TestHtmlEscaping:
    def test_list_escapes_names_from_yaml_comments(self, tmp_path):
        config = tmp_path / "campsites.yaml"
        config.write_text(
            "campsites:\n  RecreationDotGov:\n    - campground_id: 111  # Washburn <Creek> & Cove\n"
        )
        bot = _bot_with([{"campground_id": 111}], config_path=str(config))
        bot.handlers["list"](_message(42))

        text = bot.sent[0][1]
        assert "Washburn &lt;Creek&gt; &amp; Cove" in text
        assert "<Creek>" not in text

    def test_alert_escapes_names(self, tmp_path):
        config = tmp_path / "campsites.yaml"
        config.write_text(
            "campsites:\n  RecreationDotGov:\n    - campground_id: 111  # A&B <Camp>\n"
        )
        bot = _bot_with([{"campground_id": 111, "alert": True}], config_path=str(config))
        bot.handlers["alert"](_message(42))

        text = bot.sent[0][1]
        assert "A&amp;B &lt;Camp&gt;" in text
        assert "<Camp>" not in text

    def test_list_escapes_recreation_area(self):
        bot = _bot_with([{"recreation_area": "<ra&>"}])
        bot.handlers["list"](_message(42))
        text = bot.sent[0][1]
        assert "&lt;ra&amp;&gt;" in text
