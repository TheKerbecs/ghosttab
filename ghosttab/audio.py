"""Microphone capture for dictation.

Records at the device's native rate and returns 16 kHz mono 16-bit WAV, the
format the model's audio encoder expects.
"""

from __future__ import annotations

import io
import threading
import time
import wave

import numpy as np

TARGET_RATE = 16000


def to_wav(samples: np.ndarray, rate: int) -> bytes:
    """float32 mono samples in [-1, 1] at `rate` -> 16 kHz 16-bit WAV bytes."""
    if rate != TARGET_RATE and len(samples):
        n = int(round(len(samples) * TARGET_RATE / rate))
        samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class Recorder:
    """Captures until stop() and tracks loudness for silence detection."""

    def __init__(self, speech_threshold: float = 0.02):
        self.speech_threshold = speech_threshold
        self._blocks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream = None
        self.rate = 0
        self.level = 0.0                  # RMS of the latest block, 0..1
        self.heard_speech = False
        self.last_speech_at = 0.0
        self.started_at = 0.0

    def start(self):
        import sounddevice as sd

        device = sd.query_devices(kind="input")
        self.rate = int(device["default_samplerate"])
        self._blocks = []
        self.heard_speech = False
        self.started_at = self.last_speech_at = time.monotonic()
        self._stream = sd.InputStream(samplerate=self.rate, channels=1, dtype="float32",
                                      blocksize=int(self.rate * 0.05), callback=self._callback)
        self._stream.start()

    def _callback(self, data, frames, time_info, status):
        block = data[:, 0].copy()
        rms = float(np.sqrt(np.mean(block * block))) if len(block) else 0.0
        self.level = rms
        if rms >= self.speech_threshold:
            self.heard_speech = True
            self.last_speech_at = time.monotonic()
        with self._lock:
            self._blocks.append(block)

    def silence_for(self) -> float:
        return time.monotonic() - self.last_speech_at

    def stop(self) -> bytes:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            samples = np.concatenate(self._blocks) if self._blocks else np.zeros(0, dtype="float32")
            self._blocks = []
        return to_wav(samples, self.rate or TARGET_RATE)

    def cancel(self):
        self.stop()


class FileRecorder:
    """Stands in for the microphone with a WAV file, for testing: behaves as
    if the file were spoken in real time, followed by silence."""

    def __init__(self, path: str):
        self.path = path
        self.level = 0.0
        self.heard_speech = False
        self.started_at = 0.0
        self._duration = 0.0

    def start(self):
        with wave.open(self.path, "rb") as w:
            self._duration = w.getnframes() / w.getframerate()
        self.started_at = time.monotonic()
        self.heard_speech = True

    def silence_for(self) -> float:
        return max(0.0, time.monotonic() - self.started_at - self._duration)

    def stop(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()

    def cancel(self):
        pass
