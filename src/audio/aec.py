"""Optional echo cancellation adapter."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

try:
    from aec_audio_processing import AudioProcessor  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    AudioProcessor = None  # type: ignore

logger = logging.getLogger(__name__)


def resample_float32(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if audio is None or audio.size == 0 or src_sr == dst_sr:
        return audio.astype(np.float32, copy=False)
    audio = audio.astype(np.float32, copy=False)
    src_len = int(audio.size)
    dst_len = max(1, int(round(src_len * float(dst_sr) / float(src_sr))))
    if src_len == 1:
        return np.full(dst_len, float(audio[0]), dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=src_len, endpoint=False, dtype=np.float64)
    x_new = np.linspace(0.0, 1.0, num=dst_len, endpoint=False, dtype=np.float64)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def _float_to_pcm16(audio: np.ndarray) -> bytes:
    if audio is None or audio.size == 0:
        return b""
    clipped = np.clip(audio.astype(np.float32, copy=False), -1.0, 1.0)
    pcm = np.round(clipped * 32767.0).astype(np.int16)
    return pcm.tobytes()


def _pcm16_to_float(data: bytes) -> np.ndarray:
    if not data:
        return np.zeros(0, dtype=np.float32)
    pcm = np.frombuffer(data, dtype=np.int16)
    return (pcm.astype(np.float32) / 32768.0).copy()


class StreamingEchoCanceller:
    """Thin wrapper around an optional WebRTC-style AEC backend."""

    def __init__(
        self,
        sample_rate: int,
        channels: int = 1,
        frame_ms: int = 10,
        stream_delay_ms: int = 50,
        enable_ns: bool = True,
        enable_agc: bool = False,
        enable_vad: bool = False,
        echo_gate: bool = True,
        echo_gate_ratio: float = 0.70,
        echo_gate_correlation: float = 0.55,
        echo_gate_search_ms: int = 350,
        playback_hold_ms: int = 250,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.frame_samples = max(1, int(sample_rate * frame_ms / 1000))
        self.enabled = AudioProcessor is not None
        self._lock = threading.Lock()
        self._processor = None
        self._reverse_method: Optional[Callable[..., object]] = None
        self._reverse_warned = False
        self._mic_buffer = np.zeros(0, dtype=np.float32)
        self._processed_mic_buffer = np.zeros(0, dtype=np.float32)
        self._processed_raw_buffer = np.zeros(0, dtype=np.float32)
        self._playback_buffer = np.zeros(0, dtype=np.float32)
        self._echo_gate = echo_gate
        self._echo_gate_ratio = max(0.05, float(echo_gate_ratio))
        self._echo_gate_correlation = min(0.98, max(0.10, float(echo_gate_correlation)))
        self._echo_gate_search_samples = max(
            self.frame_samples,
            int(self.sample_rate * max(10, int(echo_gate_search_ms)) / 1000),
        )
        self._playback_hold_sec = max(0.0, float(playback_hold_ms) / 1000.0)
        self._playback_rms = 0.0
        self._playback_active_until = 0.0
        # 保存最近的播放参考。外放回声比声卡写入晚到达麦克风，需要在延迟窗口内匹配。
        self._playback_history = np.zeros(0, dtype=np.float32)

        if not self.enabled:
            logger.warning(
                "AEC backend is unavailable; using correlation echo gate without acoustic cancellation."
            )
            return

        self._processor = self._build_processor(enable_ns, enable_agc, enable_vad)
        if self._processor is None:
            self.enabled = False
            return

        self._configure_backend(stream_delay_ms)
        self._reverse_method = self._find_reverse_method()
        if self._reverse_method is None:
            self.enabled = False
            logger.warning(
                "AEC backend is present, but no reverse-stream API was found; disabling AEC."
            )

    def _build_processor(self, enable_ns: bool, enable_agc: bool, enable_vad: bool):
        if AudioProcessor is None:
            return None
        ctor_attempts = [
            dict(enable_aec=True, enable_ns=enable_ns, enable_agc=enable_agc, enable_vad=enable_vad),
            dict(enable_aec=True, enable_noise_suppression=enable_ns, enable_agc=enable_agc, enable_vad=enable_vad),
            dict(),
        ]
        for kwargs in ctor_attempts:
            try:
                return AudioProcessor(**kwargs)
            except TypeError:
                continue
            except Exception as exc:
                logger.warning("Failed to initialize AEC backend: %s", exc)
                return None
        return None

    def _call_first(self, names: list[str], *args):
        if self._processor is None:
            return None
        for name in names:
            fn = getattr(self._processor, name, None)
            if callable(fn):
                return fn(*args)
        return None

    def _find_reverse_method(self) -> Optional[Callable[..., object]]:
        if self._processor is None:
            return None
        for name in (
            "process_reverse_stream",
            "processReverseStream",
            "process_reverse",
            "reverse_stream",
            "push_reverse_stream",
            "add_reverse_stream",
        ):
            fn = getattr(self._processor, name, None)
            if callable(fn):
                return fn
        return None

    def _configure_backend(self, stream_delay_ms: int) -> None:
        if self._processor is None:
            return
        for name, args in (
            ("set_stream_format", (self.sample_rate, self.channels)),
            ("set_reverse_stream_format", (self.sample_rate, self.channels)),
            ("set_stream_delay", (stream_delay_ms,)),
        ):
            fn = getattr(self._processor, name, None)
            if callable(fn):
                try:
                    fn(*args)
                except TypeError:
                    try:
                        fn(args[0])
                    except Exception:
                        logger.debug("AEC backend rejected %s", name, exc_info=True)
                except Exception:
                    logger.debug("AEC backend rejected %s", name, exc_info=True)

    def _process_frame(self, frame: np.ndarray, reverse: bool = False) -> np.ndarray:
        if self._processor is None or frame.size == 0:
            return frame.astype(np.float32, copy=False)
        pcm = _float_to_pcm16(frame)
        if not pcm:
            return frame.astype(np.float32, copy=False)
        if reverse:
            if self._reverse_method is None:
                return frame.astype(np.float32, copy=False)
            result = self._reverse_method(pcm)
            if result is None:
                return frame.astype(np.float32, copy=False)
            if isinstance(result, (bytes, bytearray)):
                return _pcm16_to_float(bytes(result))
            return frame.astype(np.float32, copy=False)
        result = self._call_first(["process_stream", "processStream"], pcm)
        if result is None:
            return frame.astype(np.float32, copy=False)
        if isinstance(result, (bytes, bytearray)):
            return _pcm16_to_float(bytes(result))
        return frame.astype(np.float32, copy=False)

    def process_mic(self, frame: np.ndarray) -> np.ndarray:
        if not self.enabled:
            raw_frame = frame.astype(np.float32, copy=False)
            # AEC 后端不可用时，仍用播放参考相关性阻止 TTS 直接触发 VAD。
            with self._lock:
                if self._should_gate_echo(raw_frame, raw_frame):
                    return np.zeros(raw_frame.size, dtype=np.float32)
            return raw_frame
        original_len = int(frame.size)
        if original_len == 0:
            return frame.astype(np.float32, copy=False)
        raw_frame = frame.astype(np.float32, copy=False)
        with self._lock:
            # AEC 只能接收严格的 10ms 帧。不能为了凑整补零，否则 512 样本
            # 的回调会被虚构成 640 样本，破坏麦克风与播放参考的时间对齐。
            self._mic_buffer = np.concatenate((self._mic_buffer, raw_frame))
            while self._mic_buffer.size >= self.frame_samples:
                chunk = self._mic_buffer[: self.frame_samples]
                self._mic_buffer = self._mic_buffer[self.frame_samples :]
                self._processed_mic_buffer = np.concatenate(
                    (self._processed_mic_buffer, self._process_frame(chunk, reverse=False))
                )
                # 与处理后音频保持同样的队列边界，供回声相关性判别使用。
                self._processed_raw_buffer = np.concatenate((self._processed_raw_buffer, chunk))

            if self._processed_mic_buffer.size < original_len:
                # 首帧最多缺少 32 个样本，等待下一回调补齐。该延迟避免伪造静音，
                # 也让后续每帧都与真实麦克风时间轴连续对应。
                return np.zeros(original_len, dtype=np.float32)

            processed = self._processed_mic_buffer[:original_len]
            raw_for_processed = self._processed_raw_buffer[:original_len]
            self._processed_mic_buffer = self._processed_mic_buffer[original_len:]
            self._processed_raw_buffer = self._processed_raw_buffer[original_len:]
            if self._should_gate_echo(raw_for_processed, processed):
                # 判定为纯 TTS 回声时必须硬静音，否则 VAD 会把回声切成一句话再送 ASR。
                # 真正的用户插话应在 AEC 后仍保留足够能量，能够通过下面的门限。
                return np.zeros(original_len, dtype=np.float32)
            return processed

    def _should_gate_echo(self, raw_frame: np.ndarray, processed_frame: np.ndarray) -> bool:
        if not self._echo_gate or time.monotonic() > self._playback_active_until:
            return False
        ref = self._playback_rms
        if ref <= 1e-5:
            return False
        processed_rms = float(np.sqrt(np.mean(np.square(processed_frame), dtype=np.float64)))
        gate_threshold = ref * self._echo_gate_ratio
        correlation = self._max_playback_correlation(raw_frame)
        gated = (
            correlation >= self._echo_gate_correlation
            and processed_rms < gate_threshold
        )
        if gated:
            logger.debug(
                "AEC echo gate muted frame: processed_rms=%.5f ref_rms=%.5f threshold=%.5f correlation=%.2f",
                processed_rms,
                ref,
                gate_threshold,
                correlation,
            )
        return gated

    def _max_playback_correlation(self, mic_frame: np.ndarray) -> float:
        """在可能的外放延迟内寻找与麦克风帧最相似的播放片段。"""
        if mic_frame.size < 32 or self._playback_history.size < mic_frame.size:
            return 0.0

        # 降采样只用于回声判别，避免在声卡回调线程做大量逐样本计算。
        step = 4
        mic = mic_frame[::step].astype(np.float64, copy=False)
        reference = self._playback_history[::step].astype(np.float64, copy=False)
        if reference.size < mic.size:
            return 0.0
        mic_energy = float(np.dot(mic, mic))
        if mic_energy <= 1e-10:
            return 0.0
        dots = np.correlate(reference, mic, mode="valid")
        ref_energy = np.convolve(reference * reference, np.ones(mic.size), mode="valid")
        denominator = np.sqrt(np.maximum(ref_energy * mic_energy, 1e-12))
        return float(np.max(np.abs(dots) / denominator)) if denominator.size else 0.0

    def clear_playback_gate(self) -> None:
        """Stop treating current microphone input as playback echo after barge-in."""
        with self._lock:
            self._playback_active_until = 0.0
            self._playback_rms = 0.0
            self._playback_history = np.zeros(0, dtype=np.float32)

    def feed_playback(self, frame: np.ndarray, sample_rate: Optional[int] = None) -> None:
        if frame is None or frame.size == 0:
            return
        src_sr = sample_rate or self.sample_rate
        # 这个方法由真实播放写入循环调用，因此 reverse stream 能和扬声器
        # 实际发出的声音保持时间对齐。
        chunk = resample_float32(frame, src_sr, self.sample_rate)
        rms = float(np.sqrt(np.mean(np.square(chunk), dtype=np.float64))) if chunk.size else 0.0
        with self._lock:
            if rms > 1e-5:
                self._playback_rms = max(rms, self._playback_rms * 0.85)
                self._playback_active_until = time.monotonic() + self._playback_hold_sec
            self._playback_history = np.concatenate((self._playback_history, chunk))
            if self._playback_history.size > self._echo_gate_search_samples:
                self._playback_history = self._playback_history[-self._echo_gate_search_samples :]
            if not self.enabled:
                return
            self._playback_buffer = np.concatenate((self._playback_buffer, chunk))
            while self._playback_buffer.size >= self.frame_samples:
                piece = self._playback_buffer[: self.frame_samples]
                self._playback_buffer = self._playback_buffer[self.frame_samples :]
                result = self._process_frame(piece, reverse=True)
                if result.size == 0 and not self._reverse_warned:
                    self._reverse_warned = True
                    logger.warning(
                        "AEC backend does not expose a reverse-stream method; echo cancellation is inactive."
                    )
                    return
