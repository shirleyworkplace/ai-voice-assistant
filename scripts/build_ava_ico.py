"""Rebuild Ava logo and Windows icon with transparent background (no plate)."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RAW_CANDIDATES = [
    ROOT / "assets" / "ava-logo-ref-raw.png",
    ROOT / "assets" / "ava-logo-new-A.png",
    ROOT / "assets" / "ava-logo-concept-A.png",
    ROOT / "assets" / "ava-logo.png",
]
OUT_LOGO = ROOT / "assets" / "ava-logo.png"
OUT_ICO = ROOT / "assets" / "ava.ico"
OUT_DESKTOP_ICO = ROOT / "assets" / "AvaIcon.ico"
SIZES = (16, 32, 48, 64, 128, 256)


def chroma_key_green(img: Image.Image) -> Image.Image:
    arr = np.array(img.convert("RGBA")).astype(np.float32)
    r, g, b, a = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2], arr[:, :, 3]
    greenness = g - np.maximum(r, b)
    is_green = (g > 120) & (greenness > 35) & (g > r + 15) & (g > b + 15)
    alpha = np.where(is_green, np.clip(255 - (greenness - 15) * 5.0, 0, 255), a)
    strong = (g > 170) & (greenness > 70)
    alpha = np.where(strong, 0, alpha)
    spill = (greenness > 8) & (alpha > 0) & (alpha < 250)
    g2 = g.copy()
    g2[spill] = np.minimum(g[spill], np.maximum(r[spill], b[spill]) + 6)
    arr[:, :, 1] = g2
    arr[:, :, 3] = alpha
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def trim_alpha(img: Image.Image, pad_ratio: float = 0.02) -> Image.Image:
    alpha = np.array(img)[:, :, 3]
    ys, xs = np.where(alpha > 16)
    if not len(xs):
        return img
    pad = max(4, int(pad_ratio * max(img.size)))
    return img.crop(
        (
            max(0, xs.min() - pad),
            max(0, ys.min() - pad),
            min(img.size[0], xs.max() + pad + 1),
            min(img.size[1], ys.max() + pad + 1),
        )
    )


def strip_light_plate(img: Image.Image) -> Image.Image:
    arr = np.array(img.convert("RGBA")).astype(np.float32)
    r, g, b, a = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2], arr[:, :, 3]
    light = (r > 200) & (g > 210) & (b > 220) & (a > 200)
    colorful = ((b > r + 15) | (g > r + 10)) & (a > 16)
    plate = light & ~colorful
    arr[:, :, 3] = np.where(plate, 0, a)
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def load_mark() -> Image.Image:
    for path in RAW_CANDIDATES:
        if not path.is_file():
            continue
        img = Image.open(path).convert("RGBA")
        arr = np.array(img)
        corner = arr[0, 0]
        if corner[1] > 150 and corner[1] > corner[0] + 40 and corner[1] > corner[2] + 40:
            print(f"chroma-key from {path}")
            return trim_alpha(chroma_key_green(img))
        cleaned = strip_light_plate(img)
        a2 = np.array(cleaned).astype(np.float32)
        r, g, b, a = a2[:, :, 0], a2[:, :, 1], a2[:, :, 2], a2[:, :, 3]
        dark = (r < 25) & (g < 25) & (b < 25) & (a > 200)
        a2[:, :, 3] = np.where(dark, 0, a)
        cleaned = Image.fromarray(a2.astype(np.uint8), "RGBA")
        if float((np.array(cleaned)[:, :, 3] == 0).mean()) > 0.15:
            print(f"transparent source {path}")
            return trim_alpha(cleaned)
    raise SystemExit("No usable logo source found")


def make_app_icon(mark: Image.Image, size: int) -> Image.Image:
    """Fill as much of the square as possible so desktop icons stay recognizable."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fitted = mark.copy()
    # Use nearly the full canvas; wordmark needs max pixels on desktop.
    fitted.thumbnail((int(size * 0.96), int(size * 0.96)), Image.Resampling.LANCZOS)
    ox = (size - fitted.size[0]) // 2
    oy = (size - fitted.size[1]) // 2
    canvas.paste(fitted, (ox, oy), fitted)
    return canvas


def dib(img: Image.Image) -> bytes:
    w, h = img.size
    rgba = np.array(img.convert("RGBA"))
    xor = rgba[:, :, [2, 1, 0, 3]][::-1].tobytes()
    row_bytes = ((w + 31) // 32) * 4
    and_mask = bytes(row_bytes * h)
    header = struct.pack("<IIIHHIIIIII", 40, w, h * 2, 1, 32, 0, len(xor), 0, 0, 0, 0)
    return header + xor + and_mask


def write_ico(images: list[Image.Image], dest: Path) -> None:
    payloads = [dib(im) for im in images]
    count = len(images)
    offset = 6 + 16 * count
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    body = b""
    for im, payload in zip(images, payloads):
        size = im.size[0]
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(payload), offset)
        body += payload
        offset += len(payload)
    dest.write_bytes(header + entries + body)


def main() -> None:
    mark = load_mark()
    # Keep master logo square with modest padding for branding use.
    side = max(mark.size) + 48
    logo = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    logo.paste(mark, ((side - mark.size[0]) // 2, (side - mark.size[1]) // 2), mark)
    logo = logo.resize((1024, 1024), Image.Resampling.LANCZOS)
    logo.save(OUT_LOGO, "PNG")

    icons = [make_app_icon(mark, size) for size in SIZES]
    write_ico(icons, OUT_ICO)
    write_ico(icons, OUT_DESKTOP_ICO)
    print(f"wrote {OUT_LOGO}")
    print(f"wrote {OUT_ICO} ({OUT_ICO.stat().st_size} bytes)")
    print(f"wrote {OUT_DESKTOP_ICO}")
    alpha = np.array(logo)[:, :, 3]
    print(f"logo transparent ratio={float((alpha == 0).mean()):.3f}")


if __name__ == "__main__":
    main()
