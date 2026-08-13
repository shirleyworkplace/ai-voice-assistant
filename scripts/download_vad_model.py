"""下载 Silero VAD ONNX 模型到 models/ 目录。

用法:
    python scripts/download_vad_model.py

模型来源: snakers4/silero-vad 官方 release
"""
from __future__ import annotations

import os
import sys
import urllib.request

# Silero VAD ONNX 模型下载地址 (GitHub Release)
MODEL_URL = (
    "https://github.com/snakers4/silero-vad"
    "/raw/refs/heads/master/files/silero_vad.onnx"
)
MODEL_FILENAME = "silero_vad.onnx"


def main() -> int:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    models_dir = os.path.join(project_root, "models")
    os.makedirs(models_dir, exist_ok=True)
    dest = os.path.join(models_dir, MODEL_FILENAME)

    if os.path.isfile(dest):
        size_mb = os.path.getsize(dest) / 1024 / 1024
        print(f"已存在: {dest} ({size_mb:.1f} MB), 跳过下载")
        print("如需重新下载, 请先删除该文件。")
        return 0

    print(f"下载 Silero VAD ONNX 模型...")
    print(f"  来源: {MODEL_URL}")
    print(f"  目标: {dest}")
    try:
        urllib.request.urlretrieve(MODEL_URL, dest)
    except Exception as e:
        print(f"[错误] 下载失败: {e}")
        print("请手动下载:")
        print(f"  1. 访问 https://github.com/snakers4/silero-vad")
        print(f"  2. 下载 silero_vad.onnx")
        print(f"  3. 放到 {models_dir}")
        return 1

    size_mb = os.path.getsize(dest) / 1024 / 1024
    print(f"下载完成: {dest} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
