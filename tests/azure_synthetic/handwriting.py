"""Block-capital 'handwriting' from EMNIST samples, plus signature scribbles (synthetic test data, laptop only).

EMNIST Balanced (NIST Special Database 19 derivative; research use) is read from _downloads/emnist (download.py).  Per
character class the samples closest to the class mean are kept as a pool of clear, typical letters; a "writer" (seeded)
picks from that pool, so one form looks written by one person.  `illegible=True` uses the LEAST typical samples, squeezed
and overlapping with a strike-through, for the illegible-key trap.  Characters EMNIST lacks (. , / - @ ' :) are drawn
as pen strokes.  Everything is deterministic for a given seed.  Needs numpy + pillow (synthetic venv).
"""
from __future__ import annotations

import gzip
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

EMNIST_DIR = Path(__file__).resolve().parent / "_downloads" / "emnist"
POOL = 12                 # clear samples kept per class
_CACHE: dict = {}


def _idx(path: Path) -> np.ndarray:
    with gzip.open(path, "rb") as fh:
        data = fh.read()
    dims = data[3]
    shape = tuple(int.from_bytes(data[4 + 4 * i:8 + 4 * i], "big") for i in range(dims))
    return np.frombuffer(data, np.uint8, offset=4 + 4 * dims).reshape(shape)


def available() -> bool:
    return all((EMNIST_DIR / n).is_file() for n in ("emnist-balanced-mapping.txt", "emnist-balanced-test-images-idx3-ubyte.gz",
                                                    "emnist-balanced-test-labels-idx1-ubyte.gz"))


def _ink_box(x):
    ys, xs = np.nonzero(x > 0.3)
    return (ys.min(), ys.max(), xs.min(), xs.max()) if len(xs) else (0, 27, 0, 27)


def _l_shape(x):
    """A capital L: wide enough, with a foot running along most of the bottom of its ink box."""
    y0, y1, x0, x1 = _ink_box(x)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    if w < 0.45 * h:
        return 0.0
    foot = (x[max(y0, y1 - 3):y1 + 1, x0:x1 + 1] > 0.4).any(axis=0)
    stem = (x[y0:y1 + 1, x0:x0 + max(2, w // 3)] > 0.4).any(axis=1)
    return float(foot.mean() > 0.7) * float(stem.mean() > 0.7) * float(foot.mean())


def _wide(x):
    """Capital forms of merged classes sit tall and wide in the box (lowercase ones are short)."""
    y0, y1, x0, x1 = _ink_box(x)
    return float((y1 - y0) >= 16) * min(1.0, (x1 - x0 + 1) / 16)


CAPITAL_TEST = {"L": _l_shape, "M": _wide, "Y": _wide, "J": _wide, "K": _wide, "P": _wide, "W": _wide}


def pools() -> dict:
    """{char: (clear samples [n,28,28] float 0..1, least typical samples)} for 0-9 and A-Z."""
    if "pools" in _CACHE:
        return _CACHE["pools"]
    images = _idx(EMNIST_DIR / "emnist-balanced-test-images-idx3-ubyte.gz").transpose(0, 2, 1).astype(np.float32) / 255.0
    labels = _idx(EMNIST_DIR / "emnist-balanced-test-labels-idx1-ubyte.gz")
    mapping = {}
    for line in (EMNIST_DIR / "emnist-balanced-mapping.txt").read_text().split("\n"):
        if line.strip():
            a, b = line.split()
            mapping[int(a)] = chr(int(b))
    out = {}
    for lab, ch in sorted(mapping.items()):
        if not (ch.isdigit() or "A" <= ch <= "Z"):
            continue
        idx = np.flatnonzero(labels == lab)
        imgs = images[idx]
        mean = imgs.mean(axis=0).ravel()
        flat = imgs.reshape(len(imgs), -1)
        score = (flat - flat.mean(1, keepdims=True)) @ (mean - mean.mean())
        score /= (np.linalg.norm(flat - flat.mean(1, keepdims=True), axis=1) * np.linalg.norm(mean - mean.mean()) + 1e-6)
        capital = CAPITAL_TEST.get(ch)
        order = np.argsort(-score, kind="stable")
        clear = order[:POOL]
        if capital is not None:          # a class EMNIST Balanced merges with its lowercase: keep capital-shaped ones
            shaped = np.array([capital(x) for x in imgs])
            good = [i for i in order if shaped[i] > 0.5]
            clear = np.array(good[:POOL]) if len(good) >= 4 else order[np.argsort(-shaped[order], kind="stable")][:POOL]
        out[ch] = (imgs[clear], imgs[order[-POOL:]])
    _CACHE["pools"] = out
    return out


def _glyph(sample: np.ndarray, height: int, squeeze: float, angle: float) -> Image.Image:
    """An ink-alpha image of one EMNIST sample: cropped to its ink, scaled to `height`, rotated."""
    ys, xs = np.nonzero(sample > 0.15)
    if len(xs) == 0:
        return Image.new("L", (max(2, height // 3), height), 0)
    crop = sample[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    img = Image.fromarray((crop * 255).astype(np.uint8), "L")
    h = height
    w = max(2, int(round(img.width * h / img.height * squeeze)))
    img = img.resize((w, h), Image.BILINEAR)
    if angle:
        img = img.rotate(angle, resample=Image.BILINEAR, expand=True, fillcolor=0)
    return img


def _stroke(ch: str, height: int, pen: int) -> Image.Image:
    """Pen-drawn punctuation, same alpha convention as glyphs."""
    w = {"@": int(height * 0.9), "-": int(height * 0.45), "/": int(height * 0.45), "'": max(4, pen * 2)}.get(ch, max(6, pen * 3))
    img = Image.new("L", (w, height), 0)
    d = ImageDraw.Draw(img)
    if ch == ".":
        d.ellipse([w / 2 - pen, height - 2.5 * pen, w / 2 + pen, height - 0.5 * pen], fill=255)
    elif ch == ",":
        d.line([(w / 2, height - 2 * pen), (w / 2 - pen, height + pen * 0.5)], fill=255, width=pen)
    elif ch == ":":
        for y in (height * 0.35, height - 2 * pen):
            d.ellipse([w / 2 - pen, y - pen, w / 2 + pen, y + pen], fill=255)
    elif ch == "-":
        d.line([(1, height * 0.55), (w - 2, height * 0.52)], fill=255, width=pen)
    elif ch == "/":
        d.line([(w - 2, 1), (2, height - 2)], fill=255, width=pen)
    elif ch == "'":
        d.line([(w / 2, 1), (w / 2 - 1, height * 0.3)], fill=255, width=pen)
    elif ch == "@":
        d.arc([2, height * 0.08, w - 2, height - 2], 20, 340, fill=255, width=pen)
        d.ellipse([w * 0.33, height * 0.33, w * 0.67, height * 0.70], outline=255, width=pen)
        d.line([(w * 0.67, height * 0.35), (w * 0.70, height * 0.68)], fill=255, width=pen)
    return img


def render_line(text: str, height: int, rng: np.random.Generator, writer: int, illegible: bool = False) -> Image.Image:
    """One line of block capitals as an ink-alpha image (0 = paper, 255 = full ink)."""
    p = pools()
    pen = max(2, height // 10)
    gap = int(height * (0.10 if not illegible else -0.24))
    parts = []
    for ch in text.upper():
        if ch == " ":
            parts.append(("space", int(height * 0.45)))
            continue
        jitter = int(rng.integers(-max(1, height // 14), max(1, height // 14) + 1))
        if ch in p:
            clear, odd = p[ch]
            src = odd if illegible else clear
            pick = (writer * 7 + int(rng.integers(0, 3))) % len(src) if not illegible else int(rng.integers(0, len(src)))
            squeeze = 0.5 if illegible else float(rng.uniform(0.85, 1.0))
            angle = float(rng.uniform(-20, 20)) if illegible else float(rng.uniform(-3, 3))
            g = _glyph(src[pick], height, squeeze, angle)
        else:
            g = _stroke(ch, height, pen)
        parts.append((g, jitter))
    width = sum(x if isinstance(g, str) else g.width + gap for g, x in parts) + 4
    canvas = Image.new("L", (max(4, width), height + height // 2), 0)
    x = 2
    for g, j in parts:
        if isinstance(g, str):
            x += j
            continue
        y = (canvas.height - g.height) // 2 + j
        region = canvas.crop((x, y, x + g.width, y + g.height))
        canvas.paste(Image.fromarray(np.maximum(np.asarray(region), np.asarray(g))), (x, y))
        x += g.width + gap
    if illegible:
        d = ImageDraw.Draw(canvas)
        for k in range(3):          # crossed out and written over: a reviewer cannot be sure what it says
            pts = [(i * canvas.width / 10, canvas.height * (0.5 + 0.3 * math.sin(i * (1.3 + k) + writer + k)))
                   for i in range(11)]
            d.line(pts, fill=235, width=pen + k)
    return canvas


def render_box(value: str, box_w: int, box_h: int, rng: np.random.Generator, writer: int, illegible: bool = False) -> Image.Image:
    """`value` (lines split on '\\n') fitted into a box of box_w x box_h pixels, left aligned with a small margin."""
    lines = [ln for ln in str(value).split("\n")] or [""]
    line_h = max(10, int(min(box_h * 0.62, box_h / len(lines) * 0.78)))
    out = Image.new("L", (box_w, box_h), 0)
    y = max(1, (box_h - len(lines) * int(line_h * 1.2)) // 2) if len(lines) == 1 else 3
    for ln in lines:
        img = render_line(ln, line_h, rng, writer, illegible)
        if img.width > box_w - 8:
            img = img.resize((box_w - 8, img.height), Image.BILINEAR)
        oy = min(max(0, y - img.height // 6), max(0, box_h - img.height))
        region = out.crop((6, oy, 6 + img.width, oy + img.height))
        out.paste(Image.fromarray(np.maximum(np.asarray(region), np.asarray(img))), (6, oy))
        y += int(line_h * 1.2)
    return out


def signature(name: str, box_w: int, box_h: int, seed: int) -> Image.Image:
    """A cursive-looking scribble (not text), deterministic per name + seed, as an ink-alpha image."""
    rng = np.random.default_rng([seed, sum(map(ord, name)), len(name)])
    scale = 3
    W, H = box_w * scale, box_h * scale
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    pen = max(3, int(H * 0.035))
    x, mid = W * 0.08, H * 0.55
    pts = []
    loops = int(rng.integers(5, 9))
    step = (W * 0.78) / loops
    for i in range(loops):
        amp = H * float(rng.uniform(0.15, 0.32))
        for t in np.linspace(0, 1, 14):
            ang = 2 * math.pi * t
            pts.append((x + step * t + math.sin(ang) * step * 0.35, mid - amp * math.sin(ang + i) * (1 if i % 2 else 0.7)))
        x += step
    d.line(pts, fill=255, width=pen, joint="curve")
    d.line([(W * 0.1, mid + H * 0.22), (W * 0.9, mid + H * float(rng.uniform(0.12, 0.25)))], fill=255, width=max(2, pen - 1))
    return img.resize((box_w, box_h), Image.LANCZOS)
