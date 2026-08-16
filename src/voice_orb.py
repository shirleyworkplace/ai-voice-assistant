"""本地 Voice Orb HTTP / SSE 服务。

本模块只负责「浏览器里的语音球」：
- 把 ``voice_orb_static/`` 当作静态站点提供出去（默认 ``http://127.0.0.1:8765/``）
- 用 SSE（``GET /events``）把麦克风音量推给页面，驱动球体缩放与内部流动
- 接收页面 ``POST /control``：开关采集，或请求退出整个客户端

它不跑 ASR / LLM / TTS。那些在 ``pipeline.py``。
页面改完后，开发模式直接 ``python main.py`` 即可；打包版必须重新跑构建脚本，
因为 exe 里的 ``index.html`` 是打包时拷进去的副本。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class VoiceOrbServer:
    """静态页 + SSE 音量流 + 控制接口。

    典型接线（见 ``pipeline.py``）：
    - ``on_voice_enabled``：网页点「暂停/恢复」时，真正打开/关闭麦克风
    - ``on_shutdown``：网页点退出后，主线程做完整清理并结束进程
    - ``update_level(amp, tone)``：采集线程每帧回调，这里节流到约 24Hz 再广播
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        open_browser: bool = True,
        on_voice_enabled: Optional[Callable[[bool], bool]] = None,
        on_shutdown: Optional[Callable[[], None]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.open_browser = open_browser
        self._on_voice_enabled = on_voice_enabled
        self._on_shutdown = on_shutdown
        # 与网页开关同步；False 时 update_level 强制发 0，球体收回
        self._voice_enabled = True
        # 静态目录与本文件同级：src/voice_orb_static/index.html
        self.static_dir = Path(__file__).with_name("voice_orb_static")
        self.url = ""

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # 每个 SSE 连接一条队列；payload 为 None 表示服务要停、断开该连接
        self._clients: set[queue.Queue[dict[str, Any] | None]] = set()
        self._clients_lock = threading.Lock()
        self._stop = threading.Event()
        # 节流：两次广播至少间隔 1/24 秒，避免把采集线程拖慢
        self._last_sent = 0.0
        # 新 SSE 客户端连上时立刻推这一份，避免第一帧空白
        self._last_payload: dict[str, Any] = {
            "amplitude": 0.0,
            "tone": 0.5,
            "speaking": False,
            "status": "LISTENING",
            "enabled": True,
        }

    def set_voice_enabled_handler(self, handler: Callable[[bool], bool]) -> None:
        """运行中再注入采集开关回调（pipeline 组装完后调用）。"""
        self._on_voice_enabled = handler

    def set_shutdown_handler(self, handler: Callable[[], None]) -> None:
        """运行中再注入退出回调。"""
        self._on_shutdown = handler

    def request_shutdown(self) -> None:
        """网页确认退出后通知主线程做完整清理。

        HTTP handler 里不能直接阻塞退出，所以用 Timer 稍后再调这个方法。
        """
        if self._on_shutdown is not None:
            self._on_shutdown()

    def set_voice_enabled(self, enabled: bool) -> bool:
        """同步网页开关与真实麦克风采集。

        handler 可以否决请求（例如设备打不开），返回值才是最终状态。
        无论成败都会广播一次，让所有已打开的页面状态一致。
        """
        active = bool(enabled)
        if self._on_voice_enabled is not None:
            try:
                active = bool(self._on_voice_enabled(active))
            except Exception:
                logger.exception("Voice enable handler failed")
                active = self._voice_enabled
        self._voice_enabled = active
        self._broadcast(
            {
                "amplitude": 0.0,
                "tone": 0.5,
                "speaking": False,
                "status": "LISTENING" if active else "PAUSED",
                "enabled": active,
            }
        )
        return active

    def start(self) -> None:
        """启动后台 HTTP 线程。端口被占用时从 ``port`` 起连试 20 个。"""
        if self._httpd is not None:
            return
        if not (self.static_dir / "index.html").is_file():
            logger.warning("Voice orb page is missing: %s", self.static_dir / "index.html")
            return

        handler = self._make_handler()
        last_error: OSError | None = None
        for candidate_port in range(self.port, self.port + 20):
            try:
                self._httpd = ThreadingHTTPServer((self.host, candidate_port), handler)
                self.port = candidate_port
                break
            except OSError as exc:
                last_error = exc
        if self._httpd is None:
            logger.warning("Voice orb server failed to start: %s", last_error)
            return

        # 让 Handler 能通过 self.server.voice_orb 回到本实例
        self._httpd.voice_orb = self  # type: ignore[attr-defined]
        self.url = f"http://{self.host}:{self.port}/"
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            daemon=True,
            name="voice-orb-ui",
        )
        self._thread.start()
        logger.info("Voice orb started: %s", self.url)

        if self.open_browser:
            # 稍等 HTTP 就绪再开窗口，减少首屏空白
            threading.Timer(0.3, self.open_window).start()

    def open_window(self) -> None:
        """优先用 Edge/Chrome 的 ``--app=`` 独立窗口，避免塞进用户已有标签页。"""
        candidates = [
            shutil.which("msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            shutil.which("chrome.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        # 打包成 windowed exe 时不要再弹出一个控制台
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for browser in candidates:
            if not browser or not os.path.isfile(browser):
                continue
            try:
                subprocess.Popen(
                    [browser, f"--app={self.url}", "--new-window"],
                    creationflags=creation_flags,
                )
                logger.info("Voice orb opened in app window: %s", browser)
                return
            except OSError:
                logger.debug("Failed to open voice orb with %s", browser, exc_info=True)
        logger.warning("No app-mode browser was found; opening voice orb in the default browser")
        webbrowser.open_new(self.url)

    def stop(self) -> None:
        """停 HTTP、断开所有 SSE、等后台线程退出。"""
        self._stop.set()
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            self._offer(client, None)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        logger.info("Voice orb stopped")

    def update_level(self, amplitude: float, tone: float = 0.5) -> None:
        """把归一化音量发给浏览器。

        ``amplitude`` / ``tone`` 来自 ``recorder._emit_audio_level``，范围约 [0, 1]。
        采集回调很密，这里压到约 24Hz；页面再用弹簧平滑，所以看起来不会一顿一顿。
        ``speaking`` 阈值 0.08 与页面旧逻辑对齐，只影响状态文案，不直接改球体半径。
        """
        now = time.monotonic()
        if now - self._last_sent < 1 / 24:
            return
        self._last_sent = now
        value = min(1.0, max(0.0, float(amplitude))) if self._voice_enabled else 0.0
        payload = {
            "amplitude": value,
            "tone": min(1.0, max(0.0, float(tone))),
            "speaking": self._voice_enabled and value > 0.08,
            "status": "SPEAKING" if value > 0.08 else ("LISTENING" if self._voice_enabled else "PAUSED"),
            "enabled": self._voice_enabled,
        }
        self._broadcast(payload)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        """记下最新快照，并投递给每个 SSE 客户端队列。"""
        self._last_payload = payload
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            self._offer(client, payload)

    def _make_handler(self):
        """生成绑定了静态目录的 HTTP Handler 类。

        路由：
        - ``GET /events``  SSE
        - ``POST /control``  JSON 控制
        - 其余路径走 ``SimpleHTTPRequestHandler`` 读 ``voice_orb_static``
        """
        static_dir = str(self.static_dir)

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=static_dir, **kwargs)

            def log_message(self, format: str, *args) -> None:
                # 默认会打到 stderr，开发时太吵，降到 debug
                logger.debug("Voice orb page: " + format, *args)

            def end_headers(self) -> None:
                # 改 html 后浏览器容易吃缓存，开发/升级都会看到旧界面
                path = self.path.split("?", 1)[0]
                if path in ("", "/") or path.endswith((".html", ".js", ".css")):
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                super().end_headers()

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] == "/events":
                    self._serve_events()
                    return
                super().do_GET()

            def do_POST(self) -> None:
                if self.path.split("?", 1)[0] != "/control":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if payload.get("action") == "shutdown":
                        # 先回 200，再异步退出，避免浏览器收不到响应
                        self._send_json(HTTPStatus.OK, {"stopping": True})
                        threading.Timer(0.1, self.server.voice_orb.request_shutdown).start()  # type: ignore[attr-defined]
                        return
                    enabled = payload.get("enabled")
                    if not isinstance(enabled, bool):
                        raise ValueError("enabled must be a boolean")
                    active = self.server.voice_orb.set_voice_enabled(enabled)  # type: ignore[attr-defined]
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid control payload"})
                    return
                self._send_json(HTTPStatus.OK, {"enabled": active})

            def _serve_events(self) -> None:
                """一条长连接：先发当前 state，再循环发 level；空闲 15s 写一行 SSE ping。"""
                server: VoiceOrbServer = self.server.voice_orb  # type: ignore[attr-defined]
                # maxsize=4：页面渲染跟不上就丢旧帧，只留最新音量
                client: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=4)
                with server._clients_lock:
                    server._clients.add(client)

                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self._write_event("state", server._last_payload)

                try:
                    while not server._stop.is_set():
                        try:
                            payload = client.get(timeout=15)
                        except queue.Empty:
                            # 注释行 ping，防止代理把空闲连接掐掉
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        if payload is None:
                            break
                        self._write_event("level", payload)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # 用户关窗口是正常路径
                    pass
                finally:
                    with server._clients_lock:
                        server._clients.discard(client)

            def _write_event(self, event_name: str, payload: dict[str, Any]) -> None:
                """按 SSE 规范写 ``event:`` + ``data:`` + 空行。"""
                message = (
                    f"event: {event_name}\n"
                    f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                )
                self.wfile.write(message.encode("utf-8"))
                self.wfile.flush()

            def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    @staticmethod
    def _offer(client: queue.Queue[dict[str, Any] | None], payload: dict[str, Any] | None) -> None:
        """非阻塞入队。队列满则丢掉最旧的一条再写入，保证页面拿到的是最新音量。"""
        try:
            client.put_nowait(payload)
        except queue.Full:
            try:
                client.get_nowait()
            except queue.Empty:
                pass
            try:
                client.put_nowait(payload)
            except queue.Full:
                pass
