"""Qwen LLM 客户端 - 通过 OpenAI 兼容的 HTTP API 调用本地部署的 Qwen 服务。

适用于 vLLM (`vllm serve`)、Ollama、TGI、LocalAI 等提供 /v1/chat/completions 的服务。
使用 SSE 流式接收 token, yield 每个 delta.content 片段。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Iterator, Optional

import requests

from ..config import LlmCfg

logger = logging.getLogger(__name__)


class QwenLLM:
    """OpenAI 兼容的流式 LLM 客户端 (保持原 stream 接口)。"""

    def __init__(self, cfg: LlmCfg):
        self.cfg = cfg
        self.url = cfg.base_url.rstrip("/") + "/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        try:
            r = requests.get(
                cfg.base_url.rstrip("/") + "/models",
                headers=self.headers,
                timeout=3,
            )
            logger.info("LLM 服务探活: %s (status=%s)", self.url, r.status_code)
        except Exception as e:
            logger.warning("LLM 服务探活失败 (稍后重试): %s", e)

    def stream(
        self,
        user_text: str,
        history: Optional[list[dict]] = None,
    ) -> Iterator[str]:
        """流式生成, yield 每个 token 文本片段。"""
        messages: list[dict] = []
        if self.cfg.system_prompt.strip():
            messages.append({"role": "system", "content": self.cfg.system_prompt})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "temperature": self.cfg.temperature,
            "top_p": self.cfg.top_p,
            "max_tokens": self.cfg.max_tokens,
            # vLLM 扩展字段; 标准 OpenAI 服务会忽略
            "repetition_penalty": self.cfg.repetition_penalty,
        }

        t0 = time.time()
        first_token = True
        try:
            with requests.post(
                self.url,
                headers=self.headers,
                json=payload,
                stream=True,
                timeout=self.cfg.timeout,
            ) as resp:
                resp.raise_for_status()
                for raw in resp.iter_lines(decode_unicode=False):
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) or {}
                    token = delta.get("content")
                    if first_token and token:
                        logger.info("LLM 首 token 耗时 %.2fs", time.time() - t0)
                        first_token = False
                    if token:
                        yield token
        except Exception:
            logger.exception("LLM 流式请求失败: %s", self.url)
            return

        logger.info("LLM 生成完成, 总耗时 %.2fs", time.time() - t0)
