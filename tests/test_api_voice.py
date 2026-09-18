"""The voice HTTP surface."""

from __future__ import annotations

from fastapi.testclient import TestClient

from billie.permissions import VOICE_CLONE, VOICE_SPEAK, VOICE_TRANSCRIBE
from tests.audio_fixtures import wav, webm_opus
from tests.fake_elevenlabs import MP3_BYTES

RACHEL = "21m00Tcm4TlvDq8ikWAM"


def grant(client: TestClient, tool: str) -> None:
    assert client.post(f"/api/permissions/{tool}", json={"granted": True}).status_code == 200


# --- transcription --------------------------------------------------------


def test_a_recording_is_transcribed(client: TestClient, transcriber):
    response = client.post(
        "/api/voice/transcribe",
        files={"audio": ("clip.webm", webm_opus(1.0), "audio/webm")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["text"] == transcriber.text
    assert payload["model"] == "fake"


def test_transcription_can_be_revoked(client: TestClient):
    client.post(f"/api/permissions/{VOICE_TRANSCRIBE}", json={"granted": False})
    response = client.post(
        "/api/voice/transcribe", files={"audio": ("clip.wav", wav(0.5), "audio/wav")}
    )
    assert response.status_code == 403
    assert response.json()["permission"] == VOICE_TRANSCRIBE


def test_an_oversized_recording_is_refused(client: TestClient, assistant):
    assistant.voice.max_audio_bytes = 128
    response = client.post(
        "/api/voice/transcribe", files={"audio": ("clip.wav", b"x" * 500, "audio/wav")}
    )
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]


# --- speaking -------------------------------------------------------------


def test_voices_are_listed(client: TestClient):
    voices = client.get("/api/voice/voices").json()["voices"]
    assert {voice["name"] for voice in voices} == {"Rachel", "Adam"}
    assert all(voice["cloned"] is False for voice in voices)


def test_speaking_is_refused_until_granted(client: TestClient, elevenlabs_api):
    response = client.post("/api/voice/speak", json={"text": "Hello", "voice_id": RACHEL})
    assert response.status_code == 403
    assert response.json()["permission"] == VOICE_SPEAK
    assert elevenlabs_api.synthesised == []


def test_speaking_returns_audio_once_granted(client: TestClient):
    grant(client, VOICE_SPEAK)
    response = client.post("/api/voice/speak", json={"text": "Hello", "voice_id": RACHEL})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == MP3_BYTES
    assert response.headers["X-Billie-Voice-Id"] == RACHEL
    assert response.headers["X-Billie-Characters"] == "5"
    # Audio of your assistant talking should not sit in a browser cache.
    assert response.headers["Cache-Control"] == "no-store"


def test_empty_text_is_rejected(client: TestClient):
    grant(client, VOICE_SPEAK)
    assert client.post("/api/voice/speak", json={"text": ""}).status_code == 422


# --- cloning --------------------------------------------------------------


def test_samples_are_listed_from_the_folder(client: TestClient, assistant):
    (assistant.voice.samples_dir / "me.mp3").write_bytes(b"audio")
    payload = client.get("/api/voice/samples").json()
    assert [entry["name"] for entry in payload["samples"]] == ["me.mp3"]


def test_cloning_is_refused_without_the_permission(client: TestClient, assistant):
    (assistant.voice.samples_dir / "me.mp3").write_bytes(b"audio")
    response = client.post(
        "/api/voice/clone/propose",
        json={"name": "Ben", "samples": ["me.mp3"], "i_am_the_speaker": True},
    )
    assert response.status_code == 403
    assert response.json()["permission"] == VOICE_CLONE


def test_cloning_without_the_attestation_is_refused(client: TestClient, assistant):
    grant(client, VOICE_CLONE)
    (assistant.voice.samples_dir / "me.mp3").write_bytes(b"audio")
    response = client.post(
        "/api/voice/clone/propose",
        json={"name": "Ben", "samples": ["me.mp3"], "i_am_the_speaker": False},
    )
    assert response.status_code == 400
    assert "your own voice only" in response.json()["detail"]


def test_the_full_clone_cycle(client: TestClient, assistant, elevenlabs_api):
    grant(client, VOICE_CLONE)
    (assistant.voice.samples_dir / "me.mp3").write_bytes(b"audio")

    action = client.post(
        "/api/voice/clone/propose",
        json={"name": "Ben", "samples": ["me.mp3"], "i_am_the_speaker": True},
    ).json()["action"]
    assert elevenlabs_api.cloned == [], "proposing uploads nothing"
    assert [entry["id"] for entry in client.get("/api/voice/actions").json()["actions"]] == [
        action["id"]
    ]

    result = client.post(f"/api/voice/actions/{action['id']}/approve").json()
    assert result["applied"] is True
    assert elevenlabs_api.cloned[-1]["name"] == "Ben"


def test_rejecting_a_clone_uploads_nothing(client: TestClient, assistant, elevenlabs_api):
    grant(client, VOICE_CLONE)
    (assistant.voice.samples_dir / "me.mp3").write_bytes(b"audio")
    action = client.post(
        "/api/voice/clone/propose",
        json={"name": "Ben", "samples": ["me.mp3"], "i_am_the_speaker": True},
    ).json()["action"]

    client.post(f"/api/voice/actions/{action['id']}/reject", json={"reason": "wrong take"})
    assert elevenlabs_api.cloned == []


def test_a_cloned_voice_can_be_deleted(client: TestClient, elevenlabs_api):
    grant(client, VOICE_CLONE)
    assert client.delete(f"/api/voice/voices/{RACHEL}").status_code == 200
    assert elevenlabs_api.deleted == [RACHEL]


# --- status ---------------------------------------------------------------


def test_voice_status_says_what_runs_where(client: TestClient):
    status = client.get("/api/voice/status").json()
    assert status["speech_to_text"]["on_device"] is True
    assert status["text_to_speech"]["on_device"] is False


def test_the_main_status_includes_voice(client: TestClient):
    status = client.get("/api/status").json()
    assert status["voice"]["speech_to_text"]["granted"] is True
    assert status["voice"]["text_to_speech"]["granted"] is False
