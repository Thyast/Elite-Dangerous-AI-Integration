import time
from typing import Mapping
import pytest
from unittest.mock import MagicMock, patch, ANY
import io
import queue
from time import sleep
import openai
from src.lib.STT import STT, STTResult
from src.lib.VoiceActivation import VoiceActivation

def read_no_voice(x):
    """Return silence"""
    time.sleep(float(x)/160000)
    return b'\x00\x00' * x

def read_voice(x):
    """Return voice"""
    time.sleep(float(x)/160000)
    return b'\xff\xff' * x

@pytest.fixture
def mock_audio_input(monkeypatch):
    mock_stream = MagicMock()
    mock_stream.read.side_effect = read_no_voice
    factory = MagicMock(return_value=mock_stream)
    monkeypatch.setattr('src.lib.STT.PCMInput', factory)
    return {'audio': factory, 'stream': mock_stream}

@pytest.fixture
def mock_vad():
    """Mock SileroVAD"""
    mock = MagicMock()

    # Make the VAD callable - returns voice activity score based on audio content
    # This is used by _listen_continuous_loop which calls self.vad(buffer)
    def vad_call_side_effect(audio_bytes):
        # Check if audio contains voice (non-zero values)
        if audio_bytes and audio_bytes[0] != 0:
            return 1.0  # Voice detected (above threshold)
        return 0.0  # No voice (below threshold)
    mock.side_effect = vad_call_side_effect

    # process_chunks is used by _transcribe to filter silent audio
    def process_chunks_side_effect(audio_bytes):
        if audio_bytes and audio_bytes[0] != 0:
            return [1.0]  # Voice detected (above threshold)
        return [0.0]  # No voice (below threshold)
    mock.process_chunks.side_effect = process_chunks_side_effect
    mock.chunk_bytes.return_value = 512 * 2  # frames * sample_size
    return mock

class MockTextResponse:
    """Mock OpenAI response"""
    def __init__(self, text):
        self.text = text

@pytest.fixture
def mock_stt_model():
    mock = MagicMock()
    mock.transcribe.return_value = "Test transcription via API"
    return mock

@pytest.fixture
def mock_vad_detector():
    """Mock SileroVoiceActivityDetector class"""
    mock = MagicMock()
    return mock

@pytest.fixture
def mock_openai():
    """Mock OpenAI client (not used but referenced by test signatures)"""
    mock = MagicMock()
    return mock

@pytest.fixture
def stt(mock_stt_model, mock_vad, mock_vad_detector, mock_audio_input, monkeypatch):
    """Create STT instance with mocked dependencies"""
    monkeypatch.setattr('pysilero_vad.SileroVoiceActivityDetector', mock_vad_detector)
    stt =  STT(mock_stt_model, "TestDevice")
    stt.vad = mock_vad
    yield stt
    stt.stop()

def test_get_microphone_device_selection(mock_audio_input, stt):
    stream = stt._get_microphone()
    mock_audio_input['audio'].assert_called_once_with('TestDevice', samplerate=16000, blocksize=512)
    assert stream == mock_audio_input['stream']


def test_get_microphone_does_not_fall_back(mock_audio_input, stt):
    mock_audio_input['audio'].side_effect = ValueError('Device missing')
    with pytest.raises(ValueError, match='Device missing'):
        stt._get_microphone()
    assert mock_audio_input['audio'].call_count == 1

def test_continuous_listening(stt, mock_audio_input: dict[str, MagicMock], mock_vad: MagicMock, mock_openai, monkeypatch):
    """Test continuous listening flow"""
    monkeypatch.setattr('src.lib.STT.STT._transcribe', lambda self, audio: "Test transcription sdf")
    mock_audio_input['stream'].read.side_effect = read_no_voice

    stt.listen_continuous()
    mock_vad.return_value = 0.1

    # Wait for audio to be read
    while mock_audio_input['stream'].read.call_count < 1:
        sleep(0.001)

    # Simulate receiving voice
    mock_audio_input['stream'].read.reset()
    mock_audio_input['stream'].read.side_effect = read_voice
    while mock_audio_input['stream'].read.call_count < 16000 * 4 / 1600: # 4 seconds of audio
        sleep(0.001)
    # Voice ends after 4 seconds
    mock_audio_input['stream'].read.side_effect = read_no_voice

    # Wait for transcription
    while stt.resultQueue.empty():
        sleep(0.001)

    # Check if transcription was requested
    #assert mock_openai.audio.transcriptions.create.called

    # Verify result in queue
    result = stt.resultQueue.get()
    assert isinstance(result, STTResult)
    assert result.text == "Test transcription sdf"

def test_push_to_talk(stt, mock_audio_input, mock_openai, monkeypatch):
    """Test push-to-talk flow"""
    monkeypatch.setattr('src.lib.STT.STT._transcribe', lambda self, audio: "Test transcription asd")
    mock_audio_input['stream'].read.side_effect = read_voice
    sleep(0.01)  # Simulate button press duration

    # Start listening
    stt.listen_once_start()
    while mock_audio_input['stream'].read.call_count < 16000 * 4 / 1600: # 4 seconds of audio
        sleep(0.001)
    stt.listen_once_end()

    # Wait for transcription
    while stt.resultQueue.empty():
        sleep(0.001)

    # Check result
    result = stt.resultQueue.get()
    assert isinstance(result, STTResult)
    assert result.text == "Test transcription asd"

def test_transcribe(stt, mock_openai, mock_vad):
    """Test VAD filtering of silent audio"""
    import speech_recognition as sr

    audio = sr.AudioData(b'\x00\x00' * 16000, 16000, 2)
    res = stt._transcribe(audio)
    assert res == ''

    audio = sr.AudioData(b'\xff\xff' * 16000, 16000, 2)
    res = stt._transcribe(audio)
    assert res == 'Test transcription via API'


@pytest.mark.parametrize('manual_paused', [False, True])
@pytest.mark.parametrize('response_paused', [False, True])
@pytest.mark.parametrize('name_activation', [False, True])
def test_continuous_listening_honors_independent_pause_states(
    stt, mock_audio_input, mock_vad, mock_stt_model, manual_paused, response_paused, name_activation
):
    if name_activation:
        stt.voice_activation = VoiceActivation("Simone", 10, mock_stt_model, mock_stt_model)
    stt.pause_continuous_listening(manual_paused)
    stt.pause_continuous_listening_for_response(response_paused)
    stt.listening = True
    stt.recording = True

    def read_buffer(*args, **kwargs):
        stt.listening = False
        return b'\xff\xff' * stt.frames_per_buffer

    mock_audio_input['stream'].read.side_effect = read_buffer

    stt._listen_continuous_loop()

    assert stt.continuous_listening_paused is manual_paused
    assert stt.continuous_listening_paused_for_response is response_paused
    if manual_paused or response_paused:
        mock_vad.assert_not_called()
    else:
        mock_vad.assert_called_once()
    assert stt.recording is False
    mock_audio_input['stream'].close.assert_called_once()


def test_read_stream_buffer(stt):
    source = MagicMock()
    source.read.return_value = b'audio'
    assert stt._read_stream_buffer(source) == b'audio'
    source.read.assert_called_once_with(stt.frames_per_buffer)


def test_continuous_read_failure_closes_source(stt, mock_audio_input):
    stt.listening = True
    mock_audio_input['stream'].read.side_effect = RuntimeError('Disconnected')
    with pytest.raises(RuntimeError, match='Disconnected'):
        stt._listen_continuous_loop()
    mock_audio_input['stream'].close.assert_called_once()
    assert not stt.recording


def test_push_to_talk_read_failure_closes_source(stt, mock_audio_input):
    stt.listening = True
    mock_audio_input['stream'].read.side_effect = RuntimeError('Disconnected')
    stt._listen_once_thread()
    mock_audio_input['stream'].close.assert_called_once()
    assert not stt.listening
    assert not stt.recording


def test_voice_activation_replaces_required_word_without_erasing_it(stt, mock_stt_model):
    import speech_recognition as sr

    stt.required_word = "legacy-password"
    stt.voice_activation = VoiceActivation("Simone", 10, mock_stt_model, mock_stt_model)
    audio = sr.AudioData(b'\xff\xff' * 16000, 16000, 2)
    mock_stt_model.transcribe.return_value = "Simone, plot a route"
    stt._process_audio(audio, 1, stt.voice_activation.begin_audio())
    stt.voice_activation.end_audio()
    assert stt.resultQueue.get_nowait().text == "plot a route"
    mock_stt_model.transcribe.return_value = "avoid neutron stars"
    stt._process_audio(audio, 2, stt.voice_activation.begin_audio())
    stt.voice_activation.end_audio()
    assert stt.resultQueue.get_nowait().text == "avoid neutron stars"
    assert stt.required_word == "legacy-password"
    stt.voice_activation = None
    assert stt._transcribe(audio) == ""


def test_standby_audio_is_not_queued_or_logged(stt, mock_stt_model, caplog):
    import speech_recognition as sr

    secret = "private conversation that must not be logged"
    mock_stt_model.transcribe.return_value = secret
    stt.voice_activation = VoiceActivation("Simone", 10, mock_stt_model, mock_stt_model)
    audio = sr.AudioData(b'\xff\xff' * 16000, 16000, 2)
    stt._process_audio(audio, 1, stt.voice_activation.begin_audio())
    assert stt.resultQueue.empty()
    assert secret not in caplog.text
    assert not stt.recording
    mock_stt_model.transcribe.assert_called_once_with(audio)


@pytest.mark.parametrize("phrase", ["reste en \u00e9coute", "Restes en \u00e9coute."])
def test_keep_listening_audio_queues_control_then_followup_after_timeout(stt, mock_stt_model, phrase):
    import speech_recognition as sr

    now = [0.0]
    session = VoiceActivation("Simone", 10, mock_stt_model, mock_stt_model, clock=lambda: now[0])
    stt.voice_activation = session
    audio = sr.AudioData(b'\xff\xff' * 16000, 16000, 2)
    mock_stt_model.transcribe.return_value = f"Simone, {phrase}"
    stt._process_audio(audio, 1, session.begin_audio())
    session.end_audio()
    result = stt.resultQueue.get_nowait()
    assert result.text == ""
    assert result.voice_input.kind == "hold"
    assert session.accepts(result.voice_input)
    session.finish_control()
    session.tick(False)
    now[0] = 1000
    mock_stt_model.transcribe.return_value = "plot a route to Sol"
    stt._process_audio(audio, 2, session.begin_audio())
    session.end_audio()
    assert stt.resultQueue.get_nowait().text == "plot a route to Sol"
    mock_stt_model.transcribe.return_value = "\u00c7a sera tout"
    stt._process_audio(audio, 3, session.begin_audio())
    session.end_audio()
    assert stt.resultQueue.get_nowait().voice_input.kind == "close"
    assert session.state == "standby"
    assert stt.resultQueue.empty()
    assert mock_stt_model.transcribe.call_count == 3


@pytest.mark.parametrize("phrase,kind", [
    ("reste en \u00e9coute", "hold"), ("\u00c7a sera tout", "close"),
    ("Restes en \u00e9coute.", "hold"),
])
def test_hybrid_named_controls_do_not_retranscribe_or_queue_conversation(stt, mock_stt_model, phrase, kind):
    import speech_recognition as sr

    local = MagicMock()
    local.transcribe.return_value = f"Simone, {phrase}"
    mock_stt_model.transcribe.return_value = f"Simon, {phrase}"
    session = VoiceActivation("Simone", 10, mock_stt_model, local)
    stt.voice_activation = session
    audio = sr.AudioData(b'\xff\xff' * 16000, 16000, 2)

    stt._process_audio(audio, 1, session.begin_audio())
    session.end_audio()

    result = stt.resultQueue.get_nowait()
    assert result.text == ""
    assert result.voice_input.kind == kind
    assert session.accepts(result.voice_input)
    assert stt.resultQueue.empty()
    local.transcribe.assert_called_once_with(audio)
    mock_stt_model.transcribe.assert_not_called()


def test_hybrid_transmits_only_the_addressed_audio(stt, mock_stt_model):
    import speech_recognition as sr

    local = MagicMock()
    local.transcribe.side_effect = ["ambient speech", "Simone, plot a route to Sol"]
    mock_stt_model.transcribe.return_value = "plot a route to Sol"
    stt.voice_activation = VoiceActivation("Simone", 10, mock_stt_model, local)
    ambient = sr.AudioData(b'\xff\xff' * 16000, 16000, 2)
    addressed = sr.AudioData(b'\xfe\xfe' * 16000, 16000, 2)
    stt._process_audio(ambient, 1, stt.voice_activation.begin_audio())
    stt.voice_activation.end_audio()
    mock_stt_model.transcribe.assert_not_called()
    stt._process_audio(addressed, 2, stt.voice_activation.begin_audio())
    assert stt.resultQueue.get_nowait().text == "plot a route to Sol"
    mock_stt_model.transcribe.assert_called_once_with(addressed)


def test_continuous_standby_does_not_report_user_speaking(stt, mock_audio_input, mock_stt_model):
    stt.voice_activation = VoiceActivation("Simone", 10, mock_stt_model, mock_stt_model)
    stream = mock_audio_input["stream"]
    stream.read.side_effect = [b'\xff\xff' * 512] * 32 + [b'\x00\x00' * 512] * 34 + [b""]

    def transcribe(audio):
        assert not stt.recording
        return "private ambient conversation"

    mock_stt_model.transcribe.side_effect = transcribe
    stt.listening = True
    stt._listen_continuous_loop()
    mock_stt_model.transcribe.assert_called_once()
    assert stt.resultQueue.empty()
    stream.close.assert_called_once()


def test_oversized_standby_phrase_is_discarded_not_transcribed(stt, mock_audio_input, mock_stt_model):
    stt.voice_activation = VoiceActivation("Simone", 10, mock_stt_model, mock_stt_model)
    mock_audio_input["stream"].read.side_effect = (
        [b'\xff\xff' * 512] * 1000 + [b'\x00\x00' * 512] * 34 + [b""]
    )
    stt.listening = True
    stt._listen_continuous_loop()
    mock_stt_model.transcribe.assert_not_called()
    assert stt.resultQueue.empty()


def test_failed_voice_recognition_closes_audio_and_session(stt, mock_audio_input, mock_stt_model):
    stt.voice_activation = VoiceActivation("Simone", 10, mock_stt_model, mock_stt_model)
    mock_stt_model.transcribe.side_effect = RuntimeError("STT unavailable")
    mock_audio_input["stream"].read.side_effect = [b'\xff\xff' * 512] * 32 + [b'\x00\x00' * 512] * 34
    stt.listening = True
    with pytest.raises(RuntimeError, match="STT unavailable"):
        stt._listen_continuous_loop()
    assert stt.voice_activation.state == "error"
    assert not stt.recording
    mock_audio_input["stream"].close.assert_called_once()
    
