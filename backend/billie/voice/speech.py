"""Text to speech through ElevenLabs.

This is the one part of voice that leaves your machine: the text of Billie's
reply is sent to ElevenLabs under your own API key, and audio comes back. The
audio is streamed to the browser and not kept.

Two notes on scope, both deliberate:

* The brief named `@elevenlabs/elevenlabs-js` and `textToSpeech.convert`. That
  is the JavaScript SDK; Billie's backend is Python, so this calls the same
  REST endpoints that SDK calls, with httpx -- consistent with the Google
  integration, and testable offline.
* Voice cloning is gated on your explicit attestation that the recordings are
  of your own voice, and on samples living in your voice-samples folder.
  Software cannot verify whose voice is in a file; what it can do is refuse to
  take audio from anywhere else, make the step deliberate, and log it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("billie.speech")

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = "eleven_multilingual_v2"
#: ElevenLabs bills per character, so a runaway reply is a runaway bill.
MAX_CHARACTERS = 5000


class SpeechError(RuntimeError):
    """User-facing message about a text-to-speech operation."""


class SpeechNotConfigured(SpeechError):
    """No ElevenLabs API key in .env."""


@dataclass
class Voice:
    id: str
    name: str
    category: str = ""
    description: str = ""
    preview_url: str = ""

    @property
    def cloned(self) -> bool:
        return self.category in {"cloned", "professional", "generated"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "preview_url": self.preview_url,
            "cloned": self.cloned,
        }


@dataclass
class Speech:
    audio: bytes
    media_type: str
    voice_id: str
    model_id: str
    characters: int


class ElevenLabs:
    def __init__(
        self,
        api_key: str | None,
        *,
        model_id: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
        audit=None,
    ) -> None:
        self.api_key = api_key
        self.model_id = model_id
        self._client = client
        self.audit = audit

    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.api_key) or self._client is not None

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "elevenlabs",
            "configured": self.configured,
            "model_id": self.model_id,
            "on_device": False,
        }

    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=120.0)

    def _headers(self) -> dict[str, str]:
        if not self.api_key and self._client is None:
            raise SpeechNotConfigured(
                "ELEVENLABS_API_KEY is not set in .env, so Billie cannot speak."
            )
        return {"xi-api-key": self.api_key or "test-key"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        files: list | None = None,
        data: dict | None = None,
        expect_audio: bool = False,
    ) -> httpx.Response:
        headers = self._headers()
        if expect_audio:
            headers["Accept"] = "audio/mpeg"
        client = self._http()
        try:
            response = await client.request(
                method, f"{API_BASE}{path}", headers=headers, json=json, files=files, data=data
            )
        except httpx.HTTPError as exc:
            raise SpeechError(f"Could not reach ElevenLabs ({exc}).") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if self.audit:
            self.audit.record(
                "elevenlabs.api_call", method=method, path=path, status=response.status_code
            )

        if response.status_code >= 400:
            raise SpeechError(_explain(response))
        return response

    # ------------------------------------------------------------------
    # Voices
    # ------------------------------------------------------------------
    async def list_voices(self) -> list[Voice]:
        response = await self._request("GET", "/voices")
        return [
            Voice(
                id=item.get("voice_id", ""),
                name=item.get("name", ""),
                category=item.get("category", ""),
                description=item.get("description") or "",
                preview_url=item.get("preview_url") or "",
            )
            for item in response.json().get("voices", [])
        ]

    async def synthesise(
        self, text: str, voice_id: str, *, model_id: str | None = None
    ) -> Speech:
        text = text.strip()
        if not text:
            raise SpeechError("There is nothing to say.")
        if len(text) > MAX_CHARACTERS:
            raise SpeechError(
                f"That reply is {len(text)} characters, over the {MAX_CHARACTERS} "
                "limit for one request. ElevenLabs bills per character."
            )
        if not voice_id:
            raise SpeechError(
                "No voice is selected. Pick one under Voice in the UI, or set "
                "BILLIE_TTS_VOICE_ID in .env."
            )

        response = await self._request(
            "POST",
            f"/text-to-speech/{voice_id}",
            json={"text": text, "model_id": model_id or self.model_id},
            expect_audio=True,
        )
        return Speech(
            audio=response.content,
            media_type=response.headers.get("content-type", "audio/mpeg"),
            voice_id=voice_id,
            model_id=model_id or self.model_id,
            characters=len(text),
        )

    # ------------------------------------------------------------------
    # Instant Voice Cloning
    # ------------------------------------------------------------------
    async def clone_voice(
        self, name: str, sample_paths: list[Path], *, description: str = ""
    ) -> Voice:
        """Create a voice from your own recordings. Reached only via approval."""
        if not sample_paths:
            raise SpeechError("Cloning needs at least one recording.")

        handles = []
        files = []
        try:
            for path in sample_paths:
                handle = path.open("rb")
                handles.append(handle)
                files.append(("files", (path.name, handle, "audio/mpeg")))
            payload = {"name": name}
            if description:
                payload["description"] = description
            response = await self._request("POST", "/voices/add", files=files, data=payload)
        finally:
            for handle in handles:
                handle.close()

        body = response.json()
        voice_id = body.get("voice_id", "")
        if self.audit:
            self.audit.record(
                "elevenlabs.voice_cloned",
                voice_id=voice_id,
                name=name,
                samples=[path.name for path in sample_paths],
            )
        return Voice(id=voice_id, name=name, category="cloned", description=description)

    async def delete_voice(self, voice_id: str) -> dict[str, Any]:
        await self._request("DELETE", f"/voices/{voice_id}")
        if self.audit:
            self.audit.record("elevenlabs.voice_deleted", voice_id=voice_id)
        return {"voice_id": voice_id, "deleted": True}


def _explain(response: httpx.Response) -> str:
    """Turn an ElevenLabs error into something worth reading."""
    detail = ""
    try:
        body = response.json().get("detail")
        if isinstance(body, dict):
            detail = body.get("message") or body.get("status") or ""
        elif isinstance(body, str):
            detail = body
    except ValueError:
        detail = response.text[:200]

    if response.status_code == 401:
        return f"ElevenLabs rejected the API key in .env. {detail}".strip()
    if response.status_code == 422:
        return f"ElevenLabs could not use that request: {detail}"
    if response.status_code == 429:
        return "ElevenLabs rate limit or character quota reached."
    return f"ElevenLabs error ({response.status_code}): {detail}"
