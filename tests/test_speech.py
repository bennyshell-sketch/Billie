"""The ElevenLabs client."""

from __future__ import annotations

import json

import pytest

from billie.voice.speech import (
    MAX_CHARACTERS,
    ElevenLabs,
    SpeechError,
    SpeechNotConfigured,
)
from tests.fake_elevenlabs import MP3_BYTES, FakeElevenLabs

RACHEL = "21m00Tcm4TlvDq8ikWAM"


# --- configuration --------------------------------------------------------


async def test_without_an_api_key_it_says_so():
    speech = ElevenLabs(None)
    assert speech.configured is False
    with pytest.raises(SpeechNotConfigured) as excinfo:
        await speech.list_voices()
    assert "ELEVENLABS_API_KEY" in str(excinfo.value)


def test_it_reports_that_speech_leaves_the_machine(speech: ElevenLabs):
    assert speech.describe()["on_device"] is False


# --- voices ---------------------------------------------------------------


async def test_voices_are_listed_with_preset_and_cloned_marked(speech: ElevenLabs, elevenlabs_api):
    elevenlabs_api.voices.append(
        {"voice_id": "mine", "name": "Ben", "category": "cloned", "preview_url": ""}
    )
    voices = await speech.list_voices()

    by_name = {voice.name: voice for voice in voices}
    assert by_name["Rachel"].cloned is False
    assert by_name["Ben"].cloned is True


async def test_the_api_key_is_sent_as_a_header(speech: ElevenLabs, elevenlabs_api):
    await speech.list_voices()
    assert elevenlabs_api.api_keys_seen[-1] == "test-key"


# --- synthesis ------------------------------------------------------------


async def test_text_becomes_audio(speech: ElevenLabs, elevenlabs_api):
    result = await speech.synthesise("Good morning.", RACHEL)

    assert result.audio == MP3_BYTES
    assert result.media_type == "audio/mpeg"
    assert result.characters == len("Good morning.")
    sent = elevenlabs_api.synthesised[-1]
    assert sent["voice_id"] == RACHEL
    assert sent["text"] == "Good morning."
    assert sent["model_id"] == "eleven_multilingual_v2"


async def test_empty_text_is_refused_without_a_call(speech: ElevenLabs, elevenlabs_api):
    with pytest.raises(SpeechError):
        await speech.synthesise("   ", RACHEL)
    assert elevenlabs_api.synthesised == []


async def test_a_very_long_reply_is_refused_because_it_is_billed_per_character(
    speech: ElevenLabs, elevenlabs_api
):
    with pytest.raises(SpeechError) as excinfo:
        await speech.synthesise("x" * (MAX_CHARACTERS + 1), RACHEL)
    assert "bills per character" in str(excinfo.value)
    assert elevenlabs_api.synthesised == []


async def test_no_voice_selected_says_where_to_set_one(speech: ElevenLabs):
    with pytest.raises(SpeechError) as excinfo:
        await speech.synthesise("Hello", "")
    assert "BILLIE_TTS_VOICE_ID" in str(excinfo.value)


# --- errors ---------------------------------------------------------------


async def test_a_bad_key_is_reported_as_such(speech: ElevenLabs, elevenlabs_api):
    elevenlabs_api.fail_next_with = (401, {"message": "invalid api key"})
    with pytest.raises(SpeechError) as excinfo:
        await speech.list_voices()
    assert "rejected the API key" in str(excinfo.value)


async def test_a_quota_error_is_readable(speech: ElevenLabs, elevenlabs_api):
    elevenlabs_api.fail_next_with = (429, {"message": "quota"})
    with pytest.raises(SpeechError) as excinfo:
        await speech.synthesise("Hello", RACHEL)
    assert "quota" in str(excinfo.value).lower()


async def test_an_unknown_voice_is_an_error(speech: ElevenLabs):
    with pytest.raises(SpeechError):
        await speech.synthesise("Hello", "no-such-voice")


async def test_every_call_is_audited(speech: ElevenLabs, audit):
    await speech.list_voices()
    entry = [e for e in audit.tail() if e["event"] == "elevenlabs.api_call"][-1]
    assert entry["detail"]["path"] == "/voices"
    assert entry["detail"]["status"] == 200


# --- cloning --------------------------------------------------------------


async def test_cloning_uploads_the_files_and_returns_a_voice(
    speech: ElevenLabs, elevenlabs_api, tmp_path
):
    first = tmp_path / "sample-one.mp3"
    second = tmp_path / "sample-two.mp3"
    first.write_bytes(b"audio-one")
    second.write_bytes(b"audio-two")

    voice = await speech.clone_voice("Ben", [first, second], description="my own voice")

    assert voice.id.startswith("cloned_")
    assert voice.cloned is True
    uploaded = elevenlabs_api.cloned[-1]
    assert uploaded["name"] == "Ben"
    assert uploaded["files"] == 2


async def test_cloning_with_no_samples_is_refused(speech: ElevenLabs, elevenlabs_api):
    with pytest.raises(SpeechError):
        await speech.clone_voice("Ben", [])
    assert elevenlabs_api.cloned == []


async def test_a_clone_is_audited_with_the_filenames(speech: ElevenLabs, audit, tmp_path):
    sample = tmp_path / "sample.mp3"
    sample.write_bytes(b"audio")
    await speech.clone_voice("Ben", [sample])

    entry = [e for e in audit.tail() if e["event"] == "elevenlabs.voice_cloned"][-1]
    assert entry["detail"]["name"] == "Ben"
    assert entry["detail"]["samples"] == ["sample.mp3"]


async def test_a_voice_can_be_deleted(speech: ElevenLabs, elevenlabs_api):
    result = await speech.delete_voice(RACHEL)
    assert result == {"voice_id": RACHEL, "deleted": True}
    assert elevenlabs_api.deleted == [RACHEL]
