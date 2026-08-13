"""麦克风采集 + VAD 端点检测, 输出完整 utterance。

使用 sounddevice 的 InputStream 回调, 在回调线程中按帧喂入 VAD,
检测到一句话结束时通过回调通知上层。
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from ..config import AudioCfg, VadCfg
from .aec import StreamingEchoCanceller
from ..vad.silero_vad import SileroVAD

logger = logging.getLogger(__name__)

# 传递给上层的 utterance 回调: (audio_float32_1d, )
OnUtterance = Callable[[np.ndarray], None]
OnAudioLevel = Callable[[float, float], None]


class VoiceRecorder:
    """麦克风实时采集 + Silero VAD 端点检测。"""

    def __init__(
        self,
        audio_cfg: AudioCfg,
        vad_cfg: VadCfg,
        on_utterance: OnUtterance,
        on_speech_start: Optional[Callable[[], None]] = None,
        on_audio_level: Optional[OnAudioLevel] = None,
        aec: Optional[StreamingEchoCanceller] = None,
        speech_start_confirm_ms: int = 180,
        speech_start_min_rms: float = 0.003,
    ):
        self.audio_cfg = audio_cfg
        self.vad_cfg = vad_cfg
        self.on_utterance = on_utterance
        self.on_speech_start = on_speech_start
        self.on_audio_level = on_audio_level
        self.aec = aec
        self._speech_start_confirm_samples = max(
            0, int(audio_cfg.sample_rate * speech_start_confirm_ms / 1000)
        )
        self._speech_start_min_rms = max(0.0, float(speech_start_min_rms))

        self.vad = SileroVAD(vad_cfg, sampling_rate=audio_cfg.sample_rate)
        self._stream: Optional[sd.InputStream] = None
        self._stopped = threading.Event()
        self._muted = threading.Event()       # 静音期间不喂 VAD, 丢弃采集帧
        self._capture_enabled = threading.Event()
        self._capture_enabled.set()
        self._lock = threading.Lock()
        self._speech_start_fired = False
        self._speech_loud_samples = 0

    def mute(self) -> None:
        """静音: 暂停 VAD 检测, 用于 TTS 播放期间抑制回声反馈。"""
        self._muted.set()
        with self._lock:
            self.vad.reset()
            self._speech_start_fired = False
            self._speech_loud_samples = 0

    def unmute(self) -> None:
        """解除静音: 恢复 VAD 检测。"""
        with self._lock:
            self.vad.reset()
            self._speech_start_fired = False
            self._speech_loud_samples = 0
        self._muted.clear()

    def is_muted(self) -> bool:
        return self._muted.is_set()

    def set_capture_enabled(self, enabled: bool) -> bool:
        """启用或暂停用户语音采集，暂停时保留声卡流但不送入 VAD/ASR。"""
        with self._lock:
            self.vad.reset()
            self._speech_start_fired = False
            self._speech_loud_samples = 0
            if enabled:
                self._capture_enabled.set()
            else:
                self._capture_enabled.clear()
        return self._capture_enabled.is_set()

    def is_capture_enabled(self) -> bool:
        return self._capture_enabled.is_set()

    def _callback(self, indata: np.ndarray, frames: int, time_info, status):
        if status:
            logger.debug("音频流状态: %s", status)
        raw_frame = indata[:, 0].copy()  # (frames,) float32
        # AEC 会从麦克风信号里消掉扬声器回放声；处理后的 frame 同时给
        # VAD 和 ASR 使用，避免把 TTS 回声识别成用户说话。
        frame = raw_frame
        if self.aec is not None:
            try:
                frame = self.aec.process_mic(frame)
            except Exception:
                logger.warning("AEC processing failed; using raw microphone frame", exc_info=True)
                self.aec = None
        # 静音期间 (TTS 播放中) 丢弃采集帧, 不喂 VAD, 防止回声反馈
        if self._muted.is_set() or not self._capture_enabled.is_set():
            return
        self._emit_audio_level(frame)

        with self._lock:
            if self._stopped.is_set():
                return
            # Silero 用同一份 AEC 后音频做端点检测和缓存；检测到一句话结束后，
            # 缓存的这段音频会直接送给 ASR。
            utterance = self.vad.feed(frame)
            should_notify_speech_start = False

            # VAD 的单帧判定只作为候选。播放期间先积累足够长、足够响的
            # AEC 后语音，再真正打断，避免残余回声先停掉 TTS 又被 ASR 丢弃。
            if self.vad.is_speaking and not self._speech_start_fired:
                frame_rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
                if frame_rms >= self._speech_start_min_rms:
                    self._speech_loud_samples += frame.size
                else:
                    self._speech_loud_samples = 0
                if self._speech_loud_samples >= self._speech_start_confirm_samples:
                    self._speech_start_fired = True
                    should_notify_speech_start = True
            elif not self.vad.is_speaking:
                self._speech_start_fired = False
                self._speech_loud_samples = 0

        if should_notify_speech_start and self.on_speech_start is not None:
            try:
                self.on_speech_start()
            except Exception:
                logger.exception("on_speech_start 回调异常")

        if utterance is not None and utterance.size > 0:
            try:
                self.on_utterance(utterance)
            except Exception:
                logger.exception("on_utterance 回调异常")

    def _emit_audio_level(self, frame: np.ndarray) -> None:
        if self.on_audio_level is None:
            return
        try:
            rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
            peak = float(np.max(np.abs(frame))) if frame.size else 0.0
            dbfs = 20.0 * math.log10(max(rms, 1e-7))
            amplitude = min(1.0, max(0.0, (dbfs + 58.0) / 42.0))
            tone = min(1.0, max(0.0, peak / max(rms * 4.0, 1e-7)))
            self.on_audio_level(amplitude, tone)
        except Exception:
            logger.debug("on_audio_level 回调异常", exc_info=True)

    def start(self) -> None:
        self._stopped.clear()
        self._muted.clear()   # 启动时确保非静音, 避免上次残留状态
        self._capture_enabled.set()
        self._stream = sd.InputStream(
            samplerate=self.audio_cfg.sample_rate,
            channels=self.audio_cfg.channels,
            blocksize=self.audio_cfg.block_size,
            dtype=self.audio_cfg.dtype,
            callback=self._callback,
        )
        self._stream.start()
        logger.info(
            "麦克风采集已启动 (sr=%d, block=%d)",
            self.audio_cfg.sample_rate,
            self.audio_cfg.block_size,
        )

    def stop(self) -> None:
        self._stopped.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("麦克风采集已停止")

    def reset_vad(self) -> None:
        """打断后调用, 清空 VAD 累积状态, 避免误触发。"""
        with self._lock:
            self.vad.reset()
            self._speech_start_fired = False
            self._speech_loud_samples = 0
