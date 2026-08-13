"""Configuration loading for the voice pipeline."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class AudioCfg:
    sample_rate: int = 16000
    channels: int = 1
    block_size: int = 512
    dtype: str = "float32"


@dataclass
class VadCfg:
    threshold: float = 0.45
    min_silence_duration_ms: int = 1200
    speech_pad_ms: int = 300
    min_speech_duration_ms: int = 250
    model_repo: str = "snakers4/silero-vad"
    model_path: Optional[str] = None


@dataclass
class AsrCfg:
    base_url: str = "http://127.0.0.1:8001"
    endpoint: str = "/asr"
    language: str = "auto"
    use_itn: bool = True
    timeout: int = 30


@dataclass
class LlmCfg:
    base_url: str = "http://127.0.0.1:8002/v1"
    model: str = "Qwen3.5-4B"
    api_key: str = "EMPTY"
    max_tokens: int = 128
    temperature: float = 0.7
    top_p: float = 0.9
    repetition_penalty: float = 1.05
    timeout: int = 120
    system_prompt: str = ""


@dataclass
class TtsCfg:
    base_url: str = "http://127.0.0.1:8003"
    endpoint: str = "/v1/tts"
    prompt_text: str = ""
    prompt_audio: str = ""
    sample_rate: int = 24000
    speed: float = 1.0
    timeout: int = 60
    api_key: str = ""
    interval_silence: int = 200
    max_text_tokens_per_segment: int = 120


@dataclass
class AecCfg:
    enabled: bool = False
    stream_delay_ms: int = 50
    frame_ms: int = 10
    enable_ns: bool = True
    enable_agc: bool = False
    enable_vad: bool = False
    echo_gate: bool = True
    echo_gate_ratio: float = 0.70
    echo_gate_correlation: float = 0.55
    echo_gate_search_ms: int = 350
    playback_hold_ms: int = 250


@dataclass
class DialogCfg:
    max_history: int = 6
    enable_barge_in: bool = True
    echo_suppression: bool = True
    barge_in_confirm_ms: int = 180
    barge_in_min_rms: float = 0.003
    sentence_min_len: int = 4
    sentence_max_len: int = 30


@dataclass
class LogCfg:
    level: str = "INFO"
    show_latency: bool = True


@dataclass
class Config:
    audio: AudioCfg = field(default_factory=AudioCfg)
    vad: VadCfg = field(default_factory=VadCfg)
    asr: AsrCfg = field(default_factory=AsrCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    tts: TtsCfg = field(default_factory=TtsCfg)
    aec: AecCfg = field(default_factory=AecCfg)
    dialog: DialogCfg = field(default_factory=DialogCfg)
    log: LogCfg = field(default_factory=LogCfg)

    @classmethod
    def from_yaml(cls, path: str | os.PathLike) -> "Config":
        config_path = Path(path).resolve()
        with config_path.open("r", encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
        cfg = cls()
        if "audio" in raw:
            cfg.audio = AudioCfg(**raw["audio"])
        if "vad" in raw:
            cfg.vad = VadCfg(**raw["vad"])
        if "asr" in raw:
            cfg.asr = AsrCfg(**raw["asr"])
        if "llm" in raw:
            cfg.llm = LlmCfg(**raw["llm"])
        if "tts" in raw:
            cfg.tts = TtsCfg(**raw["tts"])
        if "aec" in raw:
            cfg.aec = AecCfg(**raw["aec"])
        if "dialog" in raw:
            cfg.dialog = DialogCfg(**raw["dialog"])
        if "log" in raw:
            cfg.log = LogCfg(**raw["log"])

        # 安装版从 exe 同级的 config.yaml 启动；将相对资源路径固定到配置文件
        # 所在目录，避免快捷方式的工作目录不同导致模型或参考音频找不到。
        def resolve_resource(value: Optional[str]) -> Optional[str]:
            if not value:
                return value
            resource = Path(value).expanduser()
            return str(resource if resource.is_absolute() else config_path.parent / resource)

        cfg.vad.model_path = resolve_resource(cfg.vad.model_path)
        cfg.tts.prompt_audio = resolve_resource(cfg.tts.prompt_audio)
        return cfg


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config.yaml from the project root by default."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    return Config.from_yaml(path)
