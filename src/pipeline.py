"""Conversation pipeline for ASR -> LLM -> TTS with barge-in support."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .audio.aec import StreamingEchoCanceller, resample_float32
from .audio.player import AudioPlayer
from .audio.recorder import VoiceRecorder
from .asr.sensevoice_asr import SenseVoiceASR
from .config import Config
from .llm.qwen_llm import QwenLLM
from .tts.indextts2_tts import IndexTTS2TTS

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[。！？?!\n…]+")
# Prefer clause breaks; then whitespace so English words stay intact.
_CLAUSE_BREAK_CHARS = "，。、；;:"
_THINK_OPEN = re.compile(r"<think>")
_THINK_CLOSE = re.compile(r"</think>")


@dataclass
class _Sentinel:
    pass


SENTINEL = _Sentinel()


class SentenceBuffer:
    def __init__(self, min_len: int = 4, max_len: int = 50):
        self.buf = ""
        self.min_len = min_len
        self.max_len = max_len
        # If no word/clause boundary appears, wait until this hard limit.
        self.hard_max_len = max(max_len * 3, 100)

    @staticmethod
    def _find_soft_cut(text: str, max_len: int) -> int | None:
        """Return inclusive end index for a safe cut within text[:max_len].

        Never splits inside an ASCII/Latin word: prefer punctuation, then
        whitespace. Returns None if no safe boundary exists yet.
        """
        if max_len <= 0 or not text:
            return None
        window = text[:max_len]
        cut = -1
        for ch in _CLAUSE_BREAK_CHARS:
            pos = window.rfind(ch)
            if pos > cut:
                cut = pos
        if cut > 0:
            return cut

        for i in range(len(window) - 1, 0, -1):
            if window[i].isspace():
                # Keep the preceding word intact; drop the trailing space.
                end = i - 1
                return end if end >= 0 else None
        return None

    def add(self, token: str) -> list[str]:
        self.buf += token
        ready: list[str] = []
        while True:
            m = _SENTENCE_END.search(self.buf)
            if not m:
                break
            idx = m.end()
            sentence = self.buf[:idx].strip()
            self.buf = self.buf[idx:]
            if sentence and len(sentence) >= self.min_len:
                ready.append(sentence)
            else:
                self.buf = sentence + self.buf
                break
        while len(self.buf) >= self.max_len:
            cut = self._find_soft_cut(self.buf, self.max_len)
            if cut is None:
                # No space/punctuation yet (e.g. mid-word stream). Wait unless
                # the buffer grows past hard_max_len.
                if len(self.buf) < self.hard_max_len:
                    break
                # Last resort: cut at last whitespace anywhere, else hard cut.
                cut = self._find_soft_cut(self.buf, len(self.buf))
                if cut is None:
                    cut = self.max_len - 1
            piece = self.buf[: cut + 1].strip()
            self.buf = self.buf[cut + 1 :].lstrip()
            if piece:
                ready.append(piece)
            else:
                break
        return ready

    def flush(self) -> list[str]:
        s = self.buf.strip()
        self.buf = ""
        return [s] if s else []


def _strip_think(text: str, state: dict) -> tuple[str, bool]:
    out = ""
    i = 0
    skipping = state.get("skipping", False)
    while i < len(text):
        if skipping:
            close = _THINK_CLOSE.search(text, i)
            if close:
                i = close.end()
                skipping = False
            else:
                break
        else:
            open_ = _THINK_OPEN.search(text, i)
            if open_:
                out += text[i: open_.start()]
                i = open_.end()
                skipping = True
            else:
                out += text[i:]
                break
    state["skipping"] = skipping
    return out, skipping


class ConversationPipeline:
    def __init__(
        self,
        config: Config,
        cosyvoice_repo_path: Optional[str] = None,
        on_audio_level: Optional[Callable[[float, float], None]] = None,
    ):
        self.config = config
        self.aec: Optional[StreamingEchoCanceller] = None
        if config.aec.enabled:
            self.aec = StreamingEchoCanceller(
                sample_rate=config.audio.sample_rate,
                channels=config.audio.channels,
                frame_ms=config.aec.frame_ms,
                stream_delay_ms=config.aec.stream_delay_ms,
                enable_ns=config.aec.enable_ns,
                enable_agc=config.aec.enable_agc,
                enable_vad=config.aec.enable_vad,
                echo_gate=config.aec.echo_gate,
                echo_gate_ratio=config.aec.echo_gate_ratio,
                echo_gate_correlation=config.aec.echo_gate_correlation,
                echo_gate_search_ms=config.aec.echo_gate_search_ms,
                playback_hold_ms=config.aec.playback_hold_ms,
            )
            if self.aec.enabled:
                logger.info("AEC backend enabled for external-speaker barge-in")
            else:
                logger.warning(
                    "AEC backend is unavailable; correlation echo gate remains active for barge-in"
                )
            if config.aec.echo_gate:
                logger.info(
                    "Echo gate enabled (ratio=%.2f, correlation=%.2f, search=%dms)",
                    config.aec.echo_gate_ratio,
                    config.aec.echo_gate_correlation,
                    config.aec.echo_gate_search_ms,
                )

        self._legacy_echo_suppression = config.dialog.echo_suppression and not (
            self.aec is not None and config.aec.echo_gate
        )

        logger.info("=== Initializing modules ===")
        self.asr = SenseVoiceASR(config.asr)
        self.llm = QwenLLM(config.llm)
        self.tts = IndexTTS2TTS(config.tts, cosyvoice_repo_path=cosyvoice_repo_path)
        player_sample_rate = config.audio.sample_rate if self.aec is not None else config.tts.sample_rate
        self.player = AudioPlayer(
            sample_rate=player_sample_rate,
            on_playback_chunk=self.aec.feed_playback if self.aec is not None else None,
        )

        self.asr_queue: "queue.Queue[object]" = queue.Queue()
        self.tts_queue: "queue.Queue[object]" = queue.Queue()
        self.play_queue: "queue.Queue[object]" = queue.Queue()

        self.history: list[dict] = []
        self._barge_in = threading.Event()
        self._busy = threading.Event()
        self._assistant_output_active = threading.Event()
        self._stop = threading.Event()
        self._playback_started_at = 0.0
        self._playback_grace_pending = True

        self.recorder = VoiceRecorder(
            audio_cfg=config.audio,
            vad_cfg=config.vad,
            on_utterance=self._on_utterance,
            on_speech_start=self._on_speech_start,
            on_audio_level=on_audio_level,
            aec=self.aec,
            speech_start_confirm_ms=config.dialog.barge_in_confirm_ms,
            speech_start_min_rms=config.dialog.barge_in_min_rms,
        )

        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._stop.clear()
        if self.recorder.is_muted():
            self.recorder.unmute()
        # 先预热扬声器, 避免进程启动后第一句字首被 WASAPI 冷启动吞掉
        self.player.warmup()
        self._threads = [
            threading.Thread(target=self._asr_worker, daemon=True, name="asr"),
            threading.Thread(target=self._tts_worker, daemon=True, name="tts"),
            threading.Thread(target=self._play_worker, daemon=True, name="player"),
        ]
        for t in self._threads:
            t.start()
        self.recorder.start()
        logger.info("Conversation pipeline started")

    def stop(self) -> None:
        self._stop.set()
        self.recorder.stop()
        self.player.close()
        self.asr_queue.put(SENTINEL)
        self.tts_queue.put(SENTINEL)
        self.play_queue.put(SENTINEL)
        for t in self._threads:
            t.join(timeout=2)
        logger.info("Conversation pipeline stopped")

    def set_voice_enabled(self, enabled: bool) -> bool:
        """供网页开关控制麦克风采集，不影响正在播放的助手语音。"""
        active = self.recorder.set_capture_enabled(enabled)
        logger.info("Voice capture %s", "enabled" if active else "paused")
        return active

    def _on_speech_start(self) -> None:
        if not self.config.dialog.enable_barge_in:
            return
        if not (self._busy.is_set() or self._assistant_output_active.is_set()):
            return
        # 开播后短暂忽略打断: 回声尚未被 AEC 收敛时, 180ms 确认也容易把第一个字掐断。
        grace_s = max(0.35, (self.config.dialog.barge_in_confirm_ms or 0) / 1000.0 + 0.25)
        if (time.monotonic() - self._playback_started_at) < grace_s:
            logger.debug("Ignore barge-in during playback onset grace (%.0fms)", grace_s * 1000)
            return
        logger.info("User speech detected, barging in")
        self._barge_in.set()
        self.player.stop()
        self._drain_queues()
        self._assistant_output_active.clear()
        self._playback_grace_pending = True
        # 保留短暂播放参考，继续覆盖声卡与扬声器停止后的尾音。

    def _on_utterance(self, audio: np.ndarray) -> None:
        if audio.size < self.config.audio.sample_rate * 0.2:
            logger.debug("Utterance too short, ignore")
            return
        rms = float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))
        if rms < 0.002:
            logger.info("Utterance is too quiet after AEC/gating, ignore (rms=%.5f)", rms)
            return
        self.asr_queue.put(audio)

    def _asr_worker(self) -> None:
        while not self._stop.is_set():
            item = self.asr_queue.get()
            if item is SENTINEL:
                continue
            audio = item
            self._busy.set()
            self._barge_in.clear()
            try:
                text = self.asr.transcribe(audio)
            except Exception:
                logger.exception("ASR failed")
                self._busy.clear()
                continue
            if not text:
                audio_rms = float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))
                audio_sec = float(audio.size) / float(self.config.audio.sample_rate)
                logger.info("ASR returned empty text (audio=%.2fs, rms=%.5f)", audio_sec, audio_rms)
                self._busy.clear()
                continue
            logger.info("User: %s", text)
            try:
                self._drive_llm_to_tts(text)
            except Exception:
                logger.exception("LLM/TTS pipeline failed")
            finally:
                self._busy.clear()

    def _drive_llm_to_tts(self, user_text: str) -> None:
        history_snapshot = list(self.history[-self.config.dialog.max_history :])
        sent_buffer = SentenceBuffer(
            min_len=self.config.dialog.sentence_min_len,
            max_len=self.config.dialog.sentence_max_len,
        )
        think_state: dict = {"skipping": False}

        full_reply = ""
        t_start = time.time()

        for token in self.llm.stream(user_text, history=history_snapshot):
            if self._barge_in.is_set() or self._stop.is_set():
                logger.info("LLM generation interrupted")
                return

            visible, _ = _strip_think(token, think_state)
            if not visible:
                continue
            full_reply += visible

            for sentence in sent_buffer.add(visible):
                if self._barge_in.is_set():
                    return
                logger.info("Assistant sentence: %s", sentence)
                self.tts_queue.put(sentence)

        for sentence in sent_buffer.flush():
            if self._barge_in.is_set():
                return
            logger.info("Assistant sentence: %s", sentence)
            self.tts_queue.put(sentence)

        self.tts_queue.put(SENTINEL)

        if full_reply.strip():
            self.history.append({"role": "user", "content": user_text})
            self.history.append({"role": "assistant", "content": full_reply.strip()})
            max_keep = self.config.dialog.max_history * 2
            if len(self.history) > max_keep:
                self.history = self.history[-max_keep:]
        logger.info("Round trip took %.2fs", time.time() - t_start)

    def _tts_worker(self) -> None:
        while not self._stop.is_set():
            item = self.tts_queue.get()
            if item is SENTINEL:
                self.play_queue.put(SENTINEL)
                continue
            sentence = str(item)
            if not sentence.strip():
                continue
            if self._barge_in.is_set():
                continue
            try:
                t0 = time.time()
                produced = 0
                self._assistant_output_active.set()
                if self._legacy_echo_suppression and not self.recorder.is_muted():
                    logger.debug("TTS start: mute VAD (legacy echo suppression)")
                    self.recorder.mute()
                playback_sr = self.player.sample_rate
                for audio_chunk in self.tts.synthesize(sentence):
                    if self._barge_in.is_set():
                        break
                    play_chunk = audio_chunk
                    if self.aec is not None:
                        server_sr = getattr(self.tts, "_server_sample_rate", None) or self.config.tts.sample_rate
                        if server_sr != playback_sr:
                            play_chunk = resample_float32(audio_chunk, server_sr, playback_sr)
                    elif produced == 0:
                        server_sr = getattr(self.tts, "_server_sample_rate", None)
                        if server_sr and server_sr != self.player.sample_rate:
                            self.player.set_sample_rate(server_sr)
                    self.play_queue.put(play_chunk)
                    produced += 1
                logger.debug("TTS '%s' -> %d chunks %.2fs", sentence[:20], produced, time.time() - t0)
            except Exception:
                logger.exception("TTS worker failed")
            finally:
                if self._legacy_echo_suppression and self.recorder.is_muted():
                    logger.debug("TTS worker done, restoring VAD")
                    self.recorder.unmute()

    def _play_worker(self) -> None:
        while not self._stop.is_set():
            item = self.play_queue.get()
            if item is SENTINEL:
                # 等内部队列无缝播完再标 idle, 避免句中小块之间插入静音杂音
                self.player.wait_drain()
                if self._legacy_echo_suppression and self.recorder.is_muted():
                    logger.debug("TTS finished, restoring VAD")
                    self.recorder.unmute()
                self._assistant_output_active.clear()
                self._playback_grace_pending = True
                self.player.mark_idle()
                continue
            if self._barge_in.is_set():
                continue
            if self._playback_grace_pending:
                self._playback_started_at = time.monotonic()
                self._playback_grace_pending = False
            try:
                # 非阻塞入队: TTS 0.2s 小块连续拼在一起, 不在块间隙填静音
                self.player.submit(item)
            except Exception:
                logger.exception("Playback failed")

    def _drain_queues(self) -> None:
        for _ in range(self.tts_queue.qsize()):
            try:
                self.tts_queue.get_nowait()
            except queue.Empty:
                break
        for _ in range(self.play_queue.qsize()):
            try:
                self.play_queue.get_nowait()
            except queue.Empty:
                break
