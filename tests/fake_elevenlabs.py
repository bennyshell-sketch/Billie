"""An in-memory stand-in for the ElevenLabs REST API."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00fake-mp3-audio"


class FakeElevenLabs:
    def __init__(self) -> None:
        self.voices: list[dict[str, Any]] = [
            {
                "voice_id": "21m00Tcm4TlvDq8ikWAM",
                "name": "Rachel",
                "category": "premade",
                "preview_url": "https://example.com/rachel.mp3",
            },
            {
                "voice_id": "pNInz6obpgDQGcFmaJgB",
                "name": "Adam",
                "category": "premade",
                "preview_url": "https://example.com/adam.mp3",
            },
        ]
        self.synthesised: list[dict[str, Any]] = []
        self.cloned: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.api_keys_seen: list[str] = []
        self.fail_next_with: tuple[int, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.api_keys_seen.append(request.headers.get("xi-api-key", ""))
        path = request.url.path

        if self.fail_next_with is not None:
            status, detail = self.fail_next_with
            self.fail_next_with = None
            return httpx.Response(status, json={"detail": detail})

        if path == "/v1/voices" and request.method == "GET":
            return httpx.Response(200, json={"voices": self.voices})

        if path.startswith("/v1/text-to-speech/") and request.method == "POST":
            voice_id = path.rsplit("/", 1)[-1]
            import json as _json

            body = _json.loads(request.content)
            if not any(voice["voice_id"] == voice_id for voice in self.voices):
                return httpx.Response(404, json={"detail": {"message": "voice not found"}})
            self.synthesised.append({"voice_id": voice_id, **body})
            return httpx.Response(
                200, content=MP3_BYTES, headers={"content-type": "audio/mpeg"}
            )

        if path == "/v1/voices/add" and request.method == "POST":
            body = request.content.decode("latin-1")
            name = ""
            for part in body.split("\r\n"):
                if part and 'name="name"' not in part and "Content-" not in part:
                    pass
            # The multipart body carries the name field and one part per file.
            if 'name="name"' in body:
                after = body.split('name="name"', 1)[1]
                name = after.split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]
            file_count = body.count('name="files"')
            voice_id = f"cloned_{uuid.uuid4().hex[:8]}"
            record = {"voice_id": voice_id, "name": name, "files": file_count}
            self.cloned.append(record)
            self.voices.append(
                {"voice_id": voice_id, "name": name, "category": "cloned", "preview_url": ""}
            )
            return httpx.Response(200, json={"voice_id": voice_id})

        if path.startswith("/v1/voices/") and request.method == "DELETE":
            voice_id = path.rsplit("/", 1)[-1]
            self.deleted.append(voice_id)
            self.voices = [v for v in self.voices if v["voice_id"] != voice_id]
            return httpx.Response(200, json={"status": "ok"})

        return httpx.Response(404, json={"detail": f"no route for {path}"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
