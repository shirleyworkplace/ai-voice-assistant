"""AI 语音助手 - 入口。

流程: 麦克风采集 -> Silero VAD 端点检测 -> SenseVoice ASR
     -> Qwen LLM 流式输出 -> 按句切分 -> CosyVoice TTS -> 流式播放

用法:
    python main.py
    python main.py --config config.yaml
    python main.py --cosyvoice-repo D:/path/to/CosyVoice
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import traceback

from src.config import load_config
from src.pipeline import ConversationPipeline
from src.voice_orb import VoiceOrbServer


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # 抑制部分库的过度日志
    for noisy in ("matplotlib", "PIL", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _pause_before_exit() -> None:
    """打包成 exe 后, 双击运行出错会闪退看不到错误。
    这个函数让窗口在出错时暂停, 等用户按键后再关闭。
    """
    # 只有打包成 exe 时才暂停 (开发时直接 python main.py 不需要)
    if getattr(sys, "frozen", False):
        print("\n" + "=" * 60)
        print("程序异常退出。按回车键关闭窗口...")
        print("=" * 60)
        try:
            input()
        except Exception:
            pass


def _write_startup_error() -> None:
    """窗口模式没有控制台时，将启动异常保留到 exe 同级日志文件。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        log_dir = os.path.join(os.path.dirname(sys.executable), "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "startup-error.log"), "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
            f.write("\n")
    except Exception:
        pass


def main() -> int:
    try:
        parser = argparse.ArgumentParser(description="AI 语音助手 (本地全离线)")
        parser.add_argument("--config", default="config.yaml", help="配置文件路径")
        parser.add_argument(
            "--cosyvoice-repo",
            default=None,
            help="CosyVoice 源码目录 (若未加入 PYTHONPATH)",
        )
        parser.add_argument(
            "--no-orb",
            action="store_true",
            help="Do not open the browser voice orb",
        )
        parser.add_argument(
            "--orb-port",
            type=int,
            default=8765,
            help="Local port for the browser voice orb",
        )
        args = parser.parse_args()

        # 打包后, config.yaml 在 exe 同级目录; 开发时在项目根目录
        config_path = args.config
        if getattr(sys, "frozen", False):
            # PyInstaller 打包后, exe 所在目录
            exe_dir = os.path.dirname(sys.executable)
            config_path = os.path.join(exe_dir, args.config)
            if not os.path.isfile(config_path):
                # 兜底: 尝试 _internal 目录
                config_path = os.path.join(exe_dir, "_internal", args.config)

        config = load_config(config_path)
        setup_logging(config.log.level)

        stop_evt = threading.Event()
        voice_orb = None
        if not args.no_orb:
            voice_orb = VoiceOrbServer(port=args.orb_port)

        pipeline = ConversationPipeline(
            config=config,
            cosyvoice_repo_path=args.cosyvoice_repo,
            on_audio_level=voice_orb.update_level if voice_orb else None,
        )
        if voice_orb is not None:
            voice_orb.set_voice_enabled_handler(pipeline.set_voice_enabled)
            voice_orb.set_shutdown_handler(stop_evt.set)
            voice_orb.start()

        pipeline.start()
        logger = logging.getLogger(__name__)
        logger.info("=== 对话流水线已启动 ===")

        # 窗口版运行时由外部进程终止；开发模式可通过 Ctrl+C 退出。
        try:
            stop_evt.wait()
        except KeyboardInterrupt:
            pass
        finally:
            pipeline.stop()
            if voice_orb is not None:
                voice_orb.stop()
        return 0
    except SystemExit:
        # argparse 解析失败等, 不算异常
        raise
    except Exception:
        _write_startup_error()
        print("\n" + "=" * 60)
        print("程序发生错误:")
        print("=" * 60)
        traceback.print_exc()
        print("=" * 60)
        _pause_before_exit()
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        _write_startup_error()
        traceback.print_exc()
        _pause_before_exit()
        sys.exit(1)
