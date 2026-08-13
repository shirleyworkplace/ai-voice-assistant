"""SenseVoice ASR 客户端 - 通过 HTTP API 调用本地部署的 SenseVoice 服务。

约定服务端接口 (见 server_examples/asr_server.py):
  POST {base_url}{endpoint}
    multipart/form-data:
      file: wav 音频 (16kHz mono)
      language: str
      use_itn: bool
    返回 JSON: {"text": "..."}
"""
from __future__ import annotations

import io
import logging
import time

import numpy as np
import requests
import soundfile as sf

from ..config import AsrCfg

logger = logging.getLogger(__name__)


class SenseVoiceASR:
    """调用远端 SenseVoice HTTP 服务的识别器 (保持原接口)。"""

    def __init__(self, cfg: AsrCfg):
        self.cfg = cfg
        self.url = cfg.base_url.rstrip("/") + cfg.endpoint
        # 启动时探活, 失败不致命 (服务可能稍后启动)
        try:
            r = requests.get(cfg.base_url.rstrip("/") + "/health", timeout=3)
            logger.info("ASR 服务探活: %s (status=%s)", self.url, r.status_code)
        except Exception as e:
            logger.warning("ASR 服务探活失败 (稍后重试): %s", e)

    def transcribe(self, audio: np.ndarray) -> str:
        """识别一段 16kHz float32 音频, 返回文本。"""
        if audio is None or audio.size == 0:
            return ""

        t0 = time.time()
        # 编码为内存中的 wav
        buf = io.BytesIO()
        sf.write(buf, audio, self.cfg_sample_rate(), subtype="FLOAT", format="WAV")
        buf.seek(0)

        files = {"file": ("utterance.wav", buf, "audio/wav")}
        data = {
            "language": self.cfg.language,
            "use_itn": "true" if self.cfg.use_itn else "false",
        }
        try:
            resp = requests.post(
                self.url,
                files=files,
                data=data,
                timeout=self.cfg.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            logger.exception("ASR 请求失败: %s", self.url)
            return ""

        text = (payload or {}).get("text", "").strip()
        logger.info("ASR 耗时 %.2fs, 文本: %s", time.time() - t0, text)
        return text

    def cfg_sample_rate(self) -> int:
        """ASR 输入采样率固定 16kHz (SenseVoice 要求)。"""
        return 16000
