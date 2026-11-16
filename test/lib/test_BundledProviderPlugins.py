import io
import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI


ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from lib.Models import (
    LLMError,
    OpenAIEmbeddingModel,
    OpenAILLMModel,
    OpenAIMultiModalSTTModel,
    OpenAIResponsesLLMModel,
    OpenAISTTModel,
    OpenAITTSModel,
)
from lib.PluginBase import PluginManifest
from plugins.EdgeTTSPlugin import EDGE_TTS_PLUGIN_GUID, EdgeTTSModel, EdgeTTSPlugin
from plugins.GoogleAIStudioPlugin import GOOGLE_AI_STUDIO_API_URL, GOOGLE_AI_STUDIO_PLUGIN_GUID, GoogleAIStudioPlugin, GoogleAIStudioLLMModel, GoogleAIStudioTTSModel
from plugins.GroqPlugin import (
    GROQ_API_URL,
    GROQ_LLM_MODEL,
    GROQ_PLUGIN_GUID,
    GROQ_STT_MODEL,
    GROQ_TTS_MODEL,
    GroqPlugin,
    GroqTTSModel,
    split_tts_text,
    wav_to_pcm16,
)
from plugins.MistralPlugin import MISTRAL_API_URL, MISTRAL_PLUGIN_GUID, MistralPlugin
from plugins.OpenAICompatiblePlugin import OPENAI_COMPATIBLE_PLUGIN_GUID, OpenAICompatiblePlugin
from plugins.OpenAIPlugin import OPENAI_API_URL, OPENAI_PLUGIN_GUID, OpenAIPlugin
from plugins.OpenRouterPlugin import OPENROUTER_API_URL, OPENROUTER_PLUGIN_GUID, OpenRouterPlugin
from plugins.ProviderPluginHelpers import ToolToggleOpenAILLMModel


def manifest(guid: str, name: str) -> PluginManifest:
    return PluginManifest(json.dumps({
        "guid": guid,
        "name": name,
        "author": "tests",
        "version": "1.0.0",
        "repository": "",
    }))


def provider_ids(plugin: Any) -> set[str]:
    return {provider["id"] for provider in plugin.model_providers or []}


def setting_keys(plugin: Any) -> set[str]:
    return {
        field["key"]
        for provider in plugin.model_providers or []
        for grid in provider["settings_config"]
        for field in grid["fields"]
        if "key" in field
    }


def voice_setting_keys(plugin: Any, provider_id: str) -> set[str]:
    provider = next(provider for provider in plugin.model_providers or [] if provider["id"] == provider_id)
    return {
        field["key"]
        for grid in provider.get("voice_settings_config", [])
        for field in grid["fields"]
        if "key" in field
    }


def test_bundled_tts_providers_define_character_scoped_voice_settings() -> None:
    assert voice_setting_keys(OpenAIPlugin(manifest(OPENAI_PLUGIN_GUID, "OpenAI")), "tts") == {
        "voice", "instructions",
    }
    assert voice_setting_keys(EdgeTTSPlugin(manifest(EDGE_TTS_PLUGIN_GUID, "Edge")), "tts") == {"voice"}
    assert voice_setting_keys(MistralPlugin(manifest(MISTRAL_PLUGIN_GUID, "Mistral")), "tts") == {"voice"}
    assert voice_setting_keys(GoogleAIStudioPlugin(manifest(GOOGLE_AI_STUDIO_PLUGIN_GUID, "Google")), "tts") == {
        "voice", "instructions",
    }
    compatible = OpenAICompatiblePlugin(manifest(OPENAI_COMPATIBLE_PLUGIN_GUID, "Compatible"))
    assert voice_setting_keys(compatible, "custom-tts") == {"voice", "instructions"}
    assert voice_setting_keys(compatible, "local-tts") == {"voice", "instructions"}


def test_fixed_providers_do_not_expose_or_accept_endpoint_overrides() -> None:
    fixed_providers = (
        (OpenAIPlugin(manifest(OPENAI_PLUGIN_GUID, "OpenAI")), "llm_endpoint", OPENAI_API_URL),
        (GoogleAIStudioPlugin(manifest(GOOGLE_AI_STUDIO_PLUGIN_GUID, "Google")), "llm_endpoint", GOOGLE_AI_STUDIO_API_URL),
        (OpenRouterPlugin(manifest(OPENROUTER_PLUGIN_GUID, "OpenRouter")), "llm_endpoint", OPENROUTER_API_URL),
        (MistralPlugin(manifest(MISTRAL_PLUGIN_GUID, "Mistral")), "endpoint", MISTRAL_API_URL),
    )

    for plugin, stale_key, expected_endpoint in fixed_providers:
        assert not any(key == "endpoint" or key.endswith("_endpoint") for key in setting_keys(plugin))
        model = plugin.create_model("llm", {stale_key: "https://invalid.example/v1"})
        assert str(model.client.base_url).rstrip("/") == expected_endpoint.rstrip("/")


def test_openai_compatible_endpoints_remain_configurable() -> None:
    plugin = OpenAICompatiblePlugin(manifest(OPENAI_COMPATIBLE_PLUGIN_GUID, "Compatible"))
    assert {"custom_llm_endpoint", "local_llm_endpoint"} <= setting_keys(plugin)

    model = plugin.create_model("custom-llm", {"custom_llm_endpoint": "https://example.test/v1"})
    assert str(model.client.base_url) == "https://example.test/v1/"


def test_openai_provider_definitions_and_models() -> None:
    plugin = OpenAIPlugin(manifest(OPENAI_PLUGIN_GUID, "OpenAI"))
    assert provider_ids(plugin) == {"llm", "agent-llm", "vlm", "stt", "tts", "embedding"}
    assert isinstance(plugin.create_model("llm", {}), OpenAIResponsesLLMModel)
    assert isinstance(plugin.create_model("agent-llm", {}), OpenAIResponsesLLMModel)
    assert isinstance(plugin.create_model("stt", {}), OpenAISTTModel)
    assert isinstance(plugin.create_model("tts", {}), OpenAITTSModel)
    assert isinstance(plugin.create_model("embedding", {}), OpenAIEmbeddingModel)


def test_google_provider_definitions_and_models() -> None:
    plugin = GoogleAIStudioPlugin(manifest(GOOGLE_AI_STUDIO_PLUGIN_GUID, "Google"))
    assert provider_ids(plugin) == {"llm", "agent-llm", "vlm", "stt", "tts", "embedding"}
    assert isinstance(plugin.create_model("llm", {}), GoogleAIStudioLLMModel)
    assert isinstance(plugin.create_model("stt", {}), OpenAIMultiModalSTTModel)
    assert isinstance(plugin.create_model("tts", {}), GoogleAIStudioTTSModel)
    assert isinstance(plugin.create_model("embedding", {}), OpenAIEmbeddingModel)


@pytest.mark.parametrize("override_key", ["", "embedding-test-key"])
def test_google_embeddings_use_google_endpoint_and_configured_key(override_key: str) -> None:
    plugin = GoogleAIStudioPlugin(manifest(GOOGLE_AI_STUDIO_PLUGIN_GUID, "Google"))
    model = plugin.create_model("embedding", {
        "api_key": "shared-google-test-key",
        "embedding_api_key": override_key,
    })
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "object": "list",
            "model": "gemini-embedding-001",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.25, 0.75]}],
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        })

    base_url = model.client.base_url
    key = model.client.api_key
    model.client.close()
    with OpenAI(
        base_url=base_url,
        api_key=key,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        model.client = client
        assert model.create_embedding("Synthetic memory") == ("gemini-embedding-001", [0.25, 0.75])

    assert len(requests) == 1
    assert requests[0].url.host == "generativelanguage.googleapis.com"
    assert requests[0].url.path.endswith("/embeddings")
    assert requests[0].headers["Authorization"] == f"Bearer {override_key or 'shared-google-test-key'}"
    assert json.loads(requests[0].content)["model"] == "gemini-embedding-001"


def test_openrouter_slots_and_models_are_independent() -> None:
    plugin = OpenRouterPlugin(manifest(OPENROUTER_PLUGIN_GUID, "OpenRouter"))
    providers = {provider["id"]: provider for provider in plugin.model_providers or []}
    assert providers["llm"]["slots"] == ["llm"]
    assert providers["agent-llm"]["slots"] == ["agent_llm"]
    settings = {"llm_model": "main", "agent_llm_model": "agent"}
    main = plugin.create_model("llm", settings)
    agent = plugin.create_model("agent-llm", settings)
    assert isinstance(main, OpenAILLMModel)
    assert main.model_name == "main"
    assert agent.model_name == "agent"


def test_openrouter_tool_toggle_suppresses_tools_per_role() -> None:
    plugin = OpenRouterPlugin(manifest(OPENROUTER_PLUGIN_GUID, "OpenRouter"))
    providers = {provider["id"]: provider for provider in plugin.model_providers or []}
    main_fields = providers["llm"]["settings_config"][0]["fields"]
    assert any(field["key"] == "llm_tools_enabled" for field in main_fields)
    model = plugin.create_model("llm", {"llm_tools_enabled": False})
    assert isinstance(model, ToolToggleOpenAILLMModel)

    tools = [{"type": "function", "function": {"name": "test"}}]
    with patch.object(OpenAILLMModel, "generate", return_value=("ok", None, None)) as generate:
        model.generate([{"role": "user", "content": "hello"}], tools, "auto")

    generate.assert_called_once_with([{"role": "user", "content": "hello"}], None, None)


def test_groq_stt_uses_fixed_endpoint_and_whisper_settings() -> None:
    plugin = GroqPlugin(manifest(GROQ_PLUGIN_GUID, "Groq"))
    providers = {provider["id"]: provider for provider in plugin.model_providers or []}
    assert {provider_id: provider["slots"] for provider_id, provider in providers.items()} == {
        "llm": ["llm"], "agent-llm": ["agent_llm"], "stt": ["stt"], "tts": ["tts"],
    }
    assert not any(key == "endpoint" or key.endswith("_endpoint") for key in setting_keys(plugin))

    default = plugin.create_model("stt", {"api_key": "groq-secret"})
    assert isinstance(default, OpenAISTTModel)
    assert str(default.client.base_url).rstrip("/") == GROQ_API_URL
    assert default.client.api_key == "groq-secret"
    assert default.model_name == GROQ_STT_MODEL
    assert default.language is None and default.prompt is None

    configured = plugin.create_model("stt", {
        "stt_model": "whisper-large-v3", "stt_language": " fr ", "stt_prompt": "Elite Dangerous",
    })
    assert configured.model_name == "whisper-large-v3"
    assert configured.language == "fr"
    assert configured.prompt == "Elite Dangerous"
    with pytest.raises(ValueError):
        plugin.create_model("vlm", {})


def test_groq_llm_roles_use_independent_models_and_tool_toggle() -> None:
    plugin = GroqPlugin(manifest(GROQ_PLUGIN_GUID, "Groq"))
    main = plugin.create_model("llm", {"api_key": "groq-secret"})
    agent = plugin.create_model("agent-llm", {
        "agent_llm_model": "openai/gpt-oss-20b", "agent_llm_reasoning_effort": "low",
        "agent_llm_tools_enabled": False,
    })
    assert isinstance(main, ToolToggleOpenAILLMModel)
    assert str(main.client.base_url).rstrip("/") == GROQ_API_URL
    assert main.client.api_key == "groq-secret"
    assert main.model_name == GROQ_LLM_MODEL and main.tools_enabled
    assert agent.model_name == "openai/gpt-oss-20b"
    assert agent.reasoning_effort == "low" and not agent.tools_enabled


def wav_bytes(samples: Any, rate: int, channels: int = 1) -> bytes:
    import numpy as np
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buffer.getvalue()


def test_groq_tts_text_split_respects_request_limit() -> None:
    text = "Première phrase courte. " + "mot " * 80 + "x" * 450
    chunks = split_tts_text(text)
    assert chunks[0] == "Première phrase courte."
    assert all(0 < len(chunk) <= 200 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_groq_wav_conversion_downmixes_and_resamples_to_playback_format() -> None:
    import numpy as np

    stereo = np.array([[1000, 3000]] * 480, dtype="<i2").reshape(-1)
    pcm = np.frombuffer(wav_to_pcm16(wav_bytes(stereo, 48000, channels=2)), dtype="<i2")
    assert len(pcm) == 240
    assert np.all(np.abs(pcm - 2000) <= 1)
    with pytest.raises(LLMError):
        wav_to_pcm16(b"not a wav")


def test_groq_tts_requests_each_chunk_and_maps_voice_to_model() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer groq-secret"
        return httpx.Response(200, content=wav_bytes([100] * 10, 24000))

    plugin = GroqPlugin(manifest(GROQ_PLUGIN_GUID, "Groq"))
    model = plugin.create_model("tts", {"api_key": "groq-secret"})
    assert isinstance(model, GroqTTSModel)
    model.client = httpx.Client(base_url=f"{GROQ_API_URL}/", headers=model.client.headers,
                                transport=httpx.MockTransport(handler))
    audio = b"".join(model.synthesize("a " * 150, "fahad"))

    assert len(audio) == 2 * 10 * 2
    assert [request["voice"] for request in requests] == ["troy", "troy"]
    assert all(request["model"] == GROQ_TTS_MODEL and request["response_format"] == "wav" for request in requests)

    model.client = httpx.Client(base_url=f"{GROQ_API_URL}/", transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": {"message": "requires terms acceptance"}})))
    with pytest.raises(LLMError, match="requires terms acceptance"):
        list(model.synthesize("hello", "troy"))


def test_openai_compatible_models_cover_existing_modalities() -> None:
    plugin = OpenAICompatiblePlugin(manifest(OPENAI_COMPATIBLE_PLUGIN_GUID, "Compatible"))
    assert isinstance(plugin.create_model("custom-llm", {}), OpenAILLMModel)
    assert isinstance(plugin.create_model("custom-stt", {}), OpenAISTTModel)
    assert isinstance(plugin.create_model("custom-multimodal-stt", {}), OpenAIMultiModalSTTModel)
    assert isinstance(plugin.create_model("custom-tts", {}), OpenAITTSModel)
    assert isinstance(plugin.create_model("local-embedding", {}), OpenAIEmbeddingModel)


def test_openai_compatible_tool_toggle_defaults_on_and_can_be_disabled() -> None:
    plugin = OpenAICompatiblePlugin(manifest(OPENAI_COMPATIBLE_PLUGIN_GUID, "Compatible"))
    providers = {provider["id"]: provider for provider in plugin.model_providers or []}
    fields = providers["custom-llm"]["settings_config"][0]["fields"]
    assert any(field["key"] == "custom_llm_tools_enabled" for field in fields)
    tools = [{"type": "function", "function": {"name": "test"}}]

    enabled = plugin.create_model("custom-llm", {})
    with patch.object(OpenAILLMModel, "generate", return_value=("ok", None, None)) as generate:
        enabled.generate([{"role": "user", "content": "hello"}], tools, "auto")
    generate.assert_called_once_with([{"role": "user", "content": "hello"}], tools, "auto")

    disabled = plugin.create_model("custom-llm", {"custom_llm_tools_enabled": False})
    with patch.object(OpenAILLMModel, "generate", return_value=("ok", None, None)) as generate:
        disabled.generate([{"role": "user", "content": "hello"}], tools, "auto")
    generate.assert_called_once_with([{"role": "user", "content": "hello"}], None, None)

    local_fields = providers["local-llm"]["settings_config"][0]["fields"]
    assert not any(field["key"] == "local_llm_tools_enabled" for field in local_fields)
    assert type(plugin.create_model("local-llm", {})) is OpenAILLMModel


def test_edge_tts_provider_uses_existing_model() -> None:
    plugin = EdgeTTSPlugin(manifest(EDGE_TTS_PLUGIN_GUID, "Edge"))
    model = plugin.create_model("tts", {})
    assert isinstance(model, EdgeTTSModel)
    assert model.model_name == "edge-tts"
