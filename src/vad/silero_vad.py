"""Silero VAD 流式端点检测 (ONNX Runtime 版)。

用 onnxruntime 替代 torch + silero-vad 库, 体积从 ~2GB 降到 ~100MB,
推理速度基本不变 (CPU 上 ONNX Runtime 甚至更快)。

VADIterator 的状态机逻辑参照 silero-vad 官方实现:
  - 概率 >= threshold → 语音开始
  - 概率 <  threshold 且静音持续 >= min_silence_duration_ms → 语音结束
  - speech_pad_ms 在语音两端填充样本

ONNX 模型接口 (Silero VAD v5):
  输入:
    input  : (batch, samples) float32  音频帧
    state  : (2, batch, 128) float32    LSTM 隐藏状态
    sr     : int64 scalar               采样率
  输出:
    output : (batch, 1) float32         语音概率 (0~1)
    stateN : (2, batch, 128) float32     更新后的 LSTM 状态
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
import onnxruntime as ort

from ..config import VadCfg

logger = logging.getLogger(__name__)


class SileroVAD:
    """基于 ONNX Runtime 的 Silero VAD 流式端点检测。

    保持与原 torch 版相同的 feed()/reset()/is_speaking 接口,
    上层 pipeline 无需改动。
    """

    def __init__(self, cfg: VadCfg, sampling_rate: int = 16000):
        self.cfg = cfg
        self.sampling_rate = sampling_rate

        # 解析模型路径: 优先用 cfg.model_path, 否则在 models/ 目录找 .onnx
        model_path = self._resolve_model_path(cfg.model_path)
        if not model_path or not os.path.isfile(model_path):
            raise FileNotFoundError(
                f"Silero VAD ONNX 模型未找到。请下载 silero_vad.onnx "
                f"放到 models/ 目录, 或在 config.yaml 里配置 vad.model_path。"
                f"  下载地址: https://github.com/snakers4/silero-vad/releases"
            )

        # 创建 ONNX Runtime session (CPU 即可, VAD 模型很小)
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # CPU 部署, 不需要 GPU
        providers = ["CPUExecutionProvider"]
        self._session = ort.InferenceSession(
            model_path, sess_options=sess_options, providers=providers
        )
        self._input_name = self._session.get_inputs()[0].name      # "input"
        self._state_name = self._session.get_inputs()[1].name       # "state"
        self._sr_name = self._session.get_inputs()[2].name          # "sr"
        self._output_name = self._session.get_outputs()[0].name     # "output"
        self._state_out_name = self._session.get_outputs()[1].name  # "stateN"

        # 状态机参数 (参照 silero-vad VADIterator)
        self._threshold = cfg.threshold
        self._min_silence_samples = int(sampling_rate * cfg.min_silence_duration_ms / 1000)
        self._speech_pad_samples = int(sampling_rate * cfg.speech_pad_ms / 1000)

        # LSTM 隐藏状态 (2, batch=1, 128) float32
        self._reset_state_tensor()

        # context buffer: Silero VAD v5 ONNX 要求每次推理把上一帧末尾
        # context_size 个样本拼到当前帧前面, 输入 shape (1, 512+64)=(1, 576)
        # 16kHz -> context_size=64, 8kHz -> 32
        self._context_size = 64 if sampling_rate == 16000 else 32
        self._context = np.zeros((1, self._context_size), dtype=np.float32)

        # 状态机变量
        self._current_sample = 0
        self._is_speech = False
        self._temp_end = 0

        # 累积语音采样, 用于在端点结束时输出完整 utterance
        self._buffer: list[np.ndarray] = []
        self._in_speech = False

        logger.info(
            "Silero VAD (ONNX) 加载完成: %s (sr=%d, threshold=%.2f, context=%d)",
            os.path.basename(model_path), sampling_rate, self._threshold, self._context_size,
        )

    def _resolve_model_path(self, configured: Optional[str]) -> Optional[str]:
        """解析 ONNX 模型路径: 配置 > models/ 目录自动查找。"""
        if configured and os.path.isfile(configured):
            return configured
        # 在项目根 models/ 目录下找 .onnx 文件
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        models_dir = os.path.join(project_root, "models")
        if os.path.isdir(models_dir):
            for name in os.listdir(models_dir):
                if name.lower().endswith(".onnx"):
                    return os.path.join(models_dir, name)
        return configured  # 返回配置值 (即使不存在, 让上层报错信息更明确)

    def _reset_state_tensor(self) -> None:
        """重置 LSTM 隐藏状态张量。"""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def reset(self) -> None:
        """重置内部状态, 用于打断后清空。"""
        self._reset_state_tensor()
        self._context = np.zeros((1, self._context_size), dtype=np.float32)
        self._current_sample = 0
        self._is_speech = False
        self._temp_end = 0
        self._buffer = []
        self._in_speech = False

    def feed(self, frame: np.ndarray) -> Optional[np.ndarray]:
        """喂入一帧 16kHz 单声道 float32 音频 (长度 = block_size)。

        返回:
            - 若本帧触发了语音段结束, 返回完整 utterance (float32, 1D)
            - 否则返回 None
        """
        if frame.dtype != np.float32:
            frame = frame.astype(np.float32)

        # ONNX 推理: 输入 shape (1, samples + context_size)
        # Silero VAD v5 要求把上一帧末尾 context_size 个样本拼到当前帧前面
        audio_input = frame.reshape(1, -1).astype(np.float32)
        x = np.concatenate((self._context, audio_input), axis=1)
        sr_input = np.array(self.sampling_rate, dtype=np.int64)

        try:
            outputs = self._session.run(
                [self._output_name, self._state_out_name],
                {
                    self._input_name: x,
                    self._state_name: self._state,
                    self._sr_name: sr_input,
                },
            )
            prob = float(outputs[0][0, 0])  # (batch=1, 1) -> scalar
            self._state = outputs[1]        # 更新 LSTM 状态
            # 保存当前帧末尾 context_size 个样本, 供下次推理用
            self._context = x[..., -self._context_size:]
        except Exception as e:
            logger.warning("VAD ONNX 推理异常: %s, 重置状态", e)
            self.reset()
            return None

        frame_samples = frame.shape[-1]
        self._current_sample += frame_samples

        # ---------- 状态机 (参照 silero-vad VADIterator) ----------
        event = None  # None / "start" / "end"

        if prob < self._threshold:
            if self._is_speech:
                # 进入静音段
                if self._temp_end == 0:
                    self._temp_end = self._current_sample
                # 检查静音是否足够长
                if self._current_sample - self._temp_end >= self._min_silence_samples:
                    # 语音段结束
                    self._is_speech = False
                    event = "end"
                    # 重置 LSTM 状态 (silero-vad 在语音结束时也会重置)
                    self._reset_state_tensor()
                    self._temp_end = 0
            # else: 持续静音, 无事件
        else:
            # 检测到语音
            if not self._is_speech:
                # 语音开始 (考虑 speech_pad, 把起点往前推)
                self._is_speech = True
                event = "start"
            self._temp_end = 0  # 重置静音计数

        # ---------- 累积音频 + 输出 utterance ----------
        if event == "start" and not self._in_speech:
            self._in_speech = True
            # recorder 传进来的是 AEC 后音频，所以这里缓存的也是
            # 后续要送给 ASR 的去回声音频。
            self._buffer = [frame.copy()]
        elif event == "end" and self._in_speech:
            utterance = (
                np.concatenate(self._buffer, axis=0)
                if self._buffer
                else np.zeros(0, dtype=np.float32)
            )
            self._buffer = []
            self._in_speech = False
            return utterance
        elif self._in_speech:
            # 正在说话, 累积当前帧
            self._buffer.append(frame.copy())

        return None

    def flush(self) -> Optional[np.ndarray]:
        """在停止采集时, 若仍有正在进行的语音段, 返回之。"""
        if self._in_speech and self._buffer:
            utterance = np.concatenate(self._buffer, axis=0)
            self._buffer = []
            self._in_speech = False
            return utterance
        return None

    @property
    def is_speaking(self) -> bool:
        return self._in_speech
