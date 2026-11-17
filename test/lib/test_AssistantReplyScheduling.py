from pathlib import Path
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from src.lib.Assistant import Assistant
from src.lib.Event import ConversationEvent, Event, StatusEvent, ToolEvent


def tool_event(responded_at: float | None) -> ToolEvent:
    return ToolEvent(
        request=[{"id": "call-1", "type": "function", "function": {"name": "FsdJump", "arguments": "{}"}}],
        results=[{"role": "tool", "tool_call_id": "call-1", "content": "Jump started"}],
        responded_at=responded_at,
    )


def make_assistant(history: list[Event]) -> tuple[Assistant, MagicMock]:
    assistant = Assistant.__new__(Assistant)
    assistant.config = {
        "active_character_index": 0,
        "characters": [{}],
        "game_actions_var": False,
        "web_search_actions_var": False,
        "ui_actions_var": False,
        "use_action_cache_var": False,
    }
    assistant.enabled_game_events = []
    assistant.disabled_game_events = []
    assistant.registered_should_reply_handlers = []
    assistant.pending = [tool_event(None)]
    assistant.reply_pending = True
    assistant.is_replying = False
    assistant.event_manager = MagicMock()
    # The database returns newest events first.
    assistant.event_manager.get_short_term_memory.return_value = list(reversed(history))
    assistant.event_manager.get_latest_memories.return_value = []
    assistant.prompt_generator = MagicMock()
    assistant.prompt_generator.generate_prompt.return_value = ([{"role": "user", "content": "prompt"}], MagicMock())
    assistant.action_manager = MagicMock()
    assistant.action_manager.getToolsList.return_value = []
    assistant.tts = MagicMock()
    assistant.llmModel = MagicMock()
    assistant.llmModel.generate.return_value = (None, None, MagicMock())
    return assistant, assistant.llmModel


def test_stale_reply_flag_does_not_resend_an_answered_tool_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.lib.Assistant.log_llm_usage", MagicMock())
    # The tool result was answered by the previous reply; only that reply and a status update remain.
    history: list[Event] = [
        tool_event(1.0),
        ConversationEvent(kind="assistant_speaking", content="", responded_at=None),
        ConversationEvent(kind="assistant", content="Supercruise engaged.", responded_at=None),
        ConversationEvent(kind="assistant_completed", content="", responded_at=None),
        StatusEvent(status={"event": "Status"}, responded_at=None),
    ]
    assistant, model = make_assistant(history)

    assistant.reply_thread({})

    model.generate.assert_not_called()
    assistant.prompt_generator.generate_prompt.assert_not_called()
    assert assistant.pending == []
    assert assistant.reply_pending is False
    assert assistant.is_replying is False


@pytest.mark.parametrize("trigger", ["user", "tool"])
def test_unanswered_request_still_gets_a_reply(monkeypatch: pytest.MonkeyPatch, trigger: str) -> None:
    monkeypatch.setattr("src.lib.Assistant.log_llm_usage", MagicMock())
    unanswered: Any = (
        ConversationEvent(kind="user", content="Engage supercruise", responded_at=None)
        if trigger == "user" else tool_event(None)
    )
    history: list[Event] = [
        ConversationEvent(kind="assistant", content="Previous reply", responded_at=1.0),
        unanswered,
        StatusEvent(status={"event": "Status"}, responded_at=None),
    ]
    assistant, model = make_assistant(history)

    assistant.reply_thread({})

    model.generate.assert_called_once()
    _, kwargs = assistant.prompt_generator.generate_prompt.call_args
    assert unanswered in kwargs["pending_events"]
