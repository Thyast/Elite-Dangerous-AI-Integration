from copy import deepcopy
from collections.abc import Mapping
from contextlib import closing
from threading import Lock, Thread
from typing import Any

from .Config import Config
from .Logger import log
from .Models import TTSModel
from .PluginManager import PluginManager
from .TTS import TTS
from .UI import emit_message


def resolve_tts_voice_settings(
    provider: Mapping[str, Any] | None,
    character: Mapping[str, Any],
    provider_ref: str,
) -> dict[str, Any] | None:
    grids = provider.get("voice_settings_config") if provider else None
    if grids is None:
        return None
    fields = [field for grid in grids for field in grid["fields"]]
    defaults = {
        field["key"]: field["default_value"]
        for field in fields
        if "default_value" in field
    }
    stored = character.get("tts_voice_settings", {}).get(provider_ref, {})
    declared_keys = {field["key"] for field in fields}
    settings = {
        **defaults,
        **({key: value for key, value in stored.items() if key in declared_keys}
           if isinstance(stored, dict) else {}),
    }
    if "voice" in declared_keys:
        settings.setdefault("voice", character["tts_voice"])
    if "instructions" in declared_keys:
        settings.setdefault("instructions", character["tts_prompt"])
    return settings


class TTSPreview:
    def __init__(self) -> None:
        self._lock = Lock()

    def start(self, config: Config, manager: PluginManager, data: dict[str, Any]) -> None:
        request_id = data.get("request_id")
        if not self._lock.acquire(blocking=False):
            emit_message("tts_preview", request_id=request_id, status="error",
                         message="A voice preview is already playing. Please wait.")
            return
        try:
            snapshot = deepcopy(config)
            Thread(target=self._run, args=(snapshot, manager, deepcopy(data)), daemon=True).start()
        except Exception as error:
            self._lock.release()
            log("error", "Could not start voice preview", error)
            emit_message("tts_preview", request_id=request_id, status="error", message=str(error))

    def _run(self, config: Config, manager: PluginManager, data: dict[str, Any]) -> None:
        request_id = data.get("request_id")
        error_message: str | None = None
        try:
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("A voice preview request ID is required.")
            text = data.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                raise ValueError("Enter a voice preview message between 1 and 500 characters.")
            index = data.get("character_index")
            if type(index) is not int or not 0 <= index < len(config["characters"]):
                raise ValueError("Select a character before previewing its voice.")
            provider_ref = config["tts_provider"]
            parts = provider_ref.split(":", 2)
            if len(parts) != 3 or parts[0] != "plugin":
                raise ValueError("Select a TTS provider before previewing a voice.")
            _, guid, provider_id = parts
            provider = manager.get_plugin_provider(guid, provider_id)
            model = manager.create_plugin_model(
                guid, provider_id, "tts",
                settings_override=config.get("plugin_settings", {}).get(guid, {}),
            )
            if not isinstance(model, TTSModel):
                raise RuntimeError("Could not initialize the selected TTS provider. Check its settings and logs.")
            with closing(model):
                character: dict[str, Any] = dict(config["characters"][index])
                settings = resolve_tts_voice_settings(provider, character, provider_ref)
                if hasattr(model, "speed"):
                    setattr(model, "speed", float(character["tts_speed"]))
                if settings is None and hasattr(model, "voice_instructions"):
                    setattr(model, "voice_instructions", character["tts_prompt"] or None)
                emit_message("tts_preview", request_id=request_id, status="playing")
                player = TTS(
                    tts_model=model,
                    voice=str((settings or {}).get("voice") or character["tts_voice"]),
                    voice_settings=settings,
                    speed=float(character["tts_speed"]),
                    postprocessing_config=character.get("tts_postprocessing"),
                    output_device=config["output_device_name"],
                    output_volume_multiplier=float(config.get("output_volume_multiplier", 1.0)),
                    start_playback_thread=False,
                )
                player.play_once(text)
        except Exception as error:
            log("error", "Voice preview failed", error)
            error_message = str(error)
        finally:
            self._lock.release()
        if error_message is not None:
            emit_message("tts_preview", request_id=request_id, status="error", message=error_message)
        else:
            emit_message("tts_preview", request_id=request_id, status="completed")
