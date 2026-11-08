"""Narrator voice (owner 2026-11-08: the edits need to be far better). Kokoro-82M (Apache-2.0 weights) through the
kokoro-onnx package (MIT), on the CPU: about half a second per line on the runner.

Reads the hook and the caption lines at the moments they appear on screen; a line too long for its slot is spoken
faster (up to MAX_SPEED), the music dips under the voice. Off (the edit is unchanged) when BOT_VOICE=off, when the
package or the model files are missing, or on any error: a voice problem never costs an episode.

The model files come from the kokoro-onnx GitHub release, checked against MODEL_SHA256 before use (the workflow
downloads them into voice/; BOT_VOICE_DIR overrides).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

DIR = Path(os.environ.get("BOT_VOICE_DIR") or Path(__file__).resolve().parent.parent / "voice")
MODEL_SHA256 = {
    "kokoro-v1.0.int8.onnx": "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
DEFAULT_VOICE = "am_michael"   # calm, low: suits liminality's quiet dread
SPEED = 0.92
MAX_SPEED = 1.3
GAIN = 0.9          # voice level against the music (both peak-normalised before the final loudnorm)
DUCK = 0.6          # how far the music dips while the voice speaks
_engine = None


def enabled() -> bool:
    if (os.environ.get("BOT_VOICE") or "").strip().lower() == "off":
        return False
    return all((DIR / f).is_file() for f in MODEL_SHA256)


def _load():
    global _engine
    if _engine is None:
        for name, want in MODEL_SHA256.items():
            if hashlib.sha256((DIR / name).read_bytes()).hexdigest() != want:
                raise ValueError(f"{name} does not match its pinned sha256")
        from kokoro_onnx import Kokoro
        _engine = Kokoro(str(DIR / "kokoro-v1.0.int8.onnx"), str(DIR / "voices-v1.0.bin"))
    return _engine


def spoken(text: str) -> str:
    """Caption text as it should be read: no hashtags or symbols, "pov:" said as letters."""
    t = re.sub(r"#\w+", "", text)
    t = re.sub(r"\bpov\b:?", "p o v,", t, flags=re.I)
    t = t.replace("...", ",").replace("_", " ")
    return re.sub(r"\s+", " ", t).strip(" ,")


def lines(sb: dict) -> list[tuple[float, float, str]]:
    """(start, slot end, text) to speak: the hook from 0, then each caption line (chunks of one line merged)."""
    out: list[tuple[float, float, str]] = []
    hook = sb.get("hook") or {}
    if hook.get("title"):
        out.append((0.15, float(hook.get("seconds") or 2.0), str(hook["title"])))
    for c in sb.get("captions") or []:
        text, start, end = str(c.get("text", "")), float(c.get("start", 0)), float(c.get("end", 0))
        if out and start - out[-1][1] < 0.06 and len(out) > (1 if hook.get("title") else 0):
            s0, _, t0 = out[-1]
            out[-1] = (s0, end, f"{t0} {text}")
        else:
            out.append((start, end, text))
    return [(s, e, spoken(t)) for s, e, t in out if spoken(t)]


def _say(text: str, voice: str, room: float) -> np.ndarray:
    from scipy.signal import resample_poly
    from . import music
    a, sr = _load().create(text, voice=voice, speed=room, lang="en-us")
    a = np.asarray(a, np.float32)
    return resample_poly(a, 147, 80).astype(np.float32) if sr == 24000 else a   # 24 kHz -> 44.1 kHz


def say(text: str, voice: str | None = None, speed: float = SPEED) -> np.ndarray | None:
    """One line as mono float32 at 44.1 kHz, or None when the narrator is off or fails."""
    if not enabled() or not spoken(text):
        return None
    try:
        return _say(spoken(text), voice or os.environ.get("BOT_VOICE") or DEFAULT_VOICE, speed)
    except Exception as e:  # noqa: BLE001 - optional
        log.warning("narrator line skipped: %s: %s", type(e).__name__, e)
        return None


def place(st: np.ndarray, clips: list[tuple[float, np.ndarray]], sr: int) -> np.ndarray:
    """Lay voice clips (start seconds, mono) over the stereo music, ducking it under them."""
    n = len(st)
    track = np.zeros(n, np.float32)
    for start, a in clips:
        i = int(start * sr)
        a = a[: max(0, min(len(a), n - i))]
        track[i:i + len(a)] += a
    if not track.any():
        return st
    track /= np.abs(track).max() + 1e-9
    win = int(0.15 * sr)
    env = np.convolve(np.abs(track) > 0.02, np.ones(win) / win, "same").clip(0, 1).astype(np.float32)
    out = st * (1 - DUCK * env)[:, None] + (track * GAIN)[:, None]
    return (out / (np.abs(out).max() + 1e-9) * 0.89).astype(np.float32)


def mix(st: np.ndarray, sb: dict, sr: int, voice: str | None = None) -> np.ndarray:
    """The stereo mix with the narrator on top and the music ducked under it. Unchanged on any problem."""
    if not enabled():
        return st
    voice = voice or os.environ.get("BOT_VOICE") or DEFAULT_VOICE
    try:
        n = len(st)
        clips = []
        todo = lines(sb)
        for k, (start, end, text) in enumerate(todo):
            nxt = todo[k + 1][0] if k + 1 < len(todo) else n / sr
            room = max(0.8, min(end, nxt) - start + 0.25)
            a = _say(text, voice, SPEED)
            if len(a) / sr > room:
                a = _say(text, voice, min(MAX_SPEED, SPEED * len(a) / sr / room))
            clips.append((start, a))
        return place(st, clips, sr)
    except Exception as e:  # noqa: BLE001 - the narrator is optional: never lose the episode over it
        log.warning("narrator skipped: %s: %s", type(e).__name__, e)
        return st
