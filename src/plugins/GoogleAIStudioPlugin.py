import base64
import binascii
import json
from email.message import Message
from typing import Any, Generator, Iterable, override

import httpx

from lib.Models import LLMError, OpenAIEmbeddingModel, OpenAILLMModel, OpenAIMultiModalSTTModel, TTSModel
from lib.PluginBase import PluginBase, PluginManifest
from plugins.ProviderPluginHelpers import api_key, float_setting, iter_sse_data, number_field, paragraph, select_field, string_setting, textarea_field, text_field
from plugins.EdgeTTSPlugin import EDGE_TTS_PLUGIN_GUID


GOOGLE_AI_STUDIO_PLUGIN_GUID = "51f1df0c-479d-4f0f-a585-57c08d9e1901"
GOOGLE_AI_STUDIO_API_URL = "https://generativelanguage.googleapis.com/v1beta"
GOOGLE_LLM_MODEL = "gemini-3.1-flash-lite-preview"
GOOGLE_AGENT_LLM_MODEL = "gemini-3.1-flash-lite-preview"
GOOGLE_VLM_MODEL = "gemini-2.5-flash"
GOOGLE_STT_MODEL = "gemini-2.5-flash-lite"
GOOGLE_TTS_MODEL = "gemini-3.8-flash-lite-tts"
GOOGLE_TTS_VOICE = "Kore"
GOOGLE_TTS_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]
GOOGLE_EMBEDDING_MODEL = "gemini-embedding-001"


class GoogleAIStudioLLMModel(OpenAILLMModel):
    def _prepare_messages(self, messages: list[dict]) -> list[dict]:
        request_messages = super()._prepare_messages(messages)
        for message in request_messages:
            calls = message.get("tool_calls") or []
            for index, call in enumerate(calls):
                if not isinstance(call, dict):
                    if hasattr(call, "model_dump"):
                        call = call.model_dump()
                    elif hasattr(call, "dict"):
                        call = call.dict()
                    calls[index] = call
                if isinstance(call, dict):
                    signature = call.get("extra_content", {}).get("google", {}).get("thought_signature")
                    if not signature:
                        call["extra_content"] = {
                            "google": {"thought_signature": "skip_thought_signature_validator"}
                        }
        return request_messages


class GoogleAIStudioTTSModel(TTSModel):
    def __init__(self, api_key: str, model_name: str = GOOGLE_TTS_MODEL):
        super().__init__(model_name, provider_name="google-ai-studio")
        self.client = httpx.Client(
            base_url=f"{GOOGLE_AI_STUDIO_API_URL}/",
            headers={"x-goog-api-key": api_key},
            timeout=60.0,
        )

    def _iter_audio_deltas(self, response: httpx.Response) -> Iterable[bytes]:
        for data in iter_sse_data(response.iter_lines()):
            try:
                event = json.loads(data)
            except ValueError as e:
                raise LLMError("TTS Google AI Studio returned invalid stream JSON", e) from e
            if not isinstance(event, dict):
                raise LLMError("TTS Google AI Studio returned an invalid stream event")
            event_type = event.get("event_type")
            if not isinstance(event_type, str):
                raise LLMError("TTS Google AI Studio stream event did not contain an event type")
            if event_type in {"error", "interaction.failed", "interaction.cancelled"} or event.get("error"):
                raise LLMError(f"TTS Google AI Studio stream error: {event.get('error') or event}")
            if event_type == "interaction.completed":
                interaction = event.get("interaction")
                if not isinstance(interaction, dict) or interaction.get("status") != "completed":
                    raise LLMError(f"TTS Google AI Studio interaction did not complete successfully: {interaction}")
                return
            if event_type != "step.delta":
                continue
            delta = event.get("delta")
            if not isinstance(delta, dict):
                raise LLMError("TTS Google AI Studio stream event did not contain a delta")
            if delta.get("type") != "audio":
                continue
            mime_type = delta.get("mime_type")
            if mime_type is not None:
                if not isinstance(mime_type, str):
                    raise LLMError("TTS Google AI Studio returned an invalid audio MIME type")
                mime = Message()
                mime["Content-Type"] = mime_type
                if (
                    mime.get_content_type() != "audio/l16"
                    or mime.get_param("rate", "24000") != "24000"
                    or mime.get_param("channels", "1") != "1"
                    or mime.get_param("codec", "pcm") != "pcm"
                ):
                    raise LLMError(f"TTS Google AI Studio returned unsupported audio format: {mime_type}")
            encoded_audio = delta.get("data")
            if not isinstance(encoded_audio, str) or not encoded_audio:
                raise LLMError("TTS Google AI Studio audio event did not contain audio data")
            try:
                yield base64.b64decode(encoded_audio, validate=True)
            except (ValueError, binascii.Error) as e:
                raise LLMError("TTS Google AI Studio returned invalid base64 audio", e) from e
        raise LLMError("TTS Google AI Studio audio stream ended before the interaction completed")

    @override
    def synthesize(self, text: str, voice: str) -> Generator[bytes, None, None]:
        return self.synthesize_with_settings(text, {"voice": voice})

    @override
    def synthesize_with_settings(self, text: str, settings: dict[str, Any]) -> Generator[bytes, None, None]:
        content: dict[str, Any] = {"type": "text", "text": text}
        instructions = str(settings.get("instructions") or "").strip()
        if instructions:
            content["annotations"] = [{"type": "speech_metadata", "style": instructions}]
        selected_voice = str(settings.get("voice") or "").strip() or GOOGLE_TTS_VOICE
        payload = {
            "model": self.model_name,
            "input": [{"type": "user_input", "content": [content]}],
            "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000},
            "generation_config": {"speech_config": [{"voice": selected_voice}]},
            "stream": True,
            "store": False,
        }
        try:
            with self.client.stream(
                "POST", "interactions",
                headers={"Accept": "text/event-stream"},
                json=payload,
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";")[0].strip() != "text/event-stream":
                    raise LLMError("TTS Google AI Studio did not return an SSE audio stream")
                buffer = bytearray()
                received_audio = False
                for delta in self._iter_audio_deltas(response):
                    received_audio = True
                    buffer.extend(delta)
                    while len(buffer) >= 1024:
                        yield bytes(buffer[:1024])
                        del buffer[:1024]
                if not received_audio:
                    raise LLMError("TTS Google AI Studio returned no audio; check model access and content restrictions")
                if len(buffer) % 2:
                    raise LLMError("TTS Google AI Studio returned incomplete 16-bit PCM audio")
                if buffer:
                    yield bytes(buffer)
        except httpx.HTTPStatusError as e:
            e.response.read()
            try:
                detail = e.response.json()
            except ValueError:
                detail = e.response.text
            raise LLMError(f"TTS Google AI Studio HTTP {e.response.status_code}: {detail}", e) from e
        except httpx.RequestError as e:
            raise LLMError(f"TTS Google AI Studio request failed: {e}", e) from e


def _fields(prefix: str) -> list[dict[str, Any]]:
    return [
        text_field("api_key", "Google AI Studio API Key", "", hidden=True),
        text_field(f"{prefix}_api_key", "Override API Key", "", hidden=True),
    ]


class GoogleAIStudioPlugin(PluginBase):
    @override
    def __init__(self, plugin_manifest: PluginManifest):
        super().__init__(plugin_manifest)
        providers: list[dict[str, Any]] = []
        for provider_id, prefix, slot, title, model, reasoning in (
            ("llm", "llm", "llm", "Google AI Studio LLM", GOOGLE_LLM_MODEL, "none"),
            ("agent-llm", "agent_llm", "agent_llm", "Google AI Studio Agent LLM", GOOGLE_AGENT_LLM_MODEL, "low"),
        ):
            providers.append({
                "kind": "llm", "id": provider_id, "label": "Google AI Studio", "slots": [slot],
                "settings_config": [{"key": prefix, "label": title, "fields": [
                    *_fields(prefix), text_field(f"{prefix}_model", "Model", model),
                    number_field(f"{prefix}_temperature", "Temperature", 1.0, 0.0, 2.0, 0.01),
                    select_field(f"{prefix}_reasoning_effort", "Reasoning Effort", reasoning, ["default", "none", "minimal", "low", "medium", "high"]),
                ]}],
            })
        providers.extend([
            {
                "kind": "vlm", "id": "vlm", "label": "Google AI Studio", "slots": ["vision"],
                "settings_config": [{"key": "vlm", "label": "Google AI Studio Vision", "fields": [
                    *_fields("vlm"), text_field("vlm_model", "Model", GOOGLE_VLM_MODEL),
                    number_field("vlm_temperature", "Temperature", 1.0, 0.0, 2.0, 0.01),
                    select_field("vlm_reasoning_effort", "Reasoning Effort", "none", ["default", "none", "minimal", "low", "medium", "high"]),
                ]}],
            },
            {
                "kind": "stt", "id": "stt", "label": "Google AI Studio", "slots": ["stt"],
                "settings_config": [{"key": "stt", "label": "Google AI Studio Speech-to-Text", "fields": [
                    *_fields("stt"), text_field("stt_model", "Model", GOOGLE_STT_MODEL),
                    text_field("stt_prompt", "Prompt", ""),
                    paragraph("Audio is sent to the selected Gemini model as multimodal conversation input."),
                ]}],
            },
            {
                "kind": "tts", "id": "tts", "label": "Google AI Studio", "slots": ["tts"],
                "settings_config": [{"key": "tts", "label": "Google AI Studio Text-to-Speech", "fields": [
                    *_fields("tts"), text_field("tts_model", "Model", GOOGLE_TTS_MODEL),
                    paragraph("Gemini 3.8 TTS streams 24 kHz mono PCM through the native Interactions API. Requires model access. Set character Voice Instructions for tone, accent and pace; instructions are not read aloud."),
                ]}],
                "voice_settings_config": [{"key": "voice", "label": "Google AI Studio Voice", "fields": [
                    select_field("voice", "Voice", GOOGLE_TTS_VOICE, GOOGLE_TTS_VOICES),
                    textarea_field("instructions", "Voice Instructions", ""),
                ]}],
            },
            {
                "kind": "embedding", "id": "embedding", "label": "Google AI Studio", "slots": ["embedding"],
                "settings_config": [{"key": "embedding", "label": "Google AI Studio Embeddings", "fields": [
                    *_fields("embedding"), text_field("embedding_model", "Model", GOOGLE_EMBEDDING_MODEL),
                ]}],
            },
        ])
        providers[0]["api_key_detection"] = {
            "patterns": [r"^AIza[A-Za-z0-9_-]{35}$", r"^AQ[A-Za-z0-9_-]{30,}$"],
            "priority": 20,
            "setting_key": "api_key",
            "provider_selections": {
                "llm_provider": "llm", "agent_llm_provider": "agent-llm",
                "vision_provider": "vlm", "stt_provider": "stt",
                "tts_provider": f"plugin:{EDGE_TTS_PLUGIN_GUID}:tts",
                "embedding_provider": "embedding",
            },
        }
        self.model_providers = providers  # type: ignore[assignment]

    @override
    def create_model(self, provider_id: str, settings: dict[str, Any]):
        if provider_id in {"llm", "agent-llm", "vlm"}:
            prefix = "agent_llm" if provider_id == "agent-llm" else provider_id
            defaults = {
                "llm": (GOOGLE_LLM_MODEL, "none"),
                "agent_llm": (GOOGLE_AGENT_LLM_MODEL, "low"),
                "vlm": (GOOGLE_VLM_MODEL, "none"),
            }
            model, reasoning = defaults[prefix]
            return GoogleAIStudioLLMModel(
                GOOGLE_AI_STUDIO_API_URL, api_key(settings, prefix),
                string_setting(settings, f"{prefix}_model", model),
                float_setting(settings, f"{prefix}_temperature", 1.0),
                reasoning_effort=string_setting(settings, f"{prefix}_reasoning_effort", reasoning),
                provider_name="google-ai-studio",
            )
        if provider_id == "stt":
            return OpenAIMultiModalSTTModel(
                GOOGLE_AI_STUDIO_API_URL, api_key(settings, "stt"),
                string_setting(settings, "stt_model", GOOGLE_STT_MODEL),
                str(settings.get("stt_prompt") or "") or None,
                provider_name="google-ai-studio",
            )
        if provider_id == "embedding":
            return OpenAIEmbeddingModel(
                GOOGLE_AI_STUDIO_API_URL, api_key(settings, "embedding"),
                string_setting(settings, "embedding_model", GOOGLE_EMBEDDING_MODEL),
            )
        if provider_id == "tts":
            return GoogleAIStudioTTSModel(
                api_key(settings, "tts"),
                string_setting(settings, "tts_model", GOOGLE_TTS_MODEL),
            )
        raise ValueError(f"Unknown Google AI Studio model provider: {provider_id}")
