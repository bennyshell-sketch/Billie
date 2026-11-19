"""The voice service: transcribe locally, speak through ElevenLabs, clone once.

Holds the three voice capabilities together and applies the rules around them:
permissions, size limits, where sample recordings may come from, and the
confirmation step before a voice is cloned.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from billie.audit import AuditLog
from billie.confirmations import ConfirmationQueue
from billie.permissions import (
    VOICE_CLONE,
    VOICE_SPEAK,
    VOICE_TRANSCRIBE,
    Permissions,
)
from billie.voice.speech import ElevenLabs, Speech, SpeechError, Voice
from billie.voice.transcribe import Transcriber, Transcription, TranscriptionError

logger = logging.getLogger("billie.voice.service")

KIND_CLONE = "voice.clone"
SAMPLE_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".webm", ".mp4"}
#: ElevenLabs asks for a few minutes of clean audio for a usable clone.
RECOMMENDED_SAMPLE_SECONDS = 300


class VoiceService:
    def __init__(
        self,
        *,
        transcriber: Transcriber,
        speech: ElevenLabs,
        permissions: Permissions,
        confirmations: ConfirmationQueue,
        audit: AuditLog,
        samples_dir: Path,
        max_audio_bytes: int = 25 * 1024 * 1024,
        default_voice_id: str | None = None,
    ) -> None:
        self.transcriber = transcriber
        self.speech = speech
        self.permissions = permissions
        self.confirmations = confirmations
        self.audit = audit
        self.samples_dir = Path(samples_dir)
        self.max_audio_bytes = max_audio_bytes
        self.default_voice_id = default_voice_id
        confirmations.register(KIND_CLONE, self._execute_clone)

    # ------------------------------------------------------------------
    # Speech in
    # ------------------------------------------------------------------
    def transcribe(self, data: bytes, *, language: str | None = None) -> Transcription:
        self.permissions.require(VOICE_TRANSCRIBE)
        if len(data) > self.max_audio_bytes:
            raise TranscriptionError(
                f"That recording is {len(data) // (1024 * 1024)}MB, over the "
                f"{self.max_audio_bytes // (1024 * 1024)}MB limit."
            )
        result = self.transcriber.transcribe(data, language=language)
        # The words themselves are about to become a chat message, which is
        # recorded there. The audit entry keeps the fact, not the content.
        self.audit.record(
            "voice.transcribed",
            model=result.model,
            audio_seconds=round(result.duration_seconds, 2),
            characters=len(result.text),
            latency_ms=result.latency_ms,
            on_device=True,
        )
        return result

    # ------------------------------------------------------------------
    # Speech out
    # ------------------------------------------------------------------
    async def speak(self, text: str, *, voice_id: str | None = None) -> Speech:
        self.permissions.require(VOICE_SPEAK)
        chosen = voice_id or self.default_voice_id
        if not chosen:
            voices = await self.speech.list_voices()
            if not voices:
                raise SpeechError("Your ElevenLabs account has no voices available.")
            chosen = voices[0].id
        result = await self.speech.synthesise(text, chosen)
        self.audit.record(
            "voice.spoken",
            voice_id=result.voice_id,
            model_id=result.model_id,
            characters=result.characters,
            on_device=False,
        )
        return result

    async def voices(self) -> list[Voice]:
        return await self.speech.list_voices()

    # ------------------------------------------------------------------
    # Cloning
    # ------------------------------------------------------------------
    def list_samples(self) -> list[dict[str, Any]]:
        """Recordings sitting in the voice-samples folder."""
        if not self.samples_dir.exists():
            return []
        samples = []
        for path in sorted(self.samples_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() not in SAMPLE_SUFFIXES:
                continue
            samples.append(
                {
                    "name": path.relative_to(self.samples_dir).as_posix(),
                    "bytes": path.stat().st_size,
                }
            )
        return samples

    def resolve_sample(self, name: str) -> Path:
        """Resolve a sample name, refusing anything outside the folder."""
        candidate = str(name).strip().replace("\\", "/")
        if not candidate:
            raise SpeechError("A sample filename is required.")
        if Path(candidate).is_absolute() or candidate.startswith("~"):
            raise SpeechError(
                "Give a filename inside your voice-samples folder, not a full path."
            )
        root = self.samples_dir.resolve()
        target = (root / candidate).resolve()
        if target == root or root not in target.parents:
            raise SpeechError(
                f"{name!r} is outside your voice-samples folder. Billie only "
                "uploads recordings you have put there."
            )
        if not target.is_file():
            raise SpeechError(f"{name!r} is not in your voice-samples folder.")
        if target.suffix.lower() not in SAMPLE_SUFFIXES:
            raise SpeechError(f"{name!r} is not an audio file Billie recognises.")
        return target

    def propose_clone(
        self,
        name: str,
        sample_names: list[str],
        *,
        i_am_the_speaker: bool,
        description: str = "",
    ):
        """Prepare a voice clone. Nothing is uploaded until this is approved."""
        self.permissions.require(VOICE_CLONE)
        if not name.strip():
            raise SpeechError("The voice needs a name.")
        if not i_am_the_speaker:
            raise SpeechError(
                "Billie clones your own voice only. Confirm that these recordings "
                "are of you before going further."
            )
        if not sample_names:
            raise SpeechError("Choose at least one recording from your samples folder.")

        paths = [self.resolve_sample(sample) for sample in sample_names]
        total_bytes = sum(path.stat().st_size for path in paths)
        lines = [
            "Billie will UPLOAD these recordings to ElevenLabs and create a voice:",
            "",
            f"  voice name: {name}",
            f"  account: your own ElevenLabs account",
            "",
            "  files:",
        ]
        lines += [
            f"    {path.relative_to(self.samples_dir).as_posix()} "
            f"({path.stat().st_size // 1024} KB)"
            for path in paths
        ]
        lines += [
            "",
            f"  total: {len(paths)} file(s), {total_bytes // 1024} KB",
            "",
            "You have confirmed these are recordings of your own voice. Billie",
            "cannot verify that, and will not upload audio from anywhere except",
            "your voice-samples folder.",
        ]
        return self.confirmations.propose(
            KIND_CLONE,
            summary=f"Clone your voice as “{name}” from {len(paths)} recording(s)",
            preview="\n".join(lines),
            payload={
                "name": name,
                "samples": [path.relative_to(self.samples_dir).as_posix() for path in paths],
                "description": description,
                "attested": True,
            },
        )

    async def _execute_clone(self, payload: dict[str, Any]) -> dict[str, Any]:
        paths = [self.resolve_sample(sample) for sample in payload["samples"]]
        voice = await self.speech.clone_voice(
            payload["name"], paths, description=payload.get("description", "")
        )
        return {"voice_id": voice.id, "name": voice.name, "samples": payload["samples"]}

    async def delete_voice(self, voice_id: str) -> dict[str, Any]:
        self.permissions.require(VOICE_CLONE)
        return await self.speech.delete_voice(voice_id)

    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        samples = self.list_samples()
        return {
            "speech_to_text": {
                **self.transcriber.describe(),
                "granted": self.permissions.is_granted(VOICE_TRANSCRIBE),
            },
            "text_to_speech": {
                **self.speech.describe(),
                "granted": self.permissions.is_granted(VOICE_SPEAK),
                "voice_id": self.default_voice_id,
            },
            "cloning": {
                "granted": self.permissions.is_granted(VOICE_CLONE),
                "samples_dir": str(self.samples_dir),
                "samples": len(samples),
                "recommended_seconds": RECOMMENDED_SAMPLE_SECONDS,
            },
            "pending_actions": len(self.confirmations.pending("voice.")),
        }
