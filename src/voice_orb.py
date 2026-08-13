"""Local browser voice orb driven by the Python audio pipeline."""
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
    """Serves the orb UI and streams microphone levels over SSE."""

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
        self._voice_enabled = True
        self.static_dir = Path(__file__).with_name("voice_orb_static")
        self.url = ""

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._clients: set[queue.Queue[dict[str, Any] | None]] = set()
        self._clients_lock = threading.Lock()
        self._stop = threading.Event()
        self._last_sent = 0.0
        self._last_payload: dict[str, Any] = {
            "amplitude": 0.0,
            "tone": 0.5,
            "speaking": False,
            "status": "LISTENING",
            "enabled": True,
        }

    def set_voice_enabled_handler(self, handler: Callable[[bool], bool]) -> None:
        self._on_voice_enabled = handler

    def set_shutdown_handler(self, handler: Callable[[], None]) -> None:
        self._on_shutdown = handler

    def request_shutdown(self) -> None:
        """由网页确认退出后通知主线程执行完整清理。"""
        if self._on_shutdown is not None:
            self._on_shutdown()

    def set_voice_enabled(self, enabled: bool) -> bool:
        """同步网页开关与实际的麦克风采集状态。"""
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
            threading.Timer(0.3, self.open_window).start()

    def open_window(self) -> None:
        """优先使用浏览器应用模式，避免复用用户已有窗口中的普通标签页。"""
        candidates = [
            shutil.which("msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            shutil.which("chrome.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
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
        """Publish a normalized audio level to the browser."""
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
        self._last_payload = payload
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            self._offer(client, payload)

    def _make_handler(self):
        static_dir = str(self.static_dir)

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=static_dir, **kwargs)

            def log_message(self, format: str, *args) -> None:
                logger.debug("Voice orb page: " + format, *args)

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
                server: VoiceOrbServer = self.server.voice_orb  # type: ignore[attr-defined]
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
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        if payload is None:
                            break
                        self._write_event("level", payload)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with server._clients_lock:
                        server._clients.discard(client)

            def _write_event(self, event_name: str, payload: dict[str, Any]) -> None:
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
