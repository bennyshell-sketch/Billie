"""Real audio for tests, generated with PyAV.

The browser's MediaRecorder produces WebM/Opus on Chrome and Firefox and MP4 on
Safari, so the decoding path is worth exercising with genuine encoded audio
rather than a stub.
"""

from __future__ import annotations

import io
import math

import av
import numpy as np


def tone(seconds: float = 1.0, frequency: float = 220.0, rate: int = 16_000) -> np.ndarray:
    count = int(rate * seconds)
    return (0.3 * np.sin(2 * math.pi * frequency * np.arange(count) / rate)).astype(np.float32)


def encode(
    samples: np.ndarray,
    *,
    container_format: str = "wav",
    codec: str = "pcm_s16le",
    rate: int = 16_000,
) -> bytes:
    """Encode float32 mono samples into a real audio container."""
    buffer = io.BytesIO()
    container = av.open(buffer, mode="w", format=container_format)
    stream = container.add_stream(codec, rate=48_000 if codec == "libopus" else rate)
    stream.layout = "mono"
    frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="flt", layout="mono")
    frame.sample_rate = rate
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return buffer.getvalue()


def wav(seconds: float = 1.0) -> bytes:
    return encode(tone(seconds))


def webm_opus(seconds: float = 1.0) -> bytes:
    """What Chrome and Firefox actually send."""
    return encode(tone(seconds), container_format="webm", codec="libopus")
