"""Build a Windows-compatible 32bpp alpha ICO (AND mask all zeros)."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets" / "ava-logo.png"
OUT_ICO = ROOT / "assets" / "ava.ico"
SIZES = (16, 32, 48, 64, 128, 256)


def _dib(img: Image.Image) -> bytes:
    w, h = img.size
    rgba = np.array(img.convert("RGBA"))
    # Straight alpha, rows bottom-up, BGRA.
    bgra = rgba[:, :, [2, 1, 0, 3]][::-1].copy()
    xor = bgra.tobytes()
    # For 32bpp icons Windows uses alpha; AND mask must be present and zeroed.
    row_bytes = ((w + 31) // 32) * 4
    and_mask = bytes(row_bytes * h)
    header = struct.pack(
        "<IIIHHIIIIII",
        40,
        w,
        h * 2,
        1,
        32,
        0,
        len(xor),
        0,
        0,
        0,
        0,
    )
    return header + xor + and_mask


def build_ico(src: Path, dest: Path) -> None:
    master = Image.open(src).convert("RGBA")
    alpha = np.array(master)[:, :, 3]
    ys, xs = np.where(alpha > 16)
    if len(xs):
        pad = max(8, int(0.04 * max(master.size)))
        master = master.crop(
            (
                max(0, xs.min() - pad),
                max(0, ys.min() - pad),
                min(master.size[0], xs.max() + pad + 1),
                min(master.size[1], ys.max() + pad + 1),
            )
        )

    images: list[tuple[int, bytes]] = []
    for size in SIZES:
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        fitted = master.copy()
        fitted.thumbnail((int(size * 0.90), int(size * 0.90)), Image.Resampling.LANCZOS)
        ox = (size - fitted.size[0]) // 2
        oy = (size - fitted.size[1]) // 2
        canvas.paste(fitted, (ox, oy), fitted)
        images.append((size, _dib(canvas)))

    count = len(images)
    offset = 6 + 16 * count
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    payloads = b""
    for size, dib in images:
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(dib), offset)
        payloads += dib
        offset += len(dib)

    dest.write_bytes(header + entries + payloads)
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    build_ico(SRC, OUT_ICO)
