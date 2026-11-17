from pathlib import Path
import json
import sys
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from src.lib.Event import ConversationEvent, Event, GameEvent, GameEventContent, ToolEvent
from src.lib.PromptGenerator import PromptGenerator


@pytest.mark.parametrize("raw_format", [False, True], ids=["projection", "raw-api"])
@pytest.mark.parametrize(
    ("government_fields", "expected_government", "engineer"),
    [
        pytest.param({}, "Federation None", False, id="missing"),
        pytest.param({"government": None}, "Federation None", False, id="null"),
        pytest.param({"government": ""}, "Federation ", False, id="empty"),
        pytest.param({"government": "Corporate"}, "Federation Corporate", False, id="corporate"),
        pytest.param({"government": " Engineer "}, "Federation  Engineer ", True, id="padded-engineer"),
        pytest.param({"government": "eNgInEeR"}, "Federation eNgInEeR", True, id="mixed-case-engineer"),
    ],
)
def test_format_stations_data_handles_optional_government(
    raw_format, government_fields, expected_government, engineer
):
    generator = PromptGenerator.__new__(PromptGenerator)
    station = {
        "name": "Test station",
        "type": "Coriolis Starport",
        "economy": "Industrial",
        "allegiance": "Federation",
        "controllingFaction": "Test faction",
        "materialTrader": "Raw",
        "technologyBroker": "Guardian",
        **government_fields,
    }
    if raw_format:
        station["haveMarket"] = True
        stations_data = {"stations": [station]}
        expected_services = ["market"]
    else:
        station["services"] = ["Market"]
        stations_data = [station]
        expected_services = ["Market"]
    if engineer:
        expected_services.append("Engineer")
    expected_services.extend(["Raw Material Trader", "Guardian Technology Broker"])

    assert generator.format_stations_data(stations_data) == {
        "In Orbit around Primary Star": {
            "Coriolis Starport": [
                {
                    "name": "Test station",
                    "economy": "Industrial",
                    "services": expected_services,
                    "government": expected_government,
                    "controllingFaction": "Test faction",
                }
            ]
        }
    }

from src.lib.SystemDatabase import SystemDatabase


class StationEventContent(GameEventContent):
    StationName: str


@pytest.fixture
def prompt_generator(monkeypatch: pytest.MonkeyPatch) -> PromptGenerator:
    monkeypatch.setattr("src.lib.PromptGenerator.QuestDatabase", Mock())
    return PromptGenerator(
        commander_name="Test Commander",
        character_prompt="You assist {commander_name}.",
        important_game_events=["Undocked"],
        system_db=Mock(spec=SystemDatabase),
    )


def test_undocked_event_does_not_imply_station_exit(prompt_generator: PromptGenerator) -> None:
    payload: StationEventContent = {
        "event": "Undocked",
        "timestamp": "2026-10-30T15:52:08Z",
        "StationName": "Test Station",
    }
    event = GameEvent(content=payload, historic=False)

    message = prompt_generator.get_event_template(event)

    assert message is not None
    assert "Test Commander has been released from the landing pad at Test Station" in message
    assert "does not confirm that the ship has exited the station" in message
    assert "Do not change throttle or boost solely in response to this event" in message


def test_launch_prompt_requires_explicit_request_for_additional_flight_actions(
    prompt_generator: PromptGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompt_generator, "generate_status_message", Mock(return_value="Ship status"))
    events: list[Event] = [ConversationEvent(kind="user", content="OK, take off.")]

    prompt, _ = prompt_generator.generate_prompt(
        events=events,
        projected_states={},
        pending_events=events,
        memories=[],
    )

    assert prompt[0]["role"] == "system"
    system_prompt = prompt[0]["content"]
    assert "A request to launch, undock or lift off alone does not authorize throttle changes or boost" in system_prompt
    assert "Do not follow liftoff or an Undocked event with setSpeed or engineBoost unless the commander explicitly requested that additional action" in system_prompt
    assert "leave any automated undocking in control" in system_prompt


@pytest.mark.parametrize("pending_count", [1, 2])
def test_unanswered_user_turns_follow_a_reply_generated_before_they_arrived(
    prompt_generator: PromptGenerator,
    monkeypatch: pytest.MonkeyPatch,
    pending_count: int,
) -> None:
    monkeypatch.setattr(prompt_generator, "generate_status_message", Mock(return_value="Ship status"))
    previous_request = ConversationEvent(kind="user", content="Previous request", responded_at=1.0)
    pending = [
        ConversationEvent(kind="user", content=f"New request {index}", responded_at=None)
        for index in range(pending_count)
    ]
    previous_response = ConversationEvent(kind="assistant", content="Reply to the previous request")
    events: list[Event] = [previous_request, *pending, previous_response]

    prompt, _ = prompt_generator.generate_prompt(
        events=events,
        projected_states={},
        pending_events=pending,
        memories=[],
    )

    assert prompt[0]["role"] == "system"
    assert prompt[-pending_count:] == [
        {"role": "user", "content": event.content} for event in pending
    ]
    assert prompt[-pending_count - 1] == {
        "role": "assistant", "content": previous_response.content,
    }
    for event in pending:
        assert sum(piece.get("content") == event.content for piece in prompt) == 1


def test_pending_user_turn_preserves_previous_tool_call_and_result(
    prompt_generator: PromptGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompt_generator, "generate_status_message", Mock(return_value="Ship status"))
    pending = ConversationEvent(kind="user", content="New request", responded_at=None)
    tool = ToolEvent(
        request=[{
            "id": "call-1", "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }],
        results=[{"role": "tool", "tool_call_id": "call-1", "content": "Lookup result"}],
    )

    prompt, _ = prompt_generator.generate_prompt(
        events=[pending, tool],
        projected_states={},
        pending_events=[pending, tool],
        memories=[],
    )

    assert prompt[-3:] == [
        {"role": "assistant", "tool_calls": tool.request},
        tool.results[0],
        {"role": "user", "content": pending.content},
    ]


def test_answered_conversation_history_keeps_its_order(
    prompt_generator: PromptGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompt_generator, "generate_status_message", Mock(return_value="Ship status"))
    events: list[Event] = [
        ConversationEvent(kind="user", content="Previous request", responded_at=1.0),
        ConversationEvent(kind="assistant", content="Previous reply", responded_at=1.0),
        ConversationEvent(kind="user", content="Latest request", responded_at=None),
    ]

    prompt, _ = prompt_generator.generate_prompt(
        events=events,
        projected_states={},
        pending_events=[events[-1]],
        memories=[],
    )

    assert prompt[-3:] == [
        {"role": "user", "content": "Previous request"},
        {"role": "assistant", "content": "Previous reply"},
        {"role": "user", "content": "Latest request"},
    ]


def test_google_request_ends_with_unanswered_user_input(
    prompt_generator: PromptGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompt_generator, "generate_status_message", Mock(return_value="Ship status"))
    pending = ConversationEvent(kind="user", content="New request", responded_at=None)
    events: list[Event] = [
        ConversationEvent(kind="user", content="Previous request", responded_at=1.0),
        pending,
        ConversationEvent(kind="assistant", content="Previous reply"),
    ]
    prompt, _ = prompt_generator.generate_prompt(
        events=events, projected_states={}, pending_events=[pending], memories=[],
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "assistant":
            return httpx.Response(400, json={"error": {
                "message": "Requests ending with a model turn are not supported.",
            }})
        return httpx.Response(200, json={
            "id": "test-completion", "object": "chat.completion", "created": 0,
            "model": "gemini-test",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Reply to the new request"},
            }],
        })

    model = GoogleAIStudioLLMModel(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="test-key", model_name="gemini-test", temperature=1.0,
        provider_name="google-ai-studio",
    )
    model.client.close()
    with OpenAI(
        base_url=model.base_url,
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        model.client = client
        text, actions, _ = model.generate(prompt)

    assert text == "Reply to the new request"
    assert actions is None
    assert len(requests) == 1
    assert json.loads(requests[0].content)["messages"][-2:] == [
        {"role": "assistant", "content": "Previous reply"},
        {"role": "user", "content": "New request"},
    ]
    assert events[1] is pending
