"""Local speech to text.

The decoding half is exercised with genuinely encoded audio -- WebM/Opus is
what a browser actually sends -- so the path from a recording to samples is
tested for real. Whisper itself is behind an interface so the suite never
downloads a model; the one test that would use a real model skips unless one
is already cached.
"""

from __future__ import annotations

import pytest

from billie.voice.transcribe import (
    SAMPLE_RATE,
    FasterWhisperTranscriber,
    TranscriptionError,
    decode_audio_bytes,
)
from tests.audio_fixtures import encode, tone, wav, webm_opus


# --- decoding what a browser records --------------------------------------


def test_a_wav_recording_decodes_to_samples():
    samples, duration = decode_audio_bytes(wav(1.0))
    assert len(samples) == SAMPLE_RATE
    assert duration == pytest.approx(1.0, abs=0.05)


def test_webm_opus_decodes_too():
    """Chrome and Firefox record WebM/Opus, not WAV."""
    samples, duration = decode_audio_bytes(webm_opus(1.5))
    assert duration == pytest.approx(1.5, abs=0.1)
    assert samples.max() > 0.0


def test_ogg_opus_decodes_too():
    data = encode(tone(0.5), container_format="ogg", codec="libopus")
    _, duration = decode_audio_bytes(data)
    assert duration == pytest.approx(0.5, abs=0.1)


def test_an_empty_recording_says_so():
    with pytest.raises(TranscriptionError) as excinfo:
        decode_audio_bytes(b"")
    assert "empty" in str(excinfo.value)


def test_something_that_is_not_audio_is_rejected_clearly():
    with pytest.raises(TranscriptionError) as excinfo:
        decode_audio_bytes(b"this is not audio at all, it is just text")
    assert "could not be decoded" in str(excinfo.value)


def test_a_recording_over_the_limit_is_refused():
    with pytest.raises(TranscriptionError) as excinfo:
        decode_audio_bytes(wav(3.0), max_seconds=2.0)
    assert "over the" in str(excinfo.value)


# --- the transcriber ------------------------------------------------------


def test_the_transcriber_describes_itself_as_on_device():
    described = FasterWhisperTranscriber("base").describe()
    assert described["on_device"] is True
    assert described["model"] == "base"
    assert described["loaded"] is False


def test_a_model_that_cannot_load_gives_an_actionable_error(tmp_path):
    transcriber = FasterWhisperTranscriber(
        "not-a-real-model-size", download_root=str(tmp_path)
    )
    if not transcriber.installed():  # pragma: no cover - depends on the environment
        pytest.skip("faster-whisper is not installed")

    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(wav(0.5))
    message = str(excinfo.value)
    assert "Whisper" in message and "download" in message
    # The failure is remembered, so the UI can show why it is unavailable.
    assert transcriber.available() is False
    assert transcriber.describe()["error"]


@pytest.mark.skipif(
    not FasterWhisperTranscriber.installed(), reason="faster-whisper not installed"
)
def test_real_whisper_transcribes_when_a_model_is_available(tmp_path):
    """Runs only where a Whisper model is already cached.

    The first load downloads from Hugging Face, which a sandboxed CI box
    usually cannot reach -- so this skips there and runs on a real machine.
    """
    transcriber = FasterWhisperTranscriber("tiny", compute_type="int8")
    try:
        transcriber._ensure_model()  # noqa: SLF001 - probing availability
    except TranscriptionError as exc:
        pytest.skip(f"no Whisper model available here: {exc}")

    result = transcriber.transcribe(wav(2.0))
    # A pure tone has no words in it; what matters is that the pipeline runs
    # end to end and reports the audio it was given.
    assert result.model == "whisper-tiny"
    assert result.duration_seconds == pytest.approx(2.0, abs=0.1)
    assert isinstance(result.text, str)
