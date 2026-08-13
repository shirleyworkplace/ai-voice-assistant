"""音频播放模块, 支持流式播放与打断 (barge-in)。

使用 callback 模式常驻 OutputStream + 内部队列:
- 声卡按固定节奏取数, 空闲时填静音, 避免 write 模式 underrun 切字首。
- submit() 非阻塞入队, 连续 TTS 小块无缝拼接, 避免块间隙静音造成字尾杂音。
- 仅在一轮回复结束 wait_drain() 后再 mark_idle / 加 lead-in。
- stop() 清空队列, 供 barge-in 使用。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Deque, Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

_LEAD_IN_MS = 80
_COLD_START_EXTRA_MS = 120
_FADE_IN_MS = 8
_BLOCKSIZE = 512
_STREAM_LATENCY = 0.12


class AudioPlayer:
    """callback 队列播放器: submit 连续入队, wait_drain 等播完。"""

    def __init__(
        self,
        sample_rate: int = 22050,
        channels: int = 1,
        on_playback_chunk: Optional[Callable[[np.ndarray, int], None]] = None,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.on_playback_chunk = on_playback_chunk

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue: Deque[np.ndarray] = deque()
        self._current: Optional[np.ndarray] = None
        self._current_pos = 0
        self._pending_samples = 0

        self._stream: Optional[sd.OutputStream] = None
        self._stop_event = threading.Event()
        self._needs_lead_in = True
        self._cold_start = True

    def set_sample_rate(self, sample_rate: int) -> None:
        if not sample_rate or sample_rate == self.sample_rate:
            return
        logger.info("AudioPlayer 采样率调整: %d -> %d", self.sample_rate, sample_rate)
        self.sample_rate = sample_rate
        self._restart_stream()

    def warmup(self) -> None:
        """启动时打开常驻输出流并空跑一段时间, 消化设备冷启动。"""
        if not self._ensure_stream():
            return
        time.sleep(0.35)
        self._needs_lead_in = True
        self._cold_start = False
        logger.info("AudioPlayer warmup done (sr=%d, callback mode)", self.sample_rate)

    def mark_idle(self) -> None:
        """一轮回复结束: 流保持开, 下次真实音频前加 lead-in。"""
        self._needs_lead_in = True

    def release(self) -> None:
        self.mark_idle()

    def submit(self, audio: np.ndarray) -> None:
        """非阻塞入队。连续调用可无缝拼接, 不会在块间插入静音。"""
        if audio is None or audio.size == 0:
            return
        if not self._ensure_stream():
            return

        pcm = np.asarray(audio, dtype=np.float32).reshape(-1)
        peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
        if peak > 1.0:
            pcm = pcm / peak

        self._stop_event.clear()

        with self._cv:
            if self._stop_event.is_set():
                return
            if self._needs_lead_in:
                lead_ms = _LEAD_IN_MS + (_COLD_START_EXTRA_MS if self._cold_start else 0)
                lead_n = max(1, int(self.sample_rate * lead_ms / 1000.0))
                lead = np.zeros(lead_n, dtype=np.float32)
                pcm = np.concatenate([lead, pcm])
                pcm = self._fade_in(pcm, _FADE_IN_MS, skip_samples=lead_n)
                self._needs_lead_in = False
                self._cold_start = False
            self._queue.append(pcm)
            self._pending_samples += int(pcm.size)
            self._cv.notify_all()

    def play(self, audio: np.ndarray) -> None:
        """兼容旧接口: 入队并等到该轮队列排空 (单块场景)。"""
        self.submit(audio)
        self.wait_drain()

    def wait_drain(self, timeout: Optional[float] = None) -> bool:
        """等到内部队列与当前块全部播完, 或被 stop()。"""
        deadline = None if timeout is None else (time.monotonic() + timeout)
        with self._cv:
            while self._pending_samples > 0 and not self._stop_event.is_set():
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                self._cv.wait(timeout=0.05 if remaining is None else min(0.05, remaining))
            return self._pending_samples <= 0

    def stop(self) -> None:
        """barge-in: 丢弃未播完数据并唤醒 wait_drain()。"""
        self._stop_event.set()
        with self._cv:
            self._queue.clear()
            self._current = None
            self._current_pos = 0
            self._pending_samples = 0
            self._cv.notify_all()
        self._needs_lead_in = True

    def reset(self) -> None:
        self._stop_event.clear()

    def close(self) -> None:
        self.stop()
        self._close_stream()

    def _fade_in(self, pcm: np.ndarray, fade_ms: float, skip_samples: int = 0) -> np.ndarray:
        n = max(1, int(self.sample_rate * fade_ms / 1000.0))
        start = max(0, min(skip_samples, pcm.size))
        end = min(pcm.size, start + n)
        if end <= start:
            return pcm
        out = pcm.copy()
        ramp = np.linspace(0.0, 1.0, end - start, dtype=np.float32)
        out[start:end] *= ramp
        return out

    def _ensure_stream(self) -> bool:
        if self._stream is not None:
            return True
        try:
            stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=_BLOCKSIZE,
                latency=_STREAM_LATENCY,
                callback=self._callback,
            )
            stream.start()
            with self._lock:
                self._stream = stream
            self._needs_lead_in = True
            return True
        except Exception:
            logger.exception("打开 OutputStream 失败")
            return False

    def _restart_stream(self) -> None:
        self.stop()
        self._close_stream()
        self._stop_event.clear()
        self._ensure_stream()

    def _close_stream(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._needs_lead_in = True

    def _callback(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            logger.debug("AudioPlayer status: %s", status)

        outdata.fill(0.0)
        filled = 0

        with self._cv:
            while filled < frames:
                if self._current is None:
                    if not self._queue:
                        break
                    self._current = self._queue.popleft()
                    self._current_pos = 0

                need = frames - filled
                avail = int(self._current.size) - self._current_pos
                n = min(need, avail)
                if n > 0:
                    outdata[filled : filled + n, 0] = self._current[
                        self._current_pos : self._current_pos + n
                    ]
                    filled += n
                    self._current_pos += n
                    self._pending_samples = max(0, self._pending_samples - n)

                if self._current_pos >= int(self._current.size):
                    self._current = None
                    self._current_pos = 0

            if self._pending_samples == 0:
                self._cv.notify_all()

        if self.on_playback_chunk is not None and filled > 0:
            try:
                self.on_playback_chunk(outdata[:filled, 0].copy(), self.sample_rate)
            except Exception:
                logger.debug("playback reference callback failed", exc_info=True)
