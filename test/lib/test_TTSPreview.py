import importlib
import base64
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
preview_module = importlib.import_module("lib.TTSPreview")
from lib.Config import load_config
from lib.Models import TTSModel
from lib.PluginManager import PluginManager
from lib.TTS import TTS
from plugins.GoogleAIStudioPlugin import GoogleAIStudioTTSModel


PROVIDER = "plugin:test:tts"
DATA = {"request_id": "preview-1", "character_index": 0, "text": "Bonjour Commandant."}


@pytest.fixture
def preview(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = load_config()
    config["tts_provider"] = PROVIDER
    config["output_device_name"] = "Test speakers"
    config["output_volume_multiplier"] = 0.8
    config["plugin_settings"] = {"test": {"api_key": "test-key"}}
    config["characters"][0]["tts_voice_settings"] = {
        PROVIDER: {"voice": "Sulafat", "instructions": "Speak French.", "obsolete": "ignored"},
    }
    manager = MagicMock(spec=PluginManager)
    manager.get_plugin_provider.return_value = {"voice_settings_config": [{"fields": [
        {"key": "voice", "default_value": "Kore"},
        {"key": "instructions", "default_value": ""},
    ]}]}
    manager.create_plugin_model.return_value = MagicMock(spec=TTSModel)
    player = MagicMock()
    monkeypatch.setattr(preview_module, "TTS", player)
    emit = MagicMock()
    monkeypatch.setattr(preview_module, "emit_message", emit)
    monkeypatch.setattr(preview_module, "Thread", lambda *, target, args, daemon: SimpleNamespace(
        start=lambda: target(*args),
    ))
    return SimpleNamespace(
        controller=preview_module.TTSPreview(), config=config, manager=manager, player=player, emit=emit,
    )


def statuses(preview):
    return [call.kwargs["status"] for call in preview.emit.call_args_list]


def test_preview_repeats_fresh_synthesis_with_current_character_and_output(preview):
    preview.controller.start(preview.config, preview.manager, DATA)
    preview.controller.start(preview.config, preview.manager, {**DATA, "request_id": "preview-2"})

    assert statuses(preview) == ["playing", "completed", "playing", "completed"]
    assert preview.manager.create_plugin_model.call_count == 2
    preview.manager.create_plugin_model.assert_called_with(
        "test", "tts", "tts", settings_override={"api_key": "test-key"},
    )
    kwargs = preview.player.call_args.kwargs
    assert kwargs["voice"] == "Sulafat"
    assert kwargs["voice_settings"] == {"voice": "Sulafat", "instructions": "Speak French."}
    assert kwargs["output_device"] == "Test speakers"
    assert kwargs["output_volume_multiplier"] == 0.8
    assert kwargs["postprocessing_config"] == preview.config["characters"][0]["tts_postprocessing"]
    assert kwargs["start_playback_thread"] is False
    assert preview.player.return_value.play_once.call_count == 2
    assert preview.manager.create_plugin_model.return_value.close.call_count == 2
    preview.player.return_value.play_once.assert_called_with(DATA["text"])
    assert preview.config["characters"][0]["tts_voice_settings"][PROVIDER]["obsolete"] == "ignored"


def test_preview_snapshots_settings_before_background_execution(preview, monkeypatch):
    thread = MagicMock()
    monkeypatch.setattr(preview_module, "Thread", thread)
    preview.controller.start(preview.config, preview.manager, DATA)
    preview.config["characters"][0]["tts_voice_settings"][PROVIDER]["voice"] = "Puck"
    preview.config["plugin_settings"]["test"]["api_key"] = "changed-key"
    kwargs = thread.call_args.kwargs
    kwargs["target"](*kwargs["args"])

    assert preview.player.call_args.kwargs["voice"] == "Sulafat"
    preview.manager.create_plugin_model.assert_called_once_with(
        "test", "tts", "tts", settings_override={"api_key": "test-key"},
    )


@pytest.mark.parametrize("changes", [
    {"text": ""}, {"text": " "}, {"text": "a" * 501}, {"text": None},
    {"character_index": -1}, {"character_index": 100}, {"character_index": True},
    {"request_id": ""},
])
def test_preview_rejects_invalid_input_without_synthesizing(preview, changes):
    preview.controller.start(preview.config, preview.manager, {**DATA, **changes})
    assert statuses(preview) == ["error"]
    assert preview.emit.call_args.kwargs["message"]
    preview.player.assert_not_called()
    preview.manager.create_plugin_model.assert_not_called()
    assert not preview.controller._lock.locked()


def test_preview_requires_selected_provider(preview):
    preview.config["tts_provider"] = "none"
    preview.controller.start(preview.config, preview.manager, DATA)
    assert statuses(preview) == ["error"]
    preview.player.assert_not_called()


def test_preview_reports_model_and_synthesis_failures(preview):
    preview.manager.create_plugin_model.return_value = None
    preview.controller.start(preview.config, preview.manager, DATA)
    assert statuses(preview) == ["error"]
    preview.player.assert_not_called()

    preview.emit.reset_mock()
    preview.manager.create_plugin_model.return_value = MagicMock(spec=TTSModel)
    preview.player.return_value.play_once.side_effect = RuntimeError("Provider unavailable")
    preview.controller.start(preview.config, preview.manager, DATA)
    assert statuses(preview) == ["playing", "error"]
    assert preview.emit.call_args.kwargs["message"] == "Provider unavailable"
    preview.manager.create_plugin_model.return_value.close.assert_called_once()
    assert not preview.controller._lock.locked()


def test_preview_releases_busy_state_before_reporting_completion(preview):
    def emit(message_type, **kwargs):
        if kwargs["status"] == "completed":
            assert not preview.controller._lock.locked()

    preview.emit.side_effect = emit
    preview.controller.start(preview.config, preview.manager, DATA)
    assert statuses(preview) == ["playing", "completed"]


def test_factory_uses_detached_preview_settings_instead_of_live_configuration(preview):
    manager = PluginManager(preview.config)
    plugin = MagicMock()
    model = MagicMock(spec=TTSModel)
    manager._owners["test"] = plugin
    manager.get_plugin_provider = MagicMock(return_value={"kind": "tts"})
    supplied = {"nested": {"voice": "snapshot"}}

    def create_model(provider_id, settings):
        assert settings["nested"]["voice"] == "snapshot"
        settings["nested"]["voice"] = "modified"
        return model

    plugin.create_model.side_effect = create_model
    assert manager.create_plugin_model("test", "tts", "tts", settings_override=supplied) is model
    assert supplied == {"nested": {"voice": "snapshot"}}
    assert manager.config["plugin_settings"]["test"] == {"api_key": "test-key"}


def test_preview_rejects_overlapping_requests(preview):
    preview.controller._lock.acquire()
    try:
        preview.controller.start(preview.config, preview.manager, DATA)
        assert statuses(preview) == ["error"]
        preview.player.assert_not_called()
        assert preview.controller._lock.locked()
    finally:
        preview.controller._lock.release()


def test_preview_reports_worker_start_failure_and_allows_retry(preview, monkeypatch):
    thread = MagicMock()
    thread.return_value.start.side_effect = RuntimeError("Cannot start thread")
    monkeypatch.setattr(preview_module, "Thread", thread)
    preview.controller.start(preview.config, preview.manager, DATA)
    assert statuses(preview) == ["error"]
    assert not preview.controller._lock.locked()


def test_voice_settings_preserve_legacy_and_voiceless_providers():
    character = {"tts_voice": "legacy", "tts_prompt": "Warm.", "tts_voice_settings": {}}
    assert preview_module.resolve_tts_voice_settings(None, character, PROVIDER) is None
    assert preview_module.resolve_tts_voice_settings({"voice_settings_config": []}, character, PROVIDER) == {}
    assert preview_module.resolve_tts_voice_settings({"voice_settings_config": [{"fields": [
        {"key": "voice"}, {"key": "instructions"},
    ]}]}, character, PROVIDER) == {"voice": "legacy", "instructions": "Warm."}


def test_preview_reaches_gemini_and_audio_output_twice_without_caching(preview, monkeypatch):
    requests = []
    pcm = b"\x0a\x00" * 512
    events = [
        {"event_type": "step.delta", "delta": {
            "type": "audio", "mime_type": "audio/l16",
            "data": base64.b64encode(pcm).decode("ascii"),
        }},
        {"event_type": "interaction.completed", "interaction": {"status": "completed"}},
    ]

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content="".join(
            f"data: {json.dumps(event)}\n\n" for event in events
        ))

    models = []

    def create_model(*args, **kwargs):
        model = GoogleAIStudioTTSModel("test-key")
        model.client.close()
        model.client = httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(respond))
        models.append(model)
        return model

    preview.manager.create_plugin_model.side_effect = create_model
    monkeypatch.setattr(preview_module, "TTS", TTS)
    audio = MagicMock()
    audio.get_sample_size.return_value = 2
    monkeypatch.setattr("lib.TTS.pyaudio.PyAudio", lambda: audio)
    for request_id in ("one", "two"):
        preview.controller.start(preview.config, preview.manager, {**DATA, "request_id": request_id})

    assert statuses(preview) == ["playing", "completed", "playing", "completed"]
    assert len(requests) == 2
    assert all(model.client.is_closed for model in models)
    assert requests[0] == requests[1]
    assert requests[0]["generation_config"]["speech_config"] == [{"voice": "Sulafat"}]
    content = requests[0]["input"][0]["content"][0]
    assert content["text"] == DATA["text"]
    assert content["annotations"] == [{"type": "speech_metadata", "style": "Speak French."}]
    assert audio.open.call_count == 2
    assert audio.open.return_value.write.call_count == 2
    assert audio.open.return_value.write.call_args.args[0] == b"\x08\x00" * 512
    assert audio.terminate.call_count == 2


def test_runtime_command_routes_preview_without_invoking_assistant(preview, monkeypatch):
    chat_module = importlib.import_module("Chat")
    chat = SimpleNamespace(config=preview.config, plugin_manager=preview.manager)
    controller = MagicMock()
    monkeypatch.setattr(chat_module, "tts_preview", controller)
    monkeypatch.setattr(chat_module, "config", preview.config, raising=False)
    monkeypatch.setattr(chat_module, "emit_message", MagicMock())

    class EndInput(Exception):
        pass

    stdin = MagicMock()
    stdin.readline.side_effect = [json.dumps({"type": "preview_tts", **DATA}), EndInput]
    monkeypatch.setattr(chat_module.sys, "stdin", stdin)
    with pytest.raises(EndInput):
        chat_module.read_stdin(chat)
    controller.start.assert_called_once_with(
        preview.config, preview.manager, {"type": "preview_tts", **DATA},
    )
