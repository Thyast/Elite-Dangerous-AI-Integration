import importlib
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
ChatModule = importlib.import_module("Chat")
from lib.Event import ConversationEvent, PluginEvent


@pytest.fixture
def chat(monkeypatch):
    instance = ChatModule.Chat.__new__(ChatModule.Chat)
    instance.previous_states = {}
    sent = Mock()
    displayed = Mock()
    monkeypatch.setattr(ChatModule, "send_message", sent)
    monkeypatch.setattr(ChatModule, "show_chat_message", displayed)
    return instance, sent, displayed


@pytest.mark.parametrize("event_name", [
    "f1d78e6b-3e3b-4dc6-a61c-bff3e2b2f11e.upgrade_notice",
    "EdCoPilotPanelContentsEvent",
])
@pytest.mark.parametrize("content", [
    "Guardian Module Reinforcement 2E has reached its plan target.",
    {"key": "value"},
    {"text": ""},
    {"text": " \t\n"},
    {"text": 42},
    {"avatar_url": "https://example.test/avatar.png"},
    None,
    ["internal notice"],
])
def test_internal_plugin_event_is_forwarded_without_a_chat_line(chat, event_name, content):
    instance, sent, displayed = chat
    event = PluginEvent(plugin_event_name=event_name, plugin_event_content=content)

    instance.on_event(event, {})

    sent.assert_called_once_with({"type": "event", "event": event})
    displayed.assert_not_called()
    assert event.plugin_event_content == content


@pytest.mark.parametrize("avatar", [
    "https://example.test/avatar.png",
    "",
    None,
    42,
])
def test_explicit_plugin_chat_text_and_valid_avatar_are_preserved(chat, avatar):
    instance, sent, displayed = chat
    event = PluginEvent(
        plugin_event_name="EdCoPilotEvent",
        plugin_event_content={"text": "Arrived at the station.", "avatar_url": avatar},
    )

    instance.on_event(event, {})

    sent.assert_called_once_with({"type": "event", "event": event})
    displayed.assert_called_once_with(
        "plugin",
        "Arrived at the station.",
        plugin_event_name="EdCoPilotEvent",
        avatar_url=avatar if isinstance(avatar, str) and avatar else None,
    )


def test_assistant_reply_remains_visible_after_an_internal_notice(chat):
    instance, sent, displayed = chat
    notice = PluginEvent(
        plugin_event_name="f1d78e6b-3e3b-4dc6-a61c-bff3e2b2f11e.upgrade_notice",
        plugin_event_content="The module has reached its plan target.",
    )
    reply = ConversationEvent(kind="assistant", content="The module is ready.")
    states = {"ShipInfo": {"ShipID": 15}}

    instance.on_event(notice, states)
    instance.on_event(reply, states)

    displayed.assert_called_once_with("covas", "The module is ready.")
    assert instance.previous_states == states
    assert sent.call_args_list[0].args == ({"type": "states", "states": states},)
    assert sent.call_args_list[1].args == ({"type": "event", "event": notice},)
    assert sent.call_args_list[2].args == ({"type": "event", "event": reply},)
