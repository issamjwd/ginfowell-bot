"""Edit effects: captions, speed lines, shake, flash, zoom punch, the memory orb, title and end cards.

All sizes scale with the frame's short side (s = min(W, H) / 1080) so a low-res dry run looks like the real thing,
smaller, and the 16:9 long video uses the same type sizes as the Shorts. Short captions sit in the Shorts safe area:
above the bottom title/channel overlay (~bottom 22%) and clear of the right-hand button column. Long-video captions
hang off the left margin (the same grid as the Remotion overlay), in the text column left of the footage panel.
"""
from __future__ import annotations

import math
import random
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import fonts, style
from .look import CREAM, INK, PINK, ROSE

# Caption layout and text colours come from assets/style.json (shared with the Remotion overlay in render/).
CAPTION_Y = style.caption()["y"]  # fraction of height: upper third, clear of faces and of the Shorts UI overlay


def wrap(text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


@lru_cache(maxsize=64)
def unit(w: int, h: int) -> float:
    """Layout scale: 1.0 at 1080 on the short side (both the 1080x1920 Short and the 1920x1080 long video)."""
    return min(w, h) / 1080


LONG_COLUMN = 1300  # px at 1080 tall: the long video's text width (full-frame footage, left-margin grid)


@lru_cache(maxsize=64)
def caption_layer(text: str, w: int, h: int, jitter: int = 0, y: float | None = None) -> Image.Image:
    """Lowercase caption: paper Montserrat with an ink stroke and a soft shadow. Cached per (text, boil offset)."""
    s = unit(w, h)
    cap = style.caption()
    wide = w > h
    f = fonts.sans(int((92 if wide else cap["size"]) * s), "bold")
    lines = wrap(text, f, int(LONG_COLUMN * s if wide else w * cap["max_width"]))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d, ds = ImageDraw.Draw(layer), ImageDraw.Draw(shadow)
    lh = int((108 if wide else cap["line_height"]) * s)
    y0 = int(h * (y if y is not None else 0.74 if wide else CAPTION_Y)) - (len(lines) - 1) * lh // 2
    # Short: centred, nudged left of the button column. Long: on the left margin, like the overlay's grid.
    cx = int(cap.get("left", 96) * s) if wide else w // 2 - int(cap["nudge_left"] * s)
    anchor = "lm" if wide else "mm"
    for i, ln in enumerate(lines):
        y = y0 + i * lh + (jitter if i % 2 == 0 else -jitter)
        ds.text((cx, y + int(5 * s)), ln, font=f, fill=(40, 20, 50, 190), anchor=anchor)
        d.text((cx + jitter, y), ln, font=f, fill=style.rgb("paper") + (255,), anchor=anchor,
               stroke_width=max(2, int(5 * s)), stroke_fill=style.rgb("ink") + (255,))
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(10 * s) or 1))
    return Image.alpha_composite(shadow, layer)


def put_caption(frame: Image.Image, text: str, t: float, drawing: int, y: float | None = None) -> Image.Image:
    """Fade + small rise in, and a 1 px 'boil' that changes on twos (reads as hand-lettered)."""
    a = min(1.0, max(0.0, (t - 0.08) / 0.22))
    if a <= 0:
        return frame
    w, h = frame.size
    lay = caption_layer(text, w, h, jitter=(drawing % 2), y=y)
    if a < 1:
        lay = lay.copy()
        lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
    rise = int((1 - a) * 18 * unit(w, h))
    frame.alpha_composite(lay, (0, rise))
    return frame


@lru_cache(maxsize=32)
def speed_lines(w: int, h: int, seed: int, focus: tuple[float, float] = (0.5, 0.45), dark: bool = False) -> Image.Image:
    """Anime focus lines: thin wedges from the edges toward the focus, a clear centre."""
    rng = random.Random(seed)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = focus[0] * w, focus[1] * h
    R = math.hypot(w, h)
    col = (INK if dark else CREAM) + (210,)
    for _ in range(110):
        ang = rng.uniform(0, 2 * math.pi)
        inner = rng.uniform(0.34, 0.5) * min(w, h)
        spread = rng.uniform(0.002, 0.009)
        p1 = (cx + math.cos(ang) * inner, cy + math.sin(ang) * inner)
        p2 = (cx + math.cos(ang - spread) * R, cy + math.sin(ang - spread) * R)
        p3 = (cx + math.cos(ang + spread) * R, cy + math.sin(ang + spread) * R)
        d.polygon([p1, p2, p3], fill=col)
    return layer


def shake_offset(t: float, seed: int, amp: float, decay: float = 5.0) -> tuple[int, int]:
    """Camera shake at shot time t: a decaying envelope over two incommensurate sines per axis (7-13 Hz), so a hit
    rattles and settles like a real camera instead of buzzing with fresh random numbers every frame (edit v4).
    bot/overlay.py and bot/rig.py read the same function, so the text and the particles ride the same shake."""
    rng = random.Random(seed * 1000 + 7)
    a = amp * math.exp(-decay * max(0.0, t))
    f1, f2, f3, f4 = rng.uniform(7, 10), rng.uniform(10, 13), rng.uniform(7, 10), rng.uniform(10, 13)
    p1, p2, p3, p4 = (rng.uniform(0, 2 * math.pi) for _ in range(4))
    tau = 2 * math.pi * t
    dx = 0.65 * math.sin(f1 * tau + p1) + 0.35 * math.sin(f2 * tau + p2)
    dy = 0.65 * math.sin(f3 * tau + p3) + 0.35 * math.sin(f4 * tau + p4)
    return int(a * dx), int(a * dy)


def apply_shake(arr: np.ndarray, dx: int, dy: int) -> np.ndarray:
    if not dx and not dy:
        return arr
    h, w = arr.shape[:2]
    pad = max(abs(dx), abs(dy)) + 2
    big = Image.fromarray(arr).resize((w + 2 * pad, h + 2 * pad), Image.BILINEAR)
    x0, y0 = pad + dx, pad + dy
    return np.asarray(big.crop((x0, y0, x0 + w, y0 + h)))


def flash(arr: np.ndarray, k: float, colour=(255, 236, 246)) -> np.ndarray:
    if k <= 0:
        return arr
    c = np.array(colour, np.float32)
    return (arr.astype(np.float32) * (1 - k) + c * k).astype(np.uint8)


def orb(frame: Image.Image, t: float, pos=(0.66, 0.56)) -> Image.Image:
    """A glowing memory, gently bobbing (the game's 3 glowing memories per dream)."""
    w, h = frame.size
    s = unit(w, h)
    cx, cy = pos[0] * w, pos[1] * h + math.sin(t * 2.2) * 14 * s
    r = 34 * s * (1 + 0.06 * math.sin(t * 5))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    for k, a in ((5.0, 40), (3.2, 70), (2.0, 120)):
        d.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k], fill=PINK + (a,))
    glow = glow.filter(ImageFilter.GaussianBlur(28 * s))
    frame.alpha_composite(glow)
    d = ImageDraw.Draw(frame)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 250, 252, 255), outline=ROSE + (255,), width=max(1, int(3 * s)))
    return frame


def card(w: int, h: int, big: str | None, line: str | None, t: float, dur: float, seed: int) -> Image.Image:
    """Dark plum card with soft stars; 'liminality' in Garamond, the line in Inter. Fades in/out.

    big=None and line=None gives the bare background (the clean render under the Remotion overlay)."""
    s = unit(w, h)
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    top, bot = np.array((44, 30, 62), np.float32), np.array((22, 16, 36), np.float32)
    base = top * (1 - yy) + bot * yy
    arr = np.broadcast_to(base, (h, w, 3)).copy()
    rng = np.random.default_rng(seed)
    for _ in range(int(70 * s * s * 4) or 20):
        x, y = rng.integers(0, w), rng.integers(0, h)
        tw = 0.5 + 0.5 * math.sin(t * 3 + x)
        arr[max(0, y - 1):y + 1, max(0, x - 1):x + 1] = np.array((255, 220, 236)) * (0.4 + 0.5 * tw)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGBA")
    a = max(0.0, min(1.0, (t - 0.1) / 0.5, (dur - t) / 0.4))
    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    if big:
        size = int(190 * s)
        while size > 40 and fonts.serif(size).getlength(big) > w * 0.86:  # a long dream name still fits the card
            size -= 8
        d.text((w // 2, int(h * 0.44) if h > w else int(h * 0.40)), big, font=fonts.serif(size, italic=False),
               fill=CREAM + (255,), anchor="mm")
    if line:
        f = fonts.serif(int(66 * s), italic=True)
        # portrait: the Shorts' own fractions; landscape (1080 tall) needs the line a fixed step under the title
        y_line = int(h * (0.53 if big else 0.48)) if h > w else (int(h * 0.40 + 150 * s) if big else int(h * 0.48))
        for i, ln in enumerate(wrap(line, f, int(w * 0.8))):
            d.text((w // 2, y_line + i * int(80 * s)), ln, font=f, fill=(255, 205, 230, 255), anchor="mm")
    lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
    img.alpha_composite(lay)
    return img


def end_card(w: int, h: int, t: float, dur: float, text: bool = True) -> Image.Image:
    s = unit(w, h)
    img = card(w, h, None, None, t, dur, 11)
    if not text:
        return img
    a = max(0.0, min(1.0, t / 0.4))
    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    # rows: portrait keeps the Shorts' fractions; landscape (1080 tall) spaces them in fixed steps around the middle
    ys = [int(h * k) for k in (0.40, 0.47, 0.53, 0.62)] if h > w else [int(h / 2 + k * s) for k in (-150, -20, 60, 190)]
    d.text((w // 2, ys[0]), "liminality", font=fonts.serif(int(150 * s)), fill=CREAM + (255,), anchor="mm")
    d.text((w // 2, ys[1]), "free on roblox", font=fonts.serif(int(64 * s), italic=True), fill=(255, 190, 220, 255), anchor="mm")
    d.text((w // 2, ys[2]), "play alone or fall asleep together with friends", font=fonts.sans(int(32 * s), "medium"),
           fill=(230, 210, 236, 255), anchor="mm")
    f = fonts.sans(int(38 * s), "bold")
    d.text((w // 2, ys[3]), "P I X E L H E L M", font=f, fill=PINK + (255,), anchor="mm")
    lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
    img.alpha_composite(lay)
    return img


__all__ = ["put_caption", "speed_lines", "shake_offset", "apply_shake", "flash", "orb", "card", "end_card", "INK"]
