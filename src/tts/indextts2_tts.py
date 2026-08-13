"""IndexTTS2 HTTP client for the conversation pipeline.

IndexTTS2 /v1/tts returns a complete WAV file rather than chunked raw PCM.
This adapter keeps the existing synthesize() contract by decoding that WAV to
float32 mono audio and yielding playback-sized chunks.
"""
from __future__ import annotations

import io
import logging
import os
import time
import wave
from typing import Iterator, Optional

import numpy as np
import requests

from ..config import TtsCfg

logger = logging.getLogger(__name__)


class IndexTTS2TTS:
    """Call an IndexTTS2 /v1/tts service while preserving the old TTS interface."""

    def __init__(self, cfg: TtsCfg, cosyvoice_repo_path: Optional[str] = None):
        self.cfg = cfg
        self.url = cfg.base_url.rstrip("/") + cfg.endpoint
        self._voice_path = cfg.prompt_audio
        self._server_sample_rate: Optional[int] = None
        self._warned_speed_ignored = False

        if not self._voice_path:
            logger.warning("TTS voice prompt is not configured; IndexTTS2 synthesis will fail")
        elif not os.path.isfile(self._voice_path):
            logger.warning("TTS voice prompt file does not exist: %s", self._voice_path)

        try:
            r = requests.get(cfg.base_url.rstrip("/") + "/health", headers=self._headers(), timeout=3)
            logger.info("TTS service probe: %s (status=%s)", self.url, r.status_code)
        except Exception as exc:
            logger.warning("TTS service probe failed; will retry on synthesis: %s", exc)

    def synthesize(self, text: str, speed: Optional[float] = None) -> Iterator[np.ndarray]:
        """Synthesize one sentence and yield float32 mono audio chunks."""
        text = (text or "").strip()
        if not text:
            return

        requested_speed = self.cfg.speed if speed is None else speed
        if requested_speed != 1.0 and not self._warned_speed_ignored:
            logger.warning("IndexTTS2 /v1/tts does not expose speed; ignoring configured speed %.2f", requested_speed)
            self._warned_speed_ignored = True

        if not self._voice_path or not os.path.isfile(self._voice_path):
            logger.error("TTS voice prompt is missing; skip synthesis: %s", self._voice_path)
            return

        data = {
            "text": text,
            "interval_silence": str(self.cfg.interval_silence),
            "max_text_tokens_per_segment": str(self.cfg.max_text_tokens_per_segment),
        }

        t0 = time.time()
        try:
            with open(self._voice_path, "rb") as voice_file:
                files = {
                    "voice": (
                        os.path.basename(self._voice_path),
                        voice_file,
                        "audio/wav",
                    )
                }
                resp = requests.post(
                    self.url,
                    files=files,
                    data=data,
                    headers=self._headers(),
                    timeout=self.cfg.timeout,
                )
            resp.raise_for_status()
        except Exception:
            logger.exception("TTS request failed: %s", self.url)
            return

        try:
            audio, sample_rate = _decode_wav_bytes(resp.content)
        except Exception:
            logger.exception("Failed to decode IndexTTS2 WAV response")
            return

        self._server_sample_rate = sample_rate
        if sample_rate != self.cfg.sample_rate:
            logger.warning(
                "TTS server sample rate %d differs from configured %d",
                sample_rate,
                self.cfg.sample_rate,
            )

        if audio.size == 0:
            logger.warning("TTS produced no audio (text: %s)", text[:50])
            return

        logger.info("TTS response took %.2fs (text: %s)", time.time() - t0, text[:30])
        chunk_samples = max(1024, int(sample_rate * 0.2))
        for start in range(0, audio.size, chunk_samples):
            yield audio[start : start + chunk_samples]

    def _headers(self) -> dict[str, str]:
        if self.cfg.api_key:
            return {"X-API-Key": self.cfg.api_key}
        return {}


def _decode_wav_bytes(payload: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(payload), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if sample_width == 1:
        audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        audio = _decode_int24_le(frames)
    elif sample_width == 4:
        audio = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV sample width: {sample_width}")

    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)

    return np.ascontiguousarray(audio, dtype=np.float32), sample_rate


def _decode_int24_le(frames: bytes) -> np.ndarray:
    raw = np.frombuffer(frames, dtype=np.uint8)
    if raw.size % 3:
        raw = raw[: raw.size - (raw.size % 3)]
    triples = raw.reshape(-1, 3).astype(np.int32)
    values = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
    values = np.where(values & 0x800000, values - 0x1000000, values)
    return values.astype(np.float32) / 8388608.0
