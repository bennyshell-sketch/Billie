"""Speech to text, on this machine.

Whisper runs locally through faster-whisper. Audio you record never leaves the
device to be turned into text -- that is the whole point of doing it here
rather than posting it to a transcription API.

faster-whisper is an optional dependency. Without it Billie still runs; the
voice button reports what to install instead of failing obscurely.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("billie.voice")

SAMPLE_RATE = 16_000
#: Whisper's own sizes, smallest first. `base` is a reasonable laptop default.
MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3", "distil-large-v3")


class TranscriptionError(RuntimeError):
    """User-facing: the message says what to do about it."""


@dataclass
class Transcription:
    text: str
    language: str = ""
    language_probability: float = 0.0
    duration_seconds: float = 0.0
    model: str = ""
    latency_ms: int = 0
    segments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "language_probability": round(self.language_probability, 3),
            "duration_seconds": round(self.duration_seconds, 2),
            "model": self.model,
            "latency_ms": self.latency_ms,
            "segments": self.segments,
        }


def decode_audio_bytes(data: bytes, *, max_seconds: float = 300.0):
    """Decode any browser-recorded audio to mono 16 kHz float32 samples.

    The browser's MediaRecorder gives WebM/Opus on Chrome and Firefox and MP4
    on Safari; PyAV (which faster-whisper already depends on) handles all of
    them, so there is no ffmpeg binary to install separately.
    """
    if not data:
        raise TranscriptionError("The recording was empty.")
    try:
        from faster_whisper.audio import decode_audio
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise TranscriptionError(
            "Speech to text needs `pip install faster-whisper`."
        ) from exc

    try:
        samples = decode_audio(io.BytesIO(data), sampling_rate=SAMPLE_RATE)
    except Exception as exc:  # noqa: BLE001 - PyAV raises a wide range
        raise TranscriptionError(
            f"That audio could not be decoded ({type(exc).__name__}). "
            "Billie accepts what a browser records: WebM/Opus, MP4, WAV, MP3."
        ) from exc

    duration = len(samples) / SAMPLE_RATE
    if duration > max_seconds:
        raise TranscriptionError(
            f"That recording is {duration:.0f}s, over the {max_seconds:.0f}s limit."
        )
    return samples, duration


class Transcriber(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool:
        """Can this actually transcribe right now?"""

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        """What the UI shows about the transcriber."""

    @abstractmethod
    def transcribe(self, data: bytes, *, language: str | None = None) -> Transcription:
        """Turn audio bytes into text. Raises `TranscriptionError`."""


class FasterWhisperTranscriber(Transcriber):
    """Whisper via faster-whisper, loaded once and kept in memory."""

    def __init__(
        self,
        model_size: str = "base",
        *,
        device: str = "auto",
        compute_type: str = "int8",
        max_seconds: float = 300.0,
        download_root: str | None = None,
    ) -> None:
        self.name = "faster-whisper"
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.max_seconds = max_seconds
        self.download_root = download_root
        self._model = None
        self._load_error: str | None = None
        # One model, one lock: faster-whisper models are not thread-safe, and
        # two concurrent requests would otherwise interleave inside it.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @staticmethod
    def installed() -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def available(self) -> bool:
        if not self.installed():
            self._load_error = "faster-whisper is not installed"
            return False
        return self._load_error is None

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model_size,
            "device": self.device,
            "compute_type": self.compute_type,
            "installed": self.installed(),
            "loaded": self._model is not None,
            "error": self._load_error,
            "on_device": True,
        }

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        if not self.installed():
            raise TranscriptionError(
                "Speech to text needs `pip install faster-whisper`."
            )
        from faster_whisper import WhisperModel

        try:
            logger.info("Loading Whisper %s (%s)", self.model_size, self.compute_type)
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                download_root=self.download_root,
            )
        except Exception as exc:  # noqa: BLE001 - download and load failures vary
            self._load_error = str(exc)[:200]
            raise TranscriptionError(
                f"Could not load the Whisper '{self.model_size}' model: {exc}. "
                "The first run downloads it, which needs internet access once; "
                "after that it works offline."
            ) from exc
        self._load_error = None
        return self._model

    def transcribe(self, data: bytes, *, language: str | None = None) -> Transcription:
        started = time.monotonic()
        samples, duration = decode_audio_bytes(data, max_seconds=self.max_seconds)
        with self._lock:
            model = self._ensure_model()
            segments, info = model.transcribe(
                samples,
                language=language,
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            collected = [
                {
                    "start": round(segment.start, 2),
                    "end": round(segment.end, 2),
                    "text": segment.text.strip(),
                }
                for segment in segments
            ]

        text = " ".join(segment["text"] for segment in collected).strip()
        return Transcription(
            text=text,
            language=getattr(info, "language", "") or "",
            language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
            duration_seconds=duration,
            model=f"whisper-{self.model_size}",
            latency_ms=int((time.monotonic() - started) * 1000),
            segments=collected,
        )
