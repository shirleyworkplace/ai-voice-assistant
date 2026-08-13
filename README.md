# AI Voice Assistant

本地实时语音助手客户端（产品安装包名 **Ava**）。

本仓库只负责客户端：麦克风采集、Silero VAD、打断（barge-in）、浏览器 Voice Orb，以及通过 HTTP 调用远端的 ASR / LLM / TTS。模型服务需另行部署，本项目不包含其安装与运维。

**流程：** 麦克风 → Silero VAD → SenseVoice ASR → Qwen LLM（SSE 流式）→ 按句切分 → IndexTTS2 TTS → 播放

中文为主，也支持英文；支持外放场景下的回声抑制与用户打断。

## 架构

```
┌──────────────────────────┐         HTTP          ┌────────────────────────────┐
│  本项目 (客户端)          │  ── ASR (wav) ──────▶ │  SenseVoice 服务            │
│  · Silero VAD (本地 ONNX) │  ◀─ text ───────────  │                            │
│  · AEC / 回声门限         │                       │  Qwen LLM (OpenAI 兼容)    │
│  · Voice Orb (浏览器 UI)  │  ── chat (SSE) ─────▶ │                            │
│                          │  ◀─ token 流 ───────  │  IndexTTS2 TTS             │
│                          │  ── tts (multipart) ▶ │                            │
│                          │  ◀─ WAV ────────────  │                            │
└──────────────────────────┘                       └────────────────────────────┘
```

VAD 必须留在客户端（约 32ms 帧级判定，网络往返不现实）。ASR / LLM / TTS 走 HTTP API。

## 目录结构

```
ai-voice-assistant/
├── config.yaml                 # API 端点、VAD、AEC、对话参数
├── main.py                     # 入口
├── requirements.txt            # 客户端依赖
├── Ava.spec                    # PyInstaller 规格（可由构建脚本重新生成）
├── assets/
│   ├── ava.ico                # 安装包 / 应用图标
│   └── zero_shot_prompt.wav    # IndexTTS2 音色参考音频
├── models/
│   └── silero_vad.onnx         # 本地 VAD 模型
├── installer/
│   └── ai-voice-assistant.iss  # Inno Setup 脚本
├── scripts/
│   ├── download_vad_model.py   # 下载 Silero VAD ONNX
│   ├── build_windows.ps1       # 打包 dist\Ava
│   └── build_installer.ps1     # 生成 release\ava.exe
└── src/
    ├── config.py               # 配置加载
    ├── pipeline.py             # 流水线（线程 + 队列 + 打断 + 按句切分）
    ├── voice_orb.py            # 本地 Voice Orb HTTP/SSE 服务
    ├── voice_orb_static/       # Voice Orb 前端
    ├── audio/                  # 采集、播放、AEC
    ├── vad/silero_vad.py       # Silero VAD（ONNX Runtime）
    ├── asr/sensevoice_asr.py   # SenseVoice HTTP 客户端
    ├── llm/qwen_llm.py         # OpenAI 兼容 SSE 客户端
    └── tts/indextts2_tts.py    # IndexTTS2 HTTP 客户端（返回整段 WAV）
```

构建产物目录 `build/`、`dist/`、`build-staging/`、`dist-staging/` 已加入 `.gitignore`，不提交。

## 远端服务接口

服务可部署在本机或局域网其他机器。默认配置指向 `127.0.0.1`；请按实际地址修改 `config.yaml`。

### 1. SenseVoice ASR

```
POST {base_url}{endpoint}            # 默认 http://127.0.0.1:8001/v1/asr
Content-Type: multipart/form-data
  file:        wav（16kHz mono）
  language:    zh / en / auto
  use_itn:     true / false
响应: application/json
  {"text": "识别出的文本"}
```

可选 `GET {base_url}/health` 探活。

### 2. Qwen LLM（OpenAI 兼容）

```
POST {base_url}/chat/completions     # 默认 http://127.0.0.1:8000/v1/chat/completions
Authorization: Bearer <api_key>
Content-Type: application/json
  {
    "model": "Qwen3.5-2B",           # 须与 config.yaml 的 llm.model 一致
    "messages": [...],
    "stream": true,
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 128
  }
响应: text/event-stream（SSE）
```

兼容 vLLM / Ollama / TGI 等 OpenAI 兼容服务。Windows 上建议用 `127.0.0.1`，避免 `localhost` 优先解析 IPv6 导致连接卡住。

### 3. IndexTTS2 TTS

```
POST {base_url}{endpoint}            # 默认 http://127.0.0.1:8002/v1/tts
Content-Type: multipart/form-data
  voice:                      参考音色 wav（客户端上传 assets/zero_shot_prompt.wav）
  text:                       要合成的句子
  interval_silence:           段间静音（ms）
  max_text_tokens_per_segment 分段上限
响应: audio/wav（完整 WAV；客户端解码后按块送播放器）
```

可选 `GET {base_url}/health` 探活。`tts.sample_rate` 应与服务端输出采样率一致（当前默认 24000）。

## 环境要求

- Python 3.10 / 3.11（推荐 Conda 环境；本仓库构建脚本默认使用名为 `real-time-voice` 的环境）
- 能访问上述三个 HTTP 服务
- Windows 外放打断场景需要 `aec-audio-processing`

本地 VAD 使用 **ONNX Runtime**，不依赖 PyTorch。

## 安装

```powershell
# 激活你的 Python 环境后
pip install -r requirements.txt

# 若缺少 VAD 模型
python scripts/download_vad_model.py
```

确认 `models/silero_vad.onnx` 与 `assets/zero_shot_prompt.wav` 存在。

## 配置

编辑 `config.yaml`，至少改三个服务地址：

```yaml
asr:
  base_url: "http://127.0.0.1:8001"
  endpoint: "/v1/asr"
llm:
  base_url: "http://127.0.0.1:8000/v1"
  model: "Qwen3.5-2B"          # 必须与远端实际模型名一致
  api_key: "EMPTY"
tts:
  base_url: "http://127.0.0.1:8002"
  endpoint: "/v1/tts"
  prompt_audio: "assets/zero_shot_prompt.wav"
  sample_rate: 24000
```

## 运行

```powershell
python main.py
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--config PATH` | 指定配置文件（默认 `config.yaml`） |
| `--no-orb` | 不启动浏览器 Voice Orb |
| `--orb-port PORT` | Voice Orb 端口（默认 `8765`） |

启动后对着麦克风说话即可。VAD 判定说完后自动走识别 → 回答 → 播放；播放中再次开口可打断。浏览器会打开 Voice Orb，可开关麦克风或退出。

开发模式用 `Ctrl+C` 结束；打包版可通过 Voice Orb 退出。

## Windows 安装包

安装包包含客户端、Silero VAD 模型与 TTS 参考音频；ASR / LLM / TTS 仍通过 `config.yaml` 中的 HTTP API 访问。

```powershell
# 生成应用目录 dist\Ava
.\scripts\build_windows.ps1

# 再生成安装程序 release\ava.exe（需安装 Inno Setup 6）
.\scripts\build_installer.ps1
```

`dist\Ava` 中的 `config.yaml`、`models`、`assets` 与 `Ava.exe` 必须放在一起，不能只拷贝 exe。

升级安装会保留用户已修改的 `config.yaml`（`onlyifdoesntexist`），避免覆盖 API 地址等本地配置。

## 关键设计说明

- **按句切分送 TTS**：LLM 流式输出累积到句末标点，或达到长度上限时优先在标点 / 空格处切开，避免把英文单词从中间拆开。
- **IndexTTS2**：服务端返回整段 WAV；客户端解码后按约 0.2s 小块连续送入播放器。
- **ASR 不在音频回调线程**：VAD 切出的 utterance 只入队，HTTP 识别在独立线程，避免阻塞采集。
- **打断（barge-in）**：AEC / 回声门限过滤扬声器回声后，用户开口可停止播放、清空队列并中断 LLM。
- **思考链过滤**：自动剥离 Qwen 的 `<think>...</think>`，不送入 TTS。
- **对话历史**：保留最近 N 轮上下文。
- **Voice Orb**：本地 SSE 推送音量与状态，网页可开关采集并请求退出。

## 配置调优

| 项 | 说明 |
|---|---|
| `vad.threshold` | 语音概率门槛，越高越严 |
| `vad.min_silence_duration_ms` | 静音多久算说完；太小易切碎，太大延迟高 |
| `asr` / `llm` / `tts` 的 `base_url` | 三个远端服务地址 |
| `llm.model` | 必须与远端模型名完全一致 |
| `tts.prompt_audio` | IndexTTS2 音色参考 wav |
| `tts.sample_rate` | 需与 TTS 服务输出采样率一致 |
| `aec.enabled` | 外放是否启用 AEC |
| `aec.echo_gate*` | 回声门限，减轻 TTS 回声误触发打断 |
| `dialog.enable_barge_in` | 是否允许用户打断播放 |
| `dialog.sentence_max_len` | 强制切分前的最大缓冲长度（在标点/空格处切） |

## 常见问题

- **连接失败**：用 `curl http://<host>:<port>/health` 探活；检查防火墙与地址（优先 `127.0.0.1`）。
- **LLM model not found**：`llm.model` 与远端加载名不一致。
- **TTS 无声 / 报参考音频缺失**：确认 `assets/zero_shot_prompt.wav` 存在，且相对 `config.yaml` 路径可解析。
- **播放变调**：`tts.sample_rate` 与服务端 WAV 采样率不一致。
- **一句话被切碎**：增大 `vad.min_silence_duration_ms`（如 1200–1500）。
- **说完很久才响应**：减小 `vad.min_silence_duration_ms`（如 500–800）。
- **外放误打断**：调高 `dialog.barge_in_min_rms` / `barge_in_confirm_ms`，或微调 `aec.echo_gate_*`、`stream_delay_ms`。
- **英文单词被拆开送 TTS**：已按空格/标点切分；若仍异常，检查日志中的 `Assistant sentence:` 是否完整。
