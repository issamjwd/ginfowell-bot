"""The drawn look: dream stills repainted as flat pastel backgrounds, and the footage grade.

Backgrounds: paint-smooth (median), quantize to a small pastel palette, ink the edges in the game's plum, lay a paper
grain over it. Done once per (dream, night) at half resolution and upscaled: the flat colours hide the upscale.
Footage grade: lifted pastel blacks, soft bloom, a touch of pink, vignette, film grain (from the repo's edit.py).
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from . import config

INK = (60, 38, 78)
CREAM = (255, 246, 250)
PINK = (255, 170, 210)
ROSE = (205, 70, 135)
PLUM = (70, 55, 95)


def cover(im: Image.Image, w: int, h: int) -> Image.Image:
    s = max(w / im.width, h / im.height)
    im = im.resize((max(w, round(im.width * s)), max(h, round(im.height * s))), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def drawn(im: Image.Image, colours: int = 28, ink: float = 0.6, night: bool = False, soften: int = 2) -> Image.Image:
    """Repaint an RGB image in the flat 'drawn' style (soft painted fills + plum ink lines)."""
    im = im.convert("RGB")
    w, h = im.size
    soft = im.resize((max(8, w // soften), max(8, h // soften)), Image.LANCZOS).filter(ImageFilter.MedianFilter(3))
    soft = soft.resize((w, h), Image.BICUBIC)
    soft = ImageEnhance.Color(soft).enhance(1.12)
    flat = soft.quantize(colors=colours, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).convert("RGB")
    flat = flat.filter(ImageFilter.ModeFilter(7)).filter(ImageFilter.SMOOTH_MORE)
    x = np.asarray(flat, np.float32) / 255
    x = 0.10 + 0.86 * np.power(x, 0.82)                    # brighter, lifted, pastel
    x = x * np.array([1.03, 0.98, 1.05], np.float32)       # dream pink
    if night:
        lum = x.mean(axis=2, keepdims=True)
        x = (x * 0.35 + lum * 0.15) * np.array([0.78, 0.82, 1.25], np.float32)
    gray = ImageOps.grayscale(im).filter(ImageFilter.GaussianBlur(1.6))
    e = np.asarray(gray.filter(ImageFilter.FIND_EDGES), np.float32) / 255
    e = np.clip((e - 0.05) * 7.0, 0, 1)[..., None] * ink
    inkc = np.array(INK if not night else (24, 18, 44), np.float32) / 255
    x = x * (1 - e) + inkc * e
    return Image.fromarray((np.clip(x, 0, 1) * 255).astype(np.uint8))


@lru_cache(maxsize=16)
def paper(w: int, h: int, seed: int = 5) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = rng.normal(0, 1, (h // 4 + 1, w // 4 + 1)).astype(np.float32)
    n = np.asarray(Image.fromarray(n).resize((w, h), Image.BICUBIC), np.float32)
    fine = rng.normal(0, 1, (h, w)).astype(np.float32)
    return (n * 0.014 + fine * 0.005)[..., None]


@lru_cache(maxsize=24)
def plate(still: str, night: bool, w: int, h: int, over: float = 1.3) -> Image.Image:
    """A drawn background plate `over` times the frame size (room for camera zoom and pan)."""
    src = Image.open(config.STILLS / still).convert("RGB")
    pw, ph = int(w * over * 1.35), int(h * over)
    base = cover(src, pw, ph)
    art = drawn(base, night=night)
    a = np.asarray(art, np.float32) / 255 + paper(pw, ph)
    return Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))


@lru_cache(maxsize=4)
def vignette(w: int, h: int, strength: float = 0.32) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    return (1 - strength * np.clip(r - 0.35, 0, 1) ** 1.6)[..., None]


class Grade:
    """Footage grade (port of the repo's edit.py Grade), resolution independent.

    Per frame: soft bloom (screen blend of a blurred eighth-size copy), lifted blacks, a touch of pink, vignette and
    grain on twos. Everything that does not depend on the frame (vignette x tint x lift, the upscaled grain) is built
    once, so a frame costs a handful of full-size passes (the long video grades ~3,500 1080p frames per episode)."""

    # Wave 1 grade (owner, 2026-11-04): darker and harder than the old pastel haze. A little bloom, blacks crushed a
    # touch (CONTRAST around a low pivot), almost no pink cast.
    BLOOM = 0.08
    CONTRAST, PIVOT = 1.14, 0.045

    def __init__(self, w: int, h: int, seed: int = 3):
        self.w, self.h = w, h
        rng = np.random.default_rng(seed)
        self.grain = [rng.normal(0, 1, (h // 2, w // 2)).astype(np.float32) for _ in range(6)]
        self._grain_full: dict[int, np.ndarray] = {}
        self._gain: dict[bool, tuple[np.ndarray, np.ndarray]] = {}

    def _terms(self, night: bool) -> tuple[np.ndarray, np.ndarray]:
        """x_out = x_bloomed * A + B: the contrast around PIVOT (crushes the darkest tones a little), a faint warm
        tint, the night tint and the vignette."""
        if night not in self._gain:
            tint = np.array([1.01, 0.995, 1.005], np.float32)
            if night:
                tint = tint * np.array([0.7, 0.75, 1.0], np.float32)
            v = vignette(self.w, self.h, 0.46 if night else 0.38).astype(np.float32)
            c, p = self.CONTRAST, self.PIVOT
            self._gain[night] = ((c * tint) * v, (-c * p * tint) * v)
        return self._gain[night]

    def _grain(self, i: int) -> np.ndarray:
        k = (i // 2) % len(self.grain)  # grain on twos too
        if k not in self._grain_full:
            g = self.grain[k]
            self._grain_full[k] = (np.repeat(np.repeat(g, 2, 0), 2, 1)[: self.h, : self.w, None] * 0.016)
        return self._grain_full[k]

    def __call__(self, f: np.ndarray, i: int, night: bool = False) -> np.ndarray:
        x = f.astype(np.float32)
        x *= 1 / 255
        small = Image.fromarray(f).resize((self.w // 8, self.h // 8), Image.BILINEAR).filter(ImageFilter.GaussianBlur(5))
        bloom = np.asarray(small.resize((self.w, self.h), Image.BILINEAR), np.float32)
        bloom *= self.BLOOM / 255
        # screen blend: 1 - (1 - x)(1 - b) = x + b (1 - x)
        bloom *= 1 - x
        x += bloom
        a, b = self._terms(night)
        x *= a
        x += b
        x += self._grain(i)
        np.clip(x, 0, 1, out=x)
        x *= 255
        return x.astype(np.uint8)


def vhs(f: np.ndarray, i: int) -> np.ndarray:
    """Found-footage tape look: colour fringing (red and blue pulled apart), scanlines, a little less colour, and a
    soft tracking band that rolls down the frame. Cheap numpy, deterministic per frame."""
    h, w = f.shape[:2]
    s = max(1, w // 270)
    x = f.astype(np.int16)
    out = x.copy()
    out[:, s:, 0] = x[:, :-s, 0]           # red to the right
    out[:, :-s, 2] = x[:, s:, 2]           # blue to the left
    lum = out.mean(axis=2, keepdims=True)
    out = (out * 0.82 + lum * 0.18)        # a little less colour
    out[::3] *= 0.86                        # scanlines
    band = int((i * 7) % (h + h // 6)) - h // 12
    y0, y1 = max(0, band), min(h, band + h // 12)
    if y1 > y0:
        out[y0:y1] = out[y0:y1] * 0.9 + 18
        out[y0:y1, : w - 2 * s] = out[y0:y1, 2 * s:]   # the band slips sideways
    return np.clip(out, 0, 255).astype(np.uint8)


def impact_frame(f: np.ndarray, invert: bool) -> np.ndarray:
    """Anime impact frame: 3-tone ink/rose/cream posterization, optionally inverted."""
    g = np.asarray(ImageOps.autocontrast(ImageOps.grayscale(Image.fromarray(f)), cutoff=2), np.float32) / 255
    lv = np.digitize(g, [0.33, 0.62])
    pal = np.array([INK, ROSE, CREAM], np.uint8)
    if invert:
        pal = pal[::-1]
    return pal[lv]


def tint_shadow(sprite: Image.Image) -> Image.Image:
    """Shadow-catcher pixels come out near-black and translucent: turn them into a soft plum shadow."""
    a = np.asarray(sprite).astype(np.float32)
    rgb, al = a[..., :3], a[..., 3]
    dark = rgb.max(axis=2) < 6  # shadow-catcher pixels are pure black; the darkest material is (34, 22, 38)
    shadow = np.where(dark, al * 0.45, 0).astype(np.uint8)
    soft = np.asarray(Image.fromarray(shadow).filter(ImageFilter.GaussianBlur(max(3, sprite.width // 50))), np.float32)
    body = ~dark
    out = np.zeros_like(a)
    out[..., :3] = np.where(body[..., None], rgb, np.array(PLUM, np.float32))
    out[..., 3] = np.where(body, al, 0)
    # shadow goes under the body: alpha-over (body over soft shadow)
    ab = out[..., 3:4] / 255
    sa = soft[..., None] / 255
    a_out = ab + sa * (1 - ab)
    rgb_out = (out[..., :3] * ab + np.array(PLUM, np.float32) * sa * (1 - ab)) / np.maximum(a_out, 1e-6)
    res = np.concatenate([rgb_out, a_out * 255], axis=2)
    return Image.fromarray(np.clip(res, 0, 255).astype(np.uint8), "RGBA")


def diff_frames(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16))))

