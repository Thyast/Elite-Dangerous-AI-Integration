import base64
from collections.abc import Iterable, Iterator
import json
from pathlib import Path
import sys
from typing import Any

import httpx
import numpy as np
import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from lib.Models import LLMError
from lib.PluginBase import PluginManifest
from plugins.EdgeTTSPlugin import EDGE_TTS_PLUGIN_GUID
from plugins.GoogleAIStudioPlugin import (
    GOOGLE_AI_STUDIO_API_URL,
    GOOGLE_AI_STUDIO_PLUGIN_GUID,
    GOOGLE_TTS_MODEL,
    GOOGLE_TTS_VOICE,
    GOOGLE_TTS_VOICES,
    GoogleAIStudioPlugin,
    GoogleAIStudioTTSModel,
)
from plugins.ProviderPluginHelpers import iter_sse_data


def audio_event(data: bytes, mime_type: str | None = "audio/l16") -> dict[str, Any]:
    delta: dict[str, Any] = {"type": "audio", "data": base64.b64encode(data).decode("ascii")}
    if mime_type is not None:
        delta["mime_type"] = mime_type
    return {"event_type": "step.delta", "index": 0, "delta": delta}


def completed_event(status: str = "completed") -> dict[str, Any]:
    return {"event_type": "interaction.completed", "interaction": {"status": status}}


class EventStream(httpx.SyncByteStream):
    def __init__(self, chunks: Iterable[bytes]):
        self.chunks = list(chunks)
        self.read_count = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def make_model():
    clients: list[httpx.Client] = []

    def create(
        events: Iterable[Any],
        *,
        content_type: str = "text/event-stream; charset=utf-8",
    ) -> tuple[GoogleAIStudioTTSModel, list[httpx.Request], EventStream]:
        stream = EventStream(
            f"event: {event.get('event_type', 'message') if isinstance(event, dict) else 'message'}\r\n"
            f"data: {json.dumps(event)}\r\n\r\n".encode("utf-8")
            for event in events
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, stream=stream, headers={"Content-Type": content_type})

        model = GoogleAIStudioTTSModel("test-google-key")
        base_url, headers = model.client.base_url, model.client.headers
        model.client.close()
        model.client = httpx.Client(
            base_url=base_url, headers=headers, transport=httpx.MockTransport(respond),
        )
        clients.append(model.client)
        return model, requests, stream

    yield create
    for client in clients:
        client.close()


def test_google_tts_uses_native_streaming_api_and_character_style(make_model):
    pcm = np.array([-32768, -100, 0, 100, 32767], dtype="<i2").tobytes()
    model, requests, stream = make_model([audio_event(pcm), completed_event()])

    assert b"".join(model.synthesize_with_settings("Bonjour Commandant.", {
        "voice": "Sulafat", "instructions": "Speak French, warmly and calmly.",
    })) == pcm

    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{GOOGLE_AI_STUDIO_API_URL}/interactions"
    assert request.headers["x-goog-api-key"] == "test-google-key"
    assert request.headers["accept"] == "text/event-stream"
    assert json.loads(request.content) == {
        "model": "gemini-3.8-flash-lite-tts",
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text", "text": "Bonjour Commandant.",
                "annotations": [{"type": "speech_metadata", "style": "Speak French, warmly and calmly."}],
            }],
        }],
        "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000},
        "generation_config": {"speech_config": [{"voice": "Sulafat"}]},
        "stream": True,
        "store": False,
    }
    assert model.provider_name == "google-ai-studio"
    assert stream.closed


@pytest.mark.parametrize("voice, expected", [("", "Kore"), ("  ", "Kore"), ("  Puck ", "Puck"), ("voice_custom", "voice_custom")])
def test_google_tts_default_and_custom_voice(make_model, voice, expected):
    model, requests, _ = make_model([audio_event(b"\x01\x00"), completed_event()])
    assert list(model.synthesize("Test", voice)) == [b"\x01\x00"]
    payload = json.loads(requests[0].content)
    assert payload["generation_config"]["speech_config"] == [{"voice": expected}]
    assert payload["input"][0]["content"] == [{"type": "text", "text": "Test"}]


def test_google_tts_does_not_retain_other_characters_style(make_model):
    model, requests, _ = make_model([audio_event(b"\x00\x00"), completed_event()])
    assert list(model.synthesize_with_settings("One", {"instructions": "Whisper."}))
    assert list(model.synthesize("Two", "Kore"))
    assert "annotations" in json.loads(requests[0].content)["input"][0]["content"][0]
    assert "annotations" not in json.loads(requests[1].content)["input"][0]["content"][0]


@pytest.mark.parametrize("fragment_size", [1, 3, 137, 1024, 4096])
def test_google_tts_preserves_pcm_samples_across_audio_fragments(make_model, fragment_size):
    pcm = (np.sin(2 * np.pi * 1000 * np.arange(6000) / 24000) * 12000).astype("<i2").tobytes()
    events = [audio_event(pcm[offset:offset + fragment_size]) for offset in range(0, len(pcm), fragment_size)]
    model, _, _ = make_model([*events, completed_event()])

    chunks = list(model.synthesize("Test", "Kore"))

    assert all(0 < len(chunk) <= 1024 and len(chunk) % 2 == 0 for chunk in chunks)
    assert b"".join(chunks) == pcm
    samples = np.frombuffer(b"".join(chunks), dtype="<i2")
    assert len(samples) / 24000 == 0.25
    frequencies = np.fft.rfftfreq(len(samples), d=1 / 24000)
    assert frequencies[np.argmax(np.abs(np.fft.rfft(samples)))] == 1000


def test_google_tts_streams_before_completion_and_closes_on_early_stop(make_model):
    model, _, stream = make_model([
        audio_event(b"\x01\x00" * 512),
        audio_event(b"\x02\x00" * 512),
        completed_event(),
    ])
    synthesis = model.synthesize("Test", "Kore")
    assert next(synthesis) == b"\x01\x00" * 512
    assert stream.read_count == 1
    assert not stream.closed
    synthesis.close()
    assert stream.closed


@pytest.mark.parametrize("mime_type", [None, "audio/l16", "audio/L16;codec=pcm;rate=24000;channels=1"])
def test_google_tts_accepts_expected_pcm_formats(make_model, mime_type):
    model, _, _ = make_model([audio_event(b"\x01\x00", mime_type), completed_event()])
    assert list(model.synthesize("Test", "Kore")) == [b"\x01\x00"]


@pytest.mark.parametrize("mime_type", [
    "audio/wav", "audio/mpeg", "audio/l16;rate=16000",
    "audio/l16;channels=2", "audio/l16;codec=mulaw", 123,
])
def test_google_tts_rejects_incompatible_audio_formats(make_model, mime_type):
    event = audio_event(b"\x01\x00")
    event["delta"]["mime_type"] = mime_type
    model, _, stream = make_model([event, completed_event()])
    with pytest.raises(LLMError, match="audio (format|MIME type)"):
        list(model.synthesize("Test", "Kore"))
    assert stream.closed


@pytest.mark.parametrize("events, message", [
    ([completed_event()], "returned no audio"),
    ([audio_event(b"\x00"), completed_event()], "incomplete 16-bit PCM"),
    ([audio_event(b"\x00\x00")], "ended before the interaction completed"),
    ([completed_event("failed")], "did not complete successfully"),
    ([{"event_type": "error", "error": {"code": "quota_exceeded", "message": "Quota exhausted"}}], "Quota exhausted"),
    ([{"event_type": "interaction.failed", "error": {"message": "Blocked"}}], "Blocked"),
    ([{"event_type": "step.delta", "delta": {"type": "audio"}}], "did not contain audio data"),
    ([{"event_type": "step.delta", "delta": {"type": "audio", "data": "%%%"}}], "invalid base64 audio"),
    ([{"event_type": "step.delta"}], "did not contain a delta"),
    ([{"event_type": []}], "did not contain an event type"),
    ([[]], "invalid stream event"),
    ([{"event_type": "step.delta", "delta": {"type": "text", "text": "Cannot generate"}}, completed_event()], "returned no audio"),
])
def test_google_tts_surfaces_stream_failures(make_model, events, message):
    model, _, stream = make_model(events)
    with pytest.raises(LLMError, match=message):
        list(model.synthesize("Test", "Kore"))
    assert stream.closed


def test_google_tts_rejects_non_streaming_response(make_model):
    model, _, _ = make_model([], content_type="application/json")
    with pytest.raises(LLMError, match="did not return an SSE audio stream"):
        list(model.synthesize("Test", "Kore"))


@pytest.mark.parametrize("status, body", [
    (403, {"error": {"message": "Model access denied"}}),
    (429, {"error": {"message": "Quota exhausted"}}),
    (500, "Service unavailable"),
])
def test_google_tts_reports_http_errors(status, body):
    def respond(request: httpx.Request) -> httpx.Response:
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    model = GoogleAIStudioTTSModel("test-google-key")
    model.client.close()
    with httpx.Client(base_url=f"{GOOGLE_AI_STUDIO_API_URL}/", transport=httpx.MockTransport(respond)) as client:
        model.client = client
        with pytest.raises(LLMError, match=f"Google AI Studio HTTP {status}") as caught:
            list(model.synthesize("Test", "Kore"))
        assert isinstance(caught.value.original_error, httpx.HTTPStatusError)


def test_google_tts_reports_network_timeouts():
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Timed out waiting for audio", request=request)

    model = GoogleAIStudioTTSModel("test-google-key")
    model.client.close()
    with httpx.Client(base_url=f"{GOOGLE_AI_STUDIO_API_URL}/", transport=httpx.MockTransport(respond)) as client:
        model.client = client
        with pytest.raises(LLMError, match="request failed: Timed out waiting for audio"):
            list(model.synthesize("Test", "Kore"))


@pytest.mark.parametrize("lines, expected", [
    (["event: step.delta", 'data: {"first": 1,', 'data: "second": 2}', ""], ['{"first": 1,\n"second": 2}']),
    ([": keepalive", "", 'data: {"done": true}'], ['{"done": true}']),
    (["data: one", "", "", "data:two", ""], ["one", "two"]),
])
def test_shared_sse_framing(lines, expected):
    assert list(iter_sse_data(lines)) == expected


def test_google_tts_invalid_json_is_reported():
    model = GoogleAIStudioTTSModel("test-google-key")
    try:
        response = httpx.Response(200, content="data: {invalid json}\n\n")
        with pytest.raises(LLMError, match="invalid stream JSON"):
            list(model._iter_audio_deltas(response))
    finally:
        model.client.close()


def test_google_tts_provider_settings_and_key_precedence():
    plugin = GoogleAIStudioPlugin(PluginManifest(json.dumps({
        "guid": GOOGLE_AI_STUDIO_PLUGIN_GUID, "name": "Google AI Studio",
    })))
    for settings, expected_key, expected_model in [
        ({}, "-", GOOGLE_TTS_MODEL),
        ({"api_key": "shared-key"}, "shared-key", GOOGLE_TTS_MODEL),
        ({"api_key": "shared-key", "tts_api_key": "tts-key", "tts_model": "gemini-3.8-flash-tts"}, "tts-key", "gemini-3.8-flash-tts"),
    ]:
        model = plugin.create_model("tts", settings)
        assert isinstance(model, GoogleAIStudioTTSModel)
        try:
            assert model.client.headers["x-goog-api-key"] == expected_key
            assert model.model_name == expected_model
            assert str(model.client.base_url) == f"{GOOGLE_AI_STUDIO_API_URL}/"
        finally:
            model.client.close()

    providers = json.loads(json.dumps(plugin.model_providers))
    provider = next(provider for provider in providers if provider["id"] == "tts")
    assert provider["kind"] == "tts"
    assert provider["slots"] == ["tts"]
    voice_fields = provider["voice_settings_config"][0]["fields"]
    voice_field = next(field for field in voice_fields if field["key"] == "voice")
    assert voice_field["default_value"] == GOOGLE_TTS_VOICE
    assert {option["value"] for option in voice_field["select_options"]} == set(GOOGLE_TTS_VOICES)
    assert len(GOOGLE_TTS_VOICES) == 30
    recommendations = providers[0]["api_key_detection"]["provider_selections"]
    assert recommendations["tts_provider"] == f"plugin:{EDGE_TTS_PLUGIN_GUID}:tts"
