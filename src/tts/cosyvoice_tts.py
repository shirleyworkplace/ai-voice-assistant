"""CosyVoice TTS 客户端 - 通过 HTTP API 调用本地部署的 CosyVoice 服务。

约定服务端接口 (zero-shot 声音克隆):
  POST {base_url}{endpoint}
    multipart/form-data:
      tts_text:    要合成的文本
      prompt_text: 参考音频对应的文本
      prompt_audio: 参考音频文件 (wav)
      speed:       语速 (float, 字符串形式)
    响应:
      Content-Type: application/octet-stream
      响应头: X-Sample-Rate, X-Dtype (float32/int16), X-Channels
      body: chunked 原始 PCM 字节流, 边生成边发送

客户端按 dtype 对齐读取, yield float32 1D numpy 数组,
供 AudioPlayer 流式播放, 实现低延迟首包。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Iterator, Optional

import numpy as np
import requests

from ..config import TtsCfg

logger = logging.getLogger(__name__)

# dtype 字符串 <-> numpy dtype 映射
# 注意: CosyVoice 服务端 (app/model_manager.py:model_output_to_pcm_bytes)
# 固定输出 int16 PCM (2 字节/样本), 且响应头不包含 X-Dtype,
# 因此默认值必须是 int16, 否则字节错位会导致严重杂音。
_DTYPE_MAP = {
    "int16": (np.int16, 2),
    "float32": (np.float32, 4),
    "int32": (np.int32, 4),
}
_DEFAULT_DTYPE = "int16"


class CosyVoiceTTS:
    """调用远端 CosyVoice zero-shot HTTP 服务的合成器 (保持原 synthesize 接口)。"""

    def __init__(self, cfg: TtsCfg, cosyvoice_repo_path: Optional[str] = None):
        self.cfg = cfg
        self.url = cfg.base_url.rstrip("/") + cfg.endpoint
        self._prompt_audio_path = cfg.prompt_audio
        self._prompt_text = cfg.prompt_text
        self._server_sample_rate: Optional[int] = None

        # 启动时校验参考音频文件存在
        if not self._prompt_audio_path:
            logger.warning("TTS 未配置 prompt_audio, zero-shot 合成将失败")
        elif not os.path.isfile(self._prompt_audio_path):
            logger.warning(
                "TTS prompt_audio 文件不存在: %s", self._prompt_audio_path
            )
        if not self._prompt_text:
            logger.warning("TTS 未配置 prompt_text, zero-shot 合成效果可能不佳")

        # 探活
        try:
            r = requests.get(cfg.base_url.rstrip("/") + "/health", timeout=3)
            logger.info("TTS 服务探活: %s (status=%s)", self.url, r.status_code)
        except Exception as e:
            logger.warning("TTS 服务探活失败 (稍后重试): %s", e)

    def synthesize(
        self,
        text: str,
        speed: Optional[float] = None,
    ) -> Iterator[np.ndarray]:
        """流式合成一句文本, yield float32 1D 音频块。"""
        text = (text or "").strip()
        if not text:
            return
        speed = speed if speed is not None else self.cfg.speed

        if not self._prompt_audio_path or not os.path.isfile(self._prompt_audio_path):
            logger.error("TTS 参考音频缺失, 跳过合成: %s", self._prompt_audio_path)
            return

        # multipart/form-data 字段
        files = {
            "prompt_audio": (
                os.path.basename(self._prompt_audio_path),
                open(self._prompt_audio_path, "rb"),
                "audio/wav",
            ),
        }
        data = {
            "tts_text": text,
            "prompt_text": self._prompt_text,
            "speed": str(speed),
            "stream": "true",
        }

        t0 = time.time()
        first = True
        resp = None
        try:
            resp = requests.post(
                self.url,
                files=files,
                data=data,
                stream=True,
                timeout=self.cfg.timeout,
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("TTS 请求失败: %s", self.url)
            self._close_files(files)
            return

        # 从响应头获取 PCM 格式
        # CosyVoice 服务端只发 X-Sample-Rate, 不发 X-Dtype,
        # PCM 固定为 int16, 因此默认 int16。
        dtype_str = resp.headers.get("X-Dtype", _DEFAULT_DTYPE).lower()
        np_dtype, sample_width = _DTYPE_MAP.get(dtype_str, _DTYPE_MAP[_DEFAULT_DTYPE])

        # 从响应头获取实际采样率, 用于校验/动态调整播放器
        server_sr = resp.headers.get("X-Sample-Rate")
        if server_sr:
            try:
                server_sr_int = int(server_sr)
                if server_sr_int != self.cfg.sample_rate:
                    logger.warning(
                        "TTS 服务端采样率 %d 与配置 %d 不一致, 可能导致播放变调",
                        server_sr_int, self.cfg.sample_rate,
                    )
                self._server_sample_rate = server_sr_int
            except ValueError:
                pass

        leftover = b""
        stream_ok = True
        try:
            for chunk in resp.iter_content(chunk_size=32768):
                if not chunk:
                    continue
                data_bytes = leftover + chunk
                n = (len(data_bytes) // sample_width) * sample_width
                usable, leftover = data_bytes[:n], data_bytes[n:]
                if not usable:
                    continue
                arr = np.frombuffer(usable, dtype=np_dtype)
                # 统一转 float32 供播放器使用
                if np_dtype != np.float32:
                    arr = arr.astype(np.float32)
                    if np_dtype == np.int16:
                        arr /= 32768.0
                if first:
                    logger.info(
                        "TTS 首包耗时 %.2fs (文本: %s)",
                        time.time() - t0,
                        text[:30],
                    )
                    first = False
                if arr.size > 0:
                    yield arr
        except requests.exceptions.ChunkedEncodingError as e:
            # 服务端 chunked 流未正常结束 (生成器异常 / 连接被切断)。
            # 已收到的音频仍然有效, 当作流结束处理, 不当 ERROR。
            logger.warning("TTS 流提前结束 (已收音频仍有效): %s", e)
            stream_ok = False
        except Exception:
            logger.exception("TTS 流式读取失败")
            stream_ok = False
        finally:
            # 处理尾部不足一个样本的 leftover (补 0 对齐后 yield, 避免丢失末尾音频)
            if stream_ok and leftover:
                pad = sample_width - (len(leftover) % sample_width)
                if pad < sample_width:
                    leftover = leftover + b"\x00" * pad
                arr = np.frombuffer(leftover, dtype=np_dtype)
                if np_dtype != np.float32:
                    arr = arr.astype(np.float32)
                    if np_dtype == np.int16:
                        arr /= 32768.0
                if arr.size > 0:
                    yield arr
            if resp is not None:
                resp.close()
            self._close_files(files)

        if first:
            logger.warning("TTS 未产生音频 (文本: %s)", text[:50])

    @staticmethod
    def _close_files(files: dict) -> None:
        """关闭打开的文件句柄, 避免资源泄漏。"""
        for _, item in files.items():
            file_obj = item[1] if isinstance(item, tuple) else item
            try:
                file_obj.close()
            except Exception:
                pass
