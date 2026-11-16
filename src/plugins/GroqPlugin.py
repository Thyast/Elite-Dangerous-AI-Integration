import io
import re
import wave
from typing import Any, Iterable, override

import httpx
import numpy as np

from lib.Models import LLMError, OpenAISTTModel, TTSModel
from lib.PluginBase import PluginBase, PluginManifest
from plugins.ProviderPluginHelpers import (
    ToolToggleOpenAILLMModel,
    api_key,
    bool_setting,
    float_setting,
    number_field,
    paragraph,
    select_field,
    string_setting,
    text_field,
    toggle_field,
)


GROQ_PLUGIN_GUID = "43a2a263-b899-40f6-b06d-560dfdfd8cc6"
GROQ_API_URL = "https://api.groq.com/openai/v1"
GROQ_LLM_MODEL = "openai/gpt-oss-120b"
GROQ_LLM_MODELS = [GROQ_LLM_MODEL, "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
GROQ_STT_MODEL = "whisper-large-v3-turbo"
GROQ_STT_MODELS = [GROQ_STT_MODEL, "whisper-large-v3"]
GROQ_TTS_MODEL = "canopylabs/orpheus-v1-english"
GROQ_TTS_VOICES = {
    GROQ_TTS_MODEL: ["troy", "austin", "daniel", "autumn", "diana", "hannah"],
    "canopylabs/orpheus-arabic-saudi": ["fahad", "abdullah", "sultan", "lulwa", "noura", "aisha"],
}
GROQ_TTS_MAX_CHARS = 200
TTS_SAMPLE_RATE = 24000
PCM_CHUNK_BYTES = 1024


def split_tts_text(text: str, limit: int = GROQ_TTS_MAX_CHARS) -> list[str]:
    """Split text at sentence, then word boundaries so each request fits Groq's input limit."""
    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?;:…])\s+", " ".join(text.split())):
        for word in sentence.split(" "):
            while len(word) > limit:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(word[:limit])
                word = word[limit:]
            candidate = f"{current} {word}" if current else word
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = word
        if current:
            chunks.append(current)
            current = ""
    return [chunk for chunk in chunks if chunk]


def wav_to_pcm16(wav_bytes: bytes) -> bytes:
    """Convert a WAV response to the 24 kHz mono PCM16 stream expected by playback."""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as wav:
            rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as e:
        raise LLMError(f"TTS Groq returned invalid WAV audio: {e}", e)

    if width == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise LLMError(f"TTS Groq returned unsupported {width * 8}-bit WAV audio")
    if channels > 1:
        samples = samples[: len(samples) - len(samples) % channels].reshape(-1, channels).mean(axis=1)
    if rate != TTS_SAMPLE_RATE and len(samples):
        target_length = max(1, round(len(samples) * TTS_SAMPLE_RATE / rate))
        positions = np.linspace(0, len(samples) - 1, target_length)
        samples = np.interp(positions, np.arange(len(samples)), samples)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class GroqTTSModel(TTSModel):
    def __init__(self, api_key: str, model_name: str):
        super().__init__(model_name, provider_name="groq")
        self.client = httpx.Client(
            base_url=f"{GROQ_API_URL}/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )

    def _voice(self, voice: str) -> str:
        voices = GROQ_TTS_VOICES.get(self.model_name)
        selected = voice.strip().lower()
        if voices and selected not in voices:
            return voices[0]
        return selected or "troy"

    @override
    def synthesize(self, text: str, voice: str) -> Iterable[bytes]:
        selected_voice = self._voice(voice)
        for chunk in split_tts_text(text):
            try:
                response = self.client.post("audio/speech", json={
                    "model": self.model_name,
                    "voice": selected_voice,
                    "input": chunk,
                    "response_format": "wav",
                })
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                try:
                    detail = e.response.json().get("error", {}).get("message") or e.response.text
                except Exception:
                    detail = e.response.text
                raise LLMError(f"TTS Groq HTTP {e.response.status_code}: {detail}", e)
            except Exception as e:
                raise LLMError(f"TTS Groq error: {e}", e)
            pcm = wav_to_pcm16(response.content)
            for start in range(0, len(pcm), PCM_CHUNK_BYTES):
                yield pcm[start:start + PCM_CHUNK_BYTES]


def _llm_definition(provider_id: str, prefix: str, slot: str, title: str) -> dict[str, Any]:
    return {
        "kind": "llm", "id": provider_id, "label": "Groq", "slots": [slot],
        "settings_config": [{"key": prefix, "label": title, "fields": [
            text_field("api_key", "Groq API Key", "", hidden=True),
            select_field(f"{prefix}_model", "Model", GROQ_LLM_MODEL, GROQ_LLM_MODELS),
            number_field(f"{prefix}_temperature", "Temperature", 1.0, 0.0, 2.0, 0.01),
            select_field(f"{prefix}_reasoning_effort", "Reasoning Effort", "default", ["default", "none", "low", "medium", "high"]),
            toggle_field(f"{prefix}_tools_enabled", "Enable Tool Use", True),
            paragraph("GPT-OSS models accept low, medium or high reasoning; Qwen accepts none or default."),
        ]}],
    }


class GroqPlugin(PluginBase):
    @override
    def __init__(self, plugin_manifest: PluginManifest):
        super().__init__(plugin_manifest)
        self.model_providers = [  # type: ignore[assignment]
            _llm_definition("llm", "llm", "llm", "Groq LLM"),
            _llm_definition("agent-llm", "agent_llm", "agent_llm", "Groq Agent LLM"),
            {
                "kind": "stt", "id": "stt", "label": "Groq", "slots": ["stt"],
                "settings_config": [{"key": "stt", "label": "Groq Speech-to-Text", "fields": [
                    text_field("api_key", "Groq API Key", "", hidden=True),
                    select_field("stt_model", "Model", GROQ_STT_MODEL, GROQ_STT_MODELS),
                    paragraph("whisper-large-v3-turbo is faster and cheaper; whisper-large-v3 is more accurate."),
                    text_field("stt_language", "Language (ISO-639-1 code, e.g. fr)", ""),
                    text_field("stt_prompt", "Prompt", ""),
                ]}],
            },
            {
                "kind": "tts", "id": "tts", "label": "Groq", "slots": ["tts"],
                "settings_config": [{"key": "tts", "label": "Groq Text-to-Speech", "fields": [
                    text_field("api_key", "Groq API Key", "", hidden=True),
                    select_field("tts_model", "Model", GROQ_TTS_MODEL, list(GROQ_TTS_VOICES)),
                    paragraph("Groq voices only speak English or Saudi Arabic. Accept the model terms in the Groq console before first use."),
                ]}],
                "voice_settings_config": [{"key": "voice", "label": "Groq Voice", "fields": [
                    select_field("voice", "Voice", "troy", [voice for voices in GROQ_TTS_VOICES.values() for voice in voices]),
                ]}],
            },
        ]

    @override
    def create_model(self, provider_id: str, settings: dict[str, Any]):
        if provider_id in {"llm", "agent-llm"}:
            prefix = "agent_llm" if provider_id == "agent-llm" else "llm"
            return ToolToggleOpenAILLMModel(
                GROQ_API_URL,
                api_key(settings, prefix),
                string_setting(settings, f"{prefix}_model", GROQ_LLM_MODEL),
                float_setting(settings, f"{prefix}_temperature", 1.0),
                reasoning_effort=string_setting(settings, f"{prefix}_reasoning_effort", "default"),
                provider_name="groq",
                tools_enabled=bool_setting(settings, f"{prefix}_tools_enabled", True),
            )
        if provider_id == "stt":
            return OpenAISTTModel(
                GROQ_API_URL,
                api_key(settings, "stt"),
                string_setting(settings, "stt_model", GROQ_STT_MODEL),
                str(settings.get("stt_language") or "").strip() or None,
                str(settings.get("stt_prompt") or "") or None,
                provider_name="groq",
            )
        if provider_id == "tts":
            return GroqTTSModel(api_key(settings, "tts"), string_setting(settings, "tts_model", GROQ_TTS_MODEL))
        raise ValueError(f"Unknown Groq model provider: {provider_id}")
