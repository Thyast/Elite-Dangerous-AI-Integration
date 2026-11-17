import ast
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletionMessageFunctionToolCall

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.actions import Actions
from lib import VrRecenter


@pytest.fixture
def game(monkeypatch):
    keys = SimpleNamespace(keys={"HMDReset": {"key": 68, "mods": [42]}}, send=Mock())
    focus = Mock()
    monkeypatch.setattr(VrRecenter, "set_game_window_active", focus)
    monkeypatch.setattr(VrRecenter, "recenter_openkneeboard", Mock(return_value=True))
    return SimpleNamespace(keys=keys, focus=focus)


def test_recenter_sends_only_the_configured_hmd_reset_binding(game):
    calls = []
    game.focus.side_effect = lambda **_kwargs: calls.append("focus")
    game.keys.send.side_effect = lambda key: calls.append(key)
    VrRecenter.recenter_game(game.keys)
    assert calls == ["focus", "HMDReset"]
    game.focus.assert_called_once_with(require_focus=True)


def test_missing_recenter_binding_is_actionable_and_sends_no_input(game):
    game.keys.keys.clear()
    with pytest.raises(ValueError, match="HMDReset"):
        VrRecenter.recenter_game(game.keys)
    game.focus.assert_not_called()
    game.keys.send.assert_not_called()


def test_closed_game_is_an_error_not_a_key_sent_to_another_app(game):
    game.focus.side_effect = RuntimeError("Elite Dangerous is not running")
    with pytest.raises(RuntimeError, match="Elite Dangerous"):
        VrRecenter.recenter_game(game.keys)
    game.keys.send.assert_not_called()


def test_failed_game_focus_does_not_send_the_binding(game):
    game.focus.side_effect = RuntimeError("Unable to focus Elite Dangerous")
    with pytest.raises(RuntimeError, match="focus"):
        VrRecenter.recenter_game(game.keys)
    game.keys.send.assert_not_called()


def test_recenter_does_not_report_success_when_input_fails(game):
    game.keys.send.side_effect = OSError("Input unavailable")
    with pytest.raises(OSError, match="Input unavailable"):
        VrRecenter.recenter_game(game.keys)


def test_chat_wires_the_local_recenter_callback_to_the_game_action(game):
    source = Path(__file__).parents[2] / "src" / "Chat.py"
    constructor = next(
        node for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "VrOverlayAdjustment"
    )
    callback = next(keyword.value for keyword in constructor.keywords if keyword.arg == "recenter")
    eval(compile(ast.Expression(body=callback), str(source), "eval"), {
        "recenter_game_and_kneeboard": VrRecenter.recenter_game_and_kneeboard,
        "self": SimpleNamespace(ed_keys=game.keys),
    })()
    game.keys.send.assert_called_once_with("HMDReset")


@pytest.mark.parametrize("allowed", [False, True])
def test_model_cannot_offer_or_execute_recenter_even_with_permission(monkeypatch, game, allowed):
    source = Path(Actions.__file__).read_text(encoding="utf-8")
    registration = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "register_actions"
    )
    monkeypatch.setattr("lib.ActionManager.KeyValueStore", lambda _name: Mock())
    manager = Actions.ActionManager()
    manager.actions = {}
    manager.set_allowed_actions({"recenterVRView": allowed, "textMessage": True})
    namespace = {
        **vars(Actions),
        "setGameWindowActive": Mock(),
        "register_web_actions": Mock(),
        "register_ui_actions": Mock(),
    }
    module = ast.Module(body=[registration], type_ignores=[])
    exec(compile(module, "<registration>", "exec"), namespace)
    namespace["register_actions"](
        actionManager=manager, eventManager=Mock(), promptGenerator=Mock(), llmModel=Mock(),
        visionModel=None, visionModelName=None, embeddingModel=None, edKeys=game.keys,
    )
    assert "recenterVRView" not in manager.actions
    for mode in ("mainship", "fighter", "nomad", "buggy", "humanoid", None):
        tools = manager.getToolsList(mode, True, False, False, {"recenterVRView": allowed, "textMessage": True})
        names = {tool["function"]["name"] for tool in tools}
        assert "textMessage" in names
        assert "recenterVRView" not in names
    stale_call = ChatCompletionMessageFunctionToolCall(
        id="stale-recenter", type="function", function={"name": "recenterVRView", "arguments": "{}"},
    )
    result = manager.runAction(stale_call, {"CurrentStatus": {"event": "LoadGame"}})
    assert result["content"] == "ERROR: Function recenterVRView does not exist!"
    game.focus.assert_not_called()
    game.keys.send.assert_not_called()
