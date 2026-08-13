# AI Voice Assistant (AI 语音助手)

纯客户端项目: 麦克风采集 + Silero VAD 端点检测, 通过 HTTP API 调用
部署在**另一台服务器**上的三个模型服务 (SenseVoice / Qwen LLM / CosyVoice)。
本项目不包含、也不负责这三个模型的部署。

流程: **麦克风采集 → Silero VAD 端点检测 → SenseVoice API (ASR) → Qwen API (流式 LLM)
→ 按句切分 → CosyVoice API (流式 TTS) → 流式播放**。中文为主, 支持打断 (barge-in)。

## 架构

```
┌─────────────────────┐        HTTP         ┌──────────────────────────┐
│  本项目 (客户端)     │  ─── ASR (wav) ───▶ │  SenseVoice 服务 (远端)   │
│  含 Silero VAD       │  ◀── text ────────  │                          │
│                     │                     │  Qwen LLM 服务 (远端)     │  ← 部署在另一台服务器
│                     │  ─── chat (SSE) ─▶  │  (OpenAI 兼容)            │     (本项目不维护)
│                     │  ◀── token 流 ────  │                          │
│                     │                     │  CosyVoice 服务 (远端)    │
│                     │  ─── tts (PCM) ──▶  │  (流式 PCM)               │
│                     │  ◀── PCM 流 ──────  │                          │
└─────────────────────┘                     └──────────────────────────┘
```

VAD 留在客户端本地: 它需要对 32ms 级别的麦克风帧做实时判定, 走网络往返不现实。
三个模型走 API: 重模型在远端服务器部署, 本项目只调接口。

## 目录结构

```
ai-voice-assistant/
├── config.yaml              # API 端点 + VAD + 对话参数
├── main.py                  # 入口
├── requirements.txt         # 客户端依赖 (轻量)
└── src/
    ├── config.py            # 配置加载 (dataclass)
    ├── audio/
    │   ├── recorder.py      # 麦克风采集 + VAD 回调 (回调线程只入队)
    │   └── player.py        # 流式播放 (可打断)
    ├── vad/silero_vad.py    # Silero VAD 流式端点检测 (本地)
    ├── asr/sensevoice_asr.py# SenseVoice HTTP 客户端 (POST wav -> text)
    ├── llm/qwen_llm.py      # OpenAI 兼容 SSE 流式客户端
    ├── tts/cosyvoice_tts.py # CosyVoice HTTP 客户端 (流式 PCM)
    └── pipeline.py          # 流水线编排 (线程+队列+打断+历史)
```

## 远端服务需满足的接口契约

三个服务部署在另一台服务器上 (由你或模型部署方维护), 本项目按以下约定调用。
只要远端服务实现这些接口即可, 实现技术不限。

### 1. SenseVoice ASR

```
POST {base_url}{endpoint}            # 默认 http://<host>:8001/asr
Content-Type: multipart/form-data
  file:        wav 音频文件 (16kHz mono)
  language:    str  ("zh" / "en" / "auto")
  use_itn:     str  ("true" / "false")
响应: application/json
  {"text": "识别出的文本"}
```

可选 `GET {base_url}/health` 用于探活。

### 2. Qwen LLM (OpenAI 兼容)

```
POST {base_url}/chat/completions     # 默认 http://<host>:8002/v1/chat/completions
Authorization: Bearer <api_key>
Content-Type: application/json
  {
    "model": "Qwen3.5-4B",           # 与 config.yaml 的 llm.model 一致
    "messages": [{"role":"system","content":"..."},{"role":"user","content":"..."}],
    "stream": true,
    "temperature": 0.7, "top_p": 0.9, "max_tokens": 512
  }
响应: SSE 流 (text/event-stream)
  data: {"choices":[{"delta":{"content":"片"}}]}
  data: {"choices":[{"delta":{"content":"段"}}]}
  ...
  data: [DONE]
```

兼容 vLLM / Ollama / TGI / LocalAI 等任何 OpenAI 兼容服务。
可选 `GET {base_url}/models` 用于探活。

### 3. CosyVoice TTS (流式 PCM)

```
POST {base_url}{endpoint}            # 默认 http://<host>:8003/tts/stream
Content-Type: application/json
  {"text": "要合成的句子", "spk": "中文女", "speed": 1.0}
响应: application/octet-stream  (chunked transfer, 边合成边发送)
  响应头:
    X-Sample-Rate: 22050
    X-Dtype: float32        # 或 int16
    X-Channels: 1
  响应体: 原始 PCM 字节流 (按 X-Dtype 编码), 客户端按 4/2 字节对齐读取并即时播放
```

可选 `GET {base_url}/health` 用于探活。

## 环境要求

客户端机器: Python 3.10/3.11, 任意 (CPU 即可, 只要能跑 Silero VAD; 有 GPU 更省 CPU)。
需能网络访问三个远端服务的端口。

## 安装

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows PowerShell
# source .venv/bin/activate    # Linux/Mac

# PyTorch (VAD 需要, 按 CUDA 版本选; CPU 版也行)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

pip install -r requirements.txt
pip install aec-audio-processing
```

## 配置

编辑 `config.yaml`, 把三个 `base_url` 指向远端服务器地址:

```yaml
asr:
  base_url: "http://10.0.0.10:8001"
  endpoint: "/asr"
llm:
  base_url: "http://10.0.0.10:8002/v1"
  model: "Qwen3.5-4B"          # 必须与远端服务加载的模型名一致
  api_key: "EMPTY"             # 远端要求鉴权时填写
tts:
  base_url: "http://10.0.0.10:8003"
  endpoint: "/tts/stream"
  sample_rate: 22050           # 需与远端 TTS 输出采样率一致 (用于本地播放)
```

## 运行

```bash
python main.py
```

启动后直接说话。VAD 检测到说完一句后自动进入 识别→回答→播放;
说话时若系统正在播放, 会自动打断。

## Windows 安装包

安装包只包含语音客户端、Silero VAD 模型和 TTS 参考音频；ASR、LLM、TTS
仍通过 `config.yaml` 中配置的 HTTP API 调用。安装前确认目标用户能访问这些服务。

在 Conda `dev` 环境安装 PyInstaller 后，可生成可分发的应用目录：

```powershell
.\scripts\build_windows.ps1
```

输出目录为 `dist\AiVoiceAssistant`。其中的 `config.yaml`、`models`、`assets` 都是
运行必需文件，不能单独移动 `AiVoiceAssistant.exe`。

安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php) 后，执行：

```powershell
.\scripts\build_installer.ps1
```

安装程序输出到 `release\joya.exe`。升级安装会保留用户已
修改的 `config.yaml`，因此 API 地址和音频参数不会被覆盖。

## 关键设计说明

- **流式按句切分**: LLM 流式 token 累积到句子结束符 (`。！？!?\n`) 或超长时立即送 TTS, 不等整段生成完, 显著降低首句响应延迟。
- **TTS 流式 PCM**: 远端 chunked 发送, 客户端按 dtype 字节对齐读取并即时播放, 首包延迟低。
- **ASR 不在音频回调线程**: VAD 切出的 utterance 只入队, ASR HTTP 请求在独立工作线程, 避免阻塞麦克风采集导致爆音。
- **打断 (barge-in)**: AEC 去回声后 VAD 检测到用户开口 → 停止播放 + 清空待播队列 + 中断 LLM 生成流。
- **思考链过滤**: 自动剥离 Qwen3 的 `<think>...</think>`, 不送 TTS。
- **对话历史**: 保留最近 N 轮注入 chat 请求, 支持多轮上下文。

## 配置调优 (`config.yaml`)

| 项 | 说明 |
|---|---|
| `vad.threshold` | VAD 语音概率门槛, 越高越严 |
| `vad.min_silence_duration_ms` | 静音多久判定说完; 太小一句话被切碎, 太大延迟高 |
| `asr.base_url` / `llm.base_url` / `tts.base_url` | 三个远端服务地址 |
| `llm.model` | 必须与远端 LLM 服务加载的模型名一致 |
| `tts.spk` | CosyVoice 音色 (透传给远端) |
| `tts.sample_rate` | 需与远端 TTS 输出采样率一致 |
| `aec.enabled` | 外放场景是否启用 AEC 去回声 |
| `dialog.enable_barge_in` | 用户开口是否打断播放 |
| `dialog.echo_suppression` | 传统静音抑制；启用 AEC 时应关闭 |

## 常见问题

- **连接失败**: `curl <base_url>/health` 确认远端服务可达; 检查网络/防火墙/端口。
- **LLM 报 model not found**: `config.yaml` 的 `llm.model` 要和远端服务暴露的模型名完全一致。
- **TTS 播放变调/速度异常**: 确认 `tts.sample_rate` 与远端响应头 `X-Sample-Rate` 一致。
- **一句话被切碎**: 调大 `vad.min_silence_duration_ms` (如 1200–1500)。
- **说完后迟迟不响应**: 调小 `vad.min_silence_duration_ms` (如 500)。
- **远端 TTS 不是流式 (一次性返回整段)**: 仍可工作, 只是首包延迟变高; 确保响应头带 `X-Sample-Rate/X-Dtype` 即可。
