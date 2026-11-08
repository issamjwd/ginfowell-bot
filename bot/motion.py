"""Camera and cut motion for the footage edit (edit v4): eased camera moves, handheld drift, snap-zoom punch-ins,
speed ramps / freeze frames, real motion blur, and the edited transitions (whip, flash, light-leak burn, dip to
black, zoom-through, glitch). Pure numpy + Pillow, deterministic, no new packages.

Shot fields read here (all optional; bot/storyboard.py validates them):
  "hand":    handheld drift amplitude, a fraction of the frame's short side (0 .. 0.03)
  "bob":     walking head-bob, a fraction of the short side (0 .. 0.02): the pov style
  "punches": shot-local seconds where the camera snaps in (a beat, a hit)
  "punch":   how far a punch-in goes (0.04 .. 0.2 of the frame)
  "ramp":    speed ramp, [[local s, speed], ...] piecewise constant from each time on; speed 0 = freeze frame
  "transition": cut | fade | whip | flash | leak | dip | zoom | glitch (how this shot comes IN; the shot before it
             also plays the transition's OUT half over its last frames, so every cut is two-sided)

Frame maths (local time tl in seconds, fps frames a second) is shared with bot/rig.py, which puts the sounds on the
same frames: whooshes land on whip cuts, hits on flash cuts and punches, risers end on the reveal.
"""
from __future__ import annotations

import math
import random
from functools import lru_cache

import numpy as np
from PIL import Image

# transitions: how many frames of the new shot (IN) and of the shot before (OUT) each one touches
IN_FRAMES = {"cut": 0, "fade": 7, "whip": 5, "flash": 3, "leak": 9, "dip": 7, "zoom": 5, "glitch": 3}
OUT_FRAMES = {"cut": 0, "fade": 0, "whip": 3, "flash": 0, "leak": 6, "dip": 5, "zoom": 3, "glitch": 2}
TRANSITIONS = tuple(IN_FRAMES)
PUNCH_SNAP = 0.07    # seconds a punch-in takes to arrive (2 frames at 24 fps: a snap, not a zoom)
PUNCH_TAU = 0.42     # seconds it takes to relax back (exponential)
FLASH_RGB = (255, 240, 232)


# ------------------------------------------------------------------------------------------------ easing
def ease_out_expo(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 1.0 if x >= 1 else 1 - 2 ** (-10 * x)


def ease_in_cubic(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * x


def ease_in_out_sine(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return -(math.cos(math.pi * x) - 1) / 2


# ------------------------------------------------------------------------------------------------ camera
def punch_env(tl: float, punches: list[float] | tuple[float, ...]) -> float:
    """0..1: how far the camera is punched in at shot-local time tl (snap in over PUNCH_SNAP, relax over PUNCH_TAU).
    Overlapping punches take the strongest, never add up."""
    best = 0.0
    for tp in punches or ():
        d = tl - float(tp)
        if d < 0:
            continue
        v = ease_out_expo(d / PUNCH_SNAP) * math.exp(-max(0.0, d - PUNCH_SNAP) / PUNCH_TAU)
        best = max(best, v)
    return best


def handheld(t: float, seed: int, amp: float) -> tuple[float, float, float]:
    """(dx, dy, rot) of a handheld camera at time t: three incommensurate slow sines per axis (0.2-1.3 Hz), so it
    drifts like a held phone, never buzzes. dx, dy in units of amp; rot is unused rotation intent (kept 0: rotation
    would cost a full-frame resample)."""
    if amp <= 0:
        return 0.0, 0.0, 0.0
    r = random.Random(seed * 7919 + 13)
    dx = dy = 0.0
    for w in (0.55, 0.3, 0.15):
        dx += w * math.sin(2 * math.pi * r.uniform(0.2, 1.3) * t + r.uniform(0, 6.283))
        dy += w * math.sin(2 * math.pi * r.uniform(0.2, 1.3) * t + r.uniform(0, 6.283))
    return dx * amp, dy * amp, 0.0


def bob(t: float, amp: float, cadence: float = 1.85) -> float:
    """Vertical walking bob (two steps a cycle): |sin| so each footfall is a little dip."""
    if amp <= 0:
        return 0.0
    return amp * (abs(math.sin(math.pi * cadence * t)) - 0.5) * 2


def camera(f: np.ndarray, z: float, ox: float = 0.0, oy: float = 0.0) -> np.ndarray:
    """Crop a 1/z window around the centre moved by (ox, oy) px (clamped to the frame) and scale it back up."""
    if z <= 1.001 and abs(ox) < 0.5 and abs(oy) < 0.5:
        return f
    z = max(z, 1.0)
    h, w = f.shape[:2]
    cw, ch = w / z, h / z
    x0 = min(max((w - cw) / 2 + ox, 0.0), w - cw)
    y0 = min(max((h - ch) / 2 + oy, 0.0), h - ch)
    im = Image.fromarray(f)
    return np.asarray(im.resize((w, h), Image.BILINEAR, box=(x0, y0, x0 + cw, y0 + ch)))


def headroom(amp_px: float, w: int, h: int) -> float:
    """The smallest zoom that leaves room for amp_px of drift on every side."""
    return 1.0 + 2.2 * amp_px / max(1, min(w, h))


# ------------------------------------------------------------------------------------------------ speed ramps
def source_time(tl: float, ramp: list | None, speed: float = 1.0) -> float:
    """Clip seconds played by shot-local time tl under a piecewise-constant speed ramp [[t0, s0], [t1, s1], ...]
    (each speed holds from its time to the next; before the first entry the shot's own speed)."""
    if not ramp:
        return tl * speed
    pts = sorted((float(a), float(b)) for a, b in ramp)
    out, t, s = 0.0, 0.0, speed
    for a, b in pts:
        if tl <= a:
            break
        out += (a - t) * s
        t, s = a, b
    return out + (tl - t) * s


def speed_at(tl: float, ramp: list | None, speed: float = 1.0) -> float:
    s = speed
    for a, b in sorted((float(a), float(b)) for a, b in (ramp or [])):
        if tl >= a:
            s = b
    return s


class RampReader:
    """Frames of a clip at an arbitrary, non-decreasing clip time: a 1x reader underneath, blended between the two
    nearest source frames, so slow motion is smooth instead of stuttering duplicates."""

    def __init__(self, reader, fps: int):
        self.r, self.fps = reader, fps
        self.a = self.b = None
        self.ib = -1   # index of frame b (frame a is ib - 1)

    def at(self, t_src: float) -> np.ndarray:
        x = max(0.0, t_src * self.fps)
        want = int(math.floor(x)) + 1
        while self.ib < want:
            self.a, self.b = self.b, self.r.next()
            self.ib += 1
        if self.a is None:
            return self.b
        k = x - (self.ib - 1)
        if k <= 0.04:
            return self.a
        if k >= 0.96:
            return self.b
        return np.asarray(Image.blend(Image.fromarray(self.a), Image.fromarray(self.b), k))  # uint8: ~4x faster

    def close(self) -> None:
        self.r.close()


# ------------------------------------------------------------------------------------------------ blur
def directional_blur(f: np.ndarray, length: float, axis: int = 1) -> np.ndarray:
    """A box motion blur `length` px long along one axis (axis 1 = sideways), computed at half size with a running
    sum, so a 1080p whip frame costs a few milliseconds."""
    if length < 2:
        return f
    h, w = f.shape[:2]
    small = np.asarray(Image.fromarray(f).resize((w // 2, h // 2), Image.BILINEAR), np.float32)
    n = max(1, int(length / 2))
    c = np.cumsum(np.pad(small, [(n, n) if a == axis else (0, 0) for a in range(3)], mode="edge"), axis=axis)
    if axis == 1:
        out = (c[:, 2 * n:] - c[:, :-2 * n]) / (2 * n)
    else:
        out = (c[2 * n:] - c[:-2 * n]) / (2 * n)
    out = out[: h // 2, : w // 2]
    return np.asarray(Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).resize((w, h), Image.BILINEAR))


def radial_blur(f: np.ndarray, strength: float, samples: int = 4) -> np.ndarray:
    """Zoom blur: the average of the frame scaled up to 1 + strength in `samples` steps (half-size maths). Sells a
    snap zoom / zoom-through as real camera motion."""
    if strength < 0.004:
        return f
    h, w = f.shape[:2]
    small = Image.fromarray(f).resize((w // 2, h // 2), Image.BILINEAR)
    acc = np.asarray(small, np.float32).copy()
    sw, sh = small.size
    for i in range(1, samples):
        z = 1 + strength * i / (samples - 1)
        cw, ch = sw / z, sh / z
        box = ((sw - cw) / 2, (sh - ch) / 2, (sw + cw) / 2, (sh + ch) / 2)
        acc += np.asarray(small.resize((sw, sh), Image.BILINEAR, box=box), np.float32)
    acc /= samples
    blurred = np.asarray(Image.fromarray(acc.astype(np.uint8)).resize((w, h), Image.BILINEAR))
    return blurred


# ------------------------------------------------------------------------------------------------ light
@lru_cache(maxsize=8)
def _leak_field(w: int, h: int, seed: int) -> np.ndarray:
    """A low-res (1/8) warm light-leak plate: two soft orange-rose blobs, wider than the frame (it slides across)."""
    sw, sh = max(8, w // 8), max(8, h // 8)
    W = sw * 2
    yy, xx = np.mgrid[0:sh, 0:W].astype(np.float32)
    r = random.Random(seed)
    out = np.zeros((sh, W, 3), np.float32)
    for col, cx, cy, rad in (((1.0, 0.55, 0.25), r.uniform(0.3, 0.7), r.uniform(0.2, 0.8), r.uniform(0.35, 0.5)),
                             ((1.0, 0.3, 0.45), r.uniform(0.9, 1.4), r.uniform(0.2, 0.8), r.uniform(0.25, 0.4))):
        d = ((xx / sw - cx) ** 2 + ((yy / sh - cy) * sh / sw) ** 2) / rad ** 2
        out += np.exp(-d)[..., None] * np.array(col, np.float32)
    return np.clip(out, 0, 1.2)


def light_leak(f: np.ndarray, amount: float, slide: float, seed: int = 1) -> np.ndarray:
    """Screen a warm light leak over the frame. amount 0..1 (its brightness), slide 0..1 (where the blobs are)."""
    if amount <= 0.01:
        return f
    h, w = f.shape[:2]
    field = _leak_field(w, h, seed)
    sw = field.shape[1] // 2
    x0 = int(slide * sw)
    part = (np.clip(field[:, x0:x0 + sw] * amount, 0, 1) * 255).astype(np.uint8)
    leak = np.asarray(Image.fromarray(part).resize((w, h), Image.BILINEAR), np.float32) / 255
    x = f.astype(np.float32) / 255
    x = 1 - (1 - x) * (1 - leak)                     # screen
    x = x * (1 + 0.25 * amount)                       # the burn over-exposes a little
    return (np.clip(x, 0, 1) * 255).astype(np.uint8)


def flash(f: np.ndarray, k: float, colour=FLASH_RGB) -> np.ndarray:
    if k <= 0:
        return f
    return (f.astype(np.float32) * (1 - k) + np.array(colour, np.float32) * k).astype(np.uint8)


def exposure(f: np.ndarray, k: float) -> np.ndarray:
    """Multiply the frame's light (k < 1 darker, k = 0 black)."""
    if abs(k - 1) < 0.01:
        return f
    return np.clip(f.astype(np.float32) * k, 0, 255).astype(np.uint8)


def freeze_look(f: np.ndarray, k: float) -> np.ndarray:
    """The freeze frame's still-photo look: colour drained and contrast pushed by k (0..1)."""
    if k <= 0:
        return f
    x = f.astype(np.float32)
    lum = x.mean(axis=2, keepdims=True)
    x = x * (1 - 0.55 * k) + lum * 0.55 * k
    x = (x - 128) * (1 + 0.35 * k) + 128
    return np.clip(x, 0, 255).astype(np.uint8)


def glitch(f: np.ndarray, prev: np.ndarray | None, amount: float, seed: int) -> np.ndarray:
    """Tape glitch: horizontal bands torn sideways, red and blue pulled apart, a few bands from the shot before."""
    if amount <= 0:
        return f
    h, w = f.shape[:2]
    r = random.Random(seed)
    out = f.copy()
    s = max(2, int(w * 0.012 * amount))
    out[:, s:, 0] = f[:, :-s, 0]
    out[:, :-s, 2] = f[:, s:, 2]
    for _ in range(int(3 + 6 * amount)):
        y0 = r.randrange(0, h)
        bh = r.randint(max(2, h // 80), max(3, h // 14))
        dx = int(r.uniform(-1, 1) * w * 0.12 * amount)
        src = prev if prev is not None and r.random() < 0.35 else out
        band = np.roll(src[y0:y0 + bh], dx, axis=1)
        out[y0:y0 + bh] = band
    return out


# ------------------------------------------------------------------------------------------------ transitions
def transition_in(kind: str, f: np.ndarray, prev: np.ndarray | None, i: int, seed: int) -> np.ndarray:
    """The new shot's first IN_FRAMES[kind] frames (i = local frame index)."""
    n = IN_FRAMES.get(kind, 0)
    if i >= n or kind == "cut":
        return f
    p = (i + 1) / (n + 1)
    h, w = f.shape[:2]
    if kind == "fade" and prev is not None:
        k = ease_in_out_sine(p)
        return (prev.astype(np.float32) * (1 - k) + f.astype(np.float32) * k).astype(np.uint8)
    if kind == "whip" and prev is not None:
        # the old shot is thrown out left, the new one comes in from the right, smeared by its own speed
        pos = ease_in_out_sine(min(1.0, (i + 1) / n))
        prev_pos = ease_in_out_sine(i / n)
        shift = max(1, min(w - 1, int(w * pos)))
        out = np.empty_like(f)
        out[:, : w - shift] = prev[:, shift:]
        out[:, w - shift:] = f[:, :shift]
        speed = (pos - prev_pos) * w
        return directional_blur(out, min(w * 0.35, speed * 0.9))
    if kind == "flash":
        return flash(f, (0.82, 0.42, 0.14)[i])
    if kind == "leak":
        k = 1 - ease_out_expo(i / n)   # strongest on the cut, burning off
        return light_leak(f, 0.95 * k, 0.5 + 0.5 * i / n, seed)
    if kind == "dip":
        return exposure(f, ((i + 1) / (n + 1)) ** 0.7)    # up out of black, quickly at first
    if kind == "zoom":
        k = (1 - i / n) ** 2            # lands at the out-half's push (1.28) and settles over the next frames
        return radial_blur(camera(f, 1 + 0.28 * k), 0.2 * k)
    if kind == "glitch":
        return glitch(f, prev, (1.0, 0.7, 0.35)[i], seed * 31 + i)
    return f


def transition_out(kind: str, f: np.ndarray, frames_left: int, seed: int) -> np.ndarray:
    """The outgoing shot's last OUT_FRAMES[kind] frames before a cut of `kind` (frames_left = 1 on the last one)."""
    n = OUT_FRAMES.get(kind, 0)
    if frames_left > n or frames_left < 1 or n == 0:
        return f
    p = (n - frames_left + 1) / n          # 1/n .. 1 toward the cut
    w = f.shape[1]
    if kind == "whip":
        # the pan starts: the old shot accelerates left and smears before the cut
        k = ease_in_cubic(p)
        moved = np.roll(f, -int(w * 0.08 * k), axis=1)
        return directional_blur(moved, w * 0.12 * k)
    if kind == "leak":
        return light_leak(f, 0.8 * ease_in_cubic(p), 0.5 * p, seed)
    if kind == "dip":
        return exposure(f, 1 - ease_in_cubic(p))
    if kind == "zoom":
        k = ease_in_cubic(p)
        return radial_blur(camera(f, 1 + 0.28 * k), 0.2 * k)
    if kind == "glitch":
        return glitch(f, None, 0.5 * p, seed * 17 + frames_left)
    return f
