"""The voice service: permissions, sample confinement, and the clone gate."""

from __future__ import annotations

import pytest

from billie.permissions import VOICE_CLONE, VOICE_SPEAK, VOICE_TRANSCRIBE, PermissionDenied
from billie.voice.service import VoiceService
from billie.voice.speech import SpeechError
from billie.voice.transcribe import TranscriptionError
from tests.audio_fixtures import wav

RACHEL = "21m00Tcm4TlvDq8ikWAM"


@pytest.fixture
def samples(voice: VoiceService):
    """Two recordings sitting in the voice-samples folder."""
    (voice.samples_dir / "one.mp3").write_bytes(b"audio-one")
    (voice.samples_dir / "two.wav").write_bytes(b"audio-two")
    return voice


# --- transcription --------------------------------------------------------


def test_transcribing_returns_text_and_stays_on_device(voice: VoiceService, transcriber):
    result = voice.transcribe(wav(1.0))
    assert result.text == transcriber.text
    assert transcriber.calls, "the transcriber was actually used"


def test_transcribing_needs_its_permission(voice: VoiceService):
    voice.permissions.set(VOICE_TRANSCRIBE, False)
    with pytest.raises(PermissionDenied) as excinfo:
        voice.transcribe(wav(0.5))
    assert excinfo.value.tool == VOICE_TRANSCRIBE


def test_an_oversized_recording_is_refused_before_transcribing(
    voice: VoiceService, transcriber
):
    voice.max_audio_bytes = 100
    with pytest.raises(TranscriptionError) as excinfo:
        voice.transcribe(b"x" * 200)
    assert "over the" in str(excinfo.value)
    assert transcriber.calls == []


def test_the_audit_log_records_the_fact_not_the_words(voice: VoiceService, audit):
    voice.transcribe(wav(1.0))
    entry = [e for e in audit.tail() if e["event"] == "voice.transcribed"][-1]
    assert entry["detail"]["on_device"] is True
    assert entry["detail"]["characters"] > 0
    # What was said belongs in the conversation, not in the audit trail.
    assert "what is on my calendar" not in str(entry)


# --- speaking -------------------------------------------------------------


async def test_speaking_needs_its_permission(voice: VoiceService, elevenlabs_api):
    with pytest.raises(PermissionDenied) as excinfo:
        await voice.speak("Hello", voice_id=RACHEL)
    assert excinfo.value.tool == VOICE_SPEAK
    assert elevenlabs_api.synthesised == [], "nothing was sent to ElevenLabs"


async def test_speaking_once_granted_returns_audio(voice: VoiceService, elevenlabs_api):
    voice.permissions.set(VOICE_SPEAK, True)
    result = await voice.speak("Good morning.", voice_id=RACHEL)
    assert result.audio
    assert elevenlabs_api.synthesised[-1]["text"] == "Good morning."


async def test_with_no_voice_chosen_the_first_available_is_used(voice: VoiceService):
    voice.permissions.set(VOICE_SPEAK, True)
    result = await voice.speak("Hello")
    assert result.voice_id == RACHEL


async def test_a_configured_default_voice_is_used(voice: VoiceService):
    voice.permissions.set(VOICE_SPEAK, True)
    voice.default_voice_id = "pNInz6obpgDQGcFmaJgB"
    result = await voice.speak("Hello")
    assert result.voice_id == "pNInz6obpgDQGcFmaJgB"


async def test_speaking_is_audited_with_the_character_count(voice: VoiceService, audit):
    voice.permissions.set(VOICE_SPEAK, True)
    await voice.speak("Good morning.", voice_id=RACHEL)
    entry = [e for e in audit.tail() if e["event"] == "voice.spoken"][-1]
    assert entry["detail"]["characters"] == len("Good morning.")
    assert entry["detail"]["on_device"] is False


# --- sample folder confinement -------------------------------------------


def test_samples_are_listed_from_the_folder(samples: VoiceService):
    listed = {entry["name"] for entry in samples.list_samples()}
    assert listed == {"one.mp3", "two.wav"}


def test_a_non_audio_file_in_the_folder_is_ignored(samples: VoiceService):
    (samples.samples_dir / "notes.txt").write_text("not audio", encoding="utf-8")
    assert "notes.txt" not in {entry["name"] for entry in samples.list_samples()}


@pytest.mark.parametrize(
    "name", ["../secret.mp3", "../../etc/passwd", "/etc/passwd", "~/private.mp3"]
)
def test_recordings_outside_the_folder_are_refused(samples: VoiceService, name):
    with pytest.raises(SpeechError):
        samples.resolve_sample(name)


def test_a_sample_that_does_not_exist_is_refused(samples: VoiceService):
    with pytest.raises(SpeechError) as excinfo:
        samples.resolve_sample("missing.mp3")
    assert "not in your voice-samples folder" in str(excinfo.value)


# --- cloning --------------------------------------------------------------


def test_cloning_needs_its_permission(samples: VoiceService):
    with pytest.raises(PermissionDenied) as excinfo:
        samples.propose_clone("Ben", ["one.mp3"], i_am_the_speaker=True)
    assert excinfo.value.tool == VOICE_CLONE


def test_cloning_requires_the_attestation(samples: VoiceService):
    samples.permissions.set(VOICE_CLONE, True)
    with pytest.raises(SpeechError) as excinfo:
        samples.propose_clone("Ben", ["one.mp3"], i_am_the_speaker=False)
    assert "your own voice only" in str(excinfo.value)


def test_proposing_a_clone_uploads_nothing(samples: VoiceService, elevenlabs_api):
    samples.permissions.set(VOICE_CLONE, True)
    action = samples.propose_clone("Ben", ["one.mp3", "two.wav"], i_am_the_speaker=True)

    assert action.status == "pending"
    assert elevenlabs_api.cloned == [], "proposing must not upload"
    assert "one.mp3" in action.preview and "two.wav" in action.preview
    assert "your own voice" in action.preview


def test_a_clone_proposal_naming_an_outside_file_is_refused(
    samples: VoiceService, elevenlabs_api
):
    samples.permissions.set(VOICE_CLONE, True)
    with pytest.raises(SpeechError):
        samples.propose_clone("Ben", ["../../etc/passwd"], i_am_the_speaker=True)
    assert elevenlabs_api.cloned == []


async def test_approving_a_clone_uploads_the_recordings(
    samples: VoiceService, confirmations, elevenlabs_api
):
    samples.permissions.set(VOICE_CLONE, True)
    action = samples.propose_clone("Ben", ["one.mp3"], i_am_the_speaker=True)

    result = await confirmations.approve(action.id)
    assert result["result"]["voice_id"].startswith("cloned_")
    assert elevenlabs_api.cloned[-1]["name"] == "Ben"


async def test_rejecting_a_clone_uploads_nothing(
    samples: VoiceService, confirmations, elevenlabs_api
):
    samples.permissions.set(VOICE_CLONE, True)
    action = samples.propose_clone("Ben", ["one.mp3"], i_am_the_speaker=True)
    confirmations.reject(action.id, reason="wrong recording")
    assert elevenlabs_api.cloned == []


# --- status ---------------------------------------------------------------


def test_status_reports_each_part_and_where_it_runs(samples: VoiceService):
    status = samples.status()
    assert status["speech_to_text"]["on_device"] is True
    assert status["speech_to_text"]["granted"] is True
    assert status["text_to_speech"]["on_device"] is False
    assert status["text_to_speech"]["granted"] is False
    assert status["cloning"]["samples"] == 2
