import ast
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def test_new_overlay_receives_running_state_and_plugin_panels() -> None:
    source = Path(__file__).resolve().parents[2] / "src" / "Chat.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    chat_class = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Chat")
    method = next(node for node in chat_class.body if isinstance(node, ast.FunctionDef) and node.name == "emit_runtime_state")
    messages: list[tuple[str, dict]] = []
    namespace = {
        "emit_message": lambda kind, **payload: messages.append((kind, payload)),
        "get_system_info": lambda: {"os": "test"},
        "copy": copy,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    states = {"ShipInfo": {"ShipIdent": "TEST"}}
    plugins = SimpleNamespace(broadcast_plugin_ui=Mock())
    chat = SimpleNamespace(
        config={"overlay_vr_content": "interface"},
        event_manager=SimpleNamespace(get_current_state=lambda: ([], states)),
        plugin_manager=plugins,
        previous_states={},
        vr_adjustment=SimpleNamespace(active=False),
        _emit_voice_activation=Mock(),
    )

    namespace["emit_runtime_state"](chat)

    assert messages == [
        ("running_config", {"config": chat.config}),
        ("system", {"system": {"os": "test"}}),
        ("states", {"states": states}),
    ]
    plugins.broadcast_plugin_ui.assert_called_once_with()
    assert chat.previous_states == states
    assert chat.previous_states is not states
    chat.vr_adjustment.active = True
    chat.vr_adjustment.publish = Mock()
    namespace["emit_runtime_state"](chat)
    chat.vr_adjustment.publish.assert_called_once_with()
