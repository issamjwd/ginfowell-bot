"""Soft dreamcore score, synthesized from scratch (no samples), locked to the episode's tempo.

Ported from the liminality repo's tools/video/ambient.py: detuned pad chords with tape wobble, a music-box melody,
hiss and a cheap convolution reverb. Additions for Shorts: everything is on a beat grid (cuts are whole beats), a
soft heartbeat pulse when the mood is tense, a dead-silent drop right before the impact frame, a low hit + reverse
swell on each impact, and the score returning softly for the end card.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve, lfilter

SR = 44100
CHORD_SETS = [
    [[53, 57, 60, 64, 69], [52, 55, 59, 62, 67], [50, 57, 60, 64, 65], [48, 55, 59, 64, 67]],  # Fmaj7 Em7 Dm9 Cmaj7
    [[45, 52, 57, 60, 64], [41, 48, 57, 60, 65], [43, 50, 55, 59, 62], [40, 47, 55, 59, 64]],  # Am F G Em
    [[50, 57, 62, 65, 69], [46, 53, 58, 62, 65], [48, 55, 60, 64, 67], [45, 52, 57, 61, 64]],  # Dm Bb C A
]
MELODIES = [
    [76, 74, 72, 71, 72, 74, 79, 76, 74, 72, 69, 71, 72, None, 67, None],
    [72, None, 76, 74, 72, None, 69, 71, 72, 74, None, 76, 79, None, 77, 76],
    [69, 72, 76, None, 74, 72, 71, None, 69, None, 67, 69, 72, None, None, None],
]


def note(m: int) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    a = np.exp(-2 * np.pi * cutoff / SR)
    for _ in range(2):
        x = lfilter([1 - a], [1, -a], x)
    return x


def pad(t: np.ndarray, chords: list, chord_len: float, rng: np.random.Generator) -> np.ndarray:
    out = np.zeros_like(t)
    dur = t[-1] if len(t) else 0
    wob = 1 + 0.0025 * np.sin(2 * np.pi * 0.31 * t) + 0.0012 * np.sin(2 * np.pi * 1.7 * t)
    cum = np.cumsum(wob) / SR
    for k in range(int(np.ceil(dur / chord_len)) + 1):
        t0 = k * chord_len
        # only the chord's own window is computed (the envelope is zero elsewhere): a 2-3 minute score stays cheap
        i0 = max(0, int((t0 - 1.0) * SR) - 1)
        i1 = min(len(t), int((t0 + chord_len + 1.0) * SR) + 2)
        tw = t[i0:i1]
        env = np.clip((tw - t0 + 1.0) / 2.0, 0, 1) * np.clip((t0 + chord_len + 1.0 - tw) / 2.0, 0, 1)
        if not env.any():
            continue
        for m in chords[k % len(chords)]:
            for det in (-0.13, 0.0, 0.11):
                ph = 2 * np.pi * note(m) * (1 + det / 100) * cum[i0:i1] + rng.uniform(0, 6.28)
                out[i0:i1] += env * (np.sin(ph) + 0.18 * np.sin(2 * ph) + 0.06 * np.sin(3 * ph)) / 3
    return out / 6


def music_box(t: np.ndarray, melody: list, step: float, start: float) -> np.ndarray:
    out = np.zeros_like(t)
    dur = len(t) / SR
    k, tt = 0, start
    while tt < dur:
        m = melody[k % len(melody)]
        if m is not None:
            f = note(m)
            i0 = int(tt * SR)
            seg = t[i0:i0 + int(SR * 2.5)] - tt
            e = np.exp(-seg * 2.2) * np.clip(seg * 200, 0, 1)
            s = (np.sin(2 * np.pi * f * seg) + 0.35 * np.sin(2 * np.pi * f * 2.01 * seg) * np.exp(-seg * 6)
                 + 0.15 * np.sin(2 * np.pi * f * 3.98 * seg) * np.exp(-seg * 9))
            out[i0:i0 + len(seg)] += 0.22 * e * s
        tt += step
        k += 1
    return out


def heartbeat(t: np.ndarray, beat: float) -> np.ndarray:
    out = np.zeros_like(t)
    for b0 in np.arange(0, len(t) / SR, beat * 2):
        for off, amp in ((0.0, 1.0), (0.16, 0.6)):
            i0 = max(0, int((b0 + off) * SR) - 1)
            i1 = min(len(t), int((b0 + off + 0.35) * SR) + 2)
            seg = t[i0:i1] - (b0 + off)
            m = (seg >= 0) & (seg < 0.35)
            s = seg[m]
            out[i0:i1][m] += amp * np.sin(2 * np.pi * (52 + 20 * np.exp(-s * 30)) * s) * np.exp(-s * 14)
    return lowpass(out, 400) * 0.35


def reverb(x: np.ndarray, rng: np.random.Generator, length: float = 2.4, mix: float = 0.35) -> np.ndarray:
    n = int(length * SR)
    ir = rng.standard_normal(n) * np.exp(-np.linspace(0, 6, n))
    ir[0] = 0
    wet = fftconvolve(x, ir)[:len(x)]
    wet /= np.max(np.abs(wet)) + 1e-9
    return x * (1 - mix) + wet * mix * np.max(np.abs(x))


def hit(t: np.ndarray, at: float) -> np.ndarray:
    seg = t - at
    out = np.zeros_like(t)
    m = (seg >= 0) & (seg < 2.5)
    s = seg[m]
    out[m] = np.sin(2 * np.pi * (48 + 30 * np.exp(-s * 8)) * s) * np.exp(-s * 2.5) * 0.9
    m2 = (seg > -1.2) & (seg < 0)
    s2 = -seg[m2]
    out[m2] += np.sin(2 * np.pi * 220 * s2 + 3 * np.sin(2 * np.pi * 3 * s2)) * (1 - s2 / 1.2) ** 3 * 0.22
    return out


BEDS = ("full", "drone", "pulse", "box")


def score(seconds: float, bpm: int, seed: int, mood: str = "soft", hits: list[float] | None = None,
          drop_at: float | None = None, resume_at: float | None = None, bed: str = "full") -> np.ndarray:
    """Stereo float32 array (n, 2) in [-1, 1].

    bed (the edit styles' different music): full = pad + music box (+ heartbeat when tense); drone = the pad alone,
    darker; pulse = pad + a steady heartbeat, no melody; box = the music box up front over a thin pad."""
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    t = np.arange(int(seconds * SR)) / SR
    chords = CHORD_SETS[seed % len(CHORD_SETS)]
    melody = MELODIES[(seed // 7) % len(MELODIES)]
    p = lowpass(pad(t, chords, beat * 8, rng), (1800 if mood == "soft" else 1300) * (0.6 if bed == "drone" else 1.0))
    mb = music_box(t, melody, beat if mood == "tense" else beat / 2 * 1.0, beat * 2)
    box_gain = {"full": 0.8 if mood == "tense" else 1.0, "drone": 0.0, "pulse": 0.0, "box": 1.25}.get(bed, 1.0)
    pad_gain = {"box": 0.3, "drone": 0.8}.get(bed, 0.55)
    mix = reverb(p * pad_gain + mb * box_gain, rng)
    if (mood == "tense" and bed != "box") or bed == "pulse":
        mix += heartbeat(t, beat)
    mix += lowpass(rng.standard_normal(len(t)), 5000) * 0.012
    if drop_at is not None:
        gate = np.clip((drop_at - t) / 0.06, 0, 1)
        if resume_at is not None:
            gate = np.maximum(gate, np.clip((t - resume_at) / 1.2, 0, 1) * 0.8)
        mix *= gate
    # the bed fades in and out; the hits come after the fade, so a cold open's hit at 0 s lands at full strength
    mix *= np.clip(t / 1.2, 0, 1) * np.clip((seconds - t) / 1.8, 0, 1)
    for h in hits or []:
        mix += hit(t, h)
    mix /= np.max(np.abs(mix)) + 1e-9
    mix *= 0.85
    d = int(0.012 * SR)
    right = np.concatenate([np.zeros(d), mix[:-d]])
    return np.stack([mix, right], 1).astype(np.float32)


def write_wav(path: Path, st: np.ndarray) -> Path:
    pcm = (np.clip(st, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


# ------------------------------------------------------------------ real tracks (owner 2026-11-08: "same songs")
# The synth score above made every edit sound alike. When the music pack is present (release music-v1, downloaded
# into music/ by the workflow), each episode gets a real track: one it has not used recently, from a fresh point.
# The synth stays as the fallback (no pack, a broken file).
import json as _json
import os as _os
import random as _random
import subprocess as _subprocess

LIBRARY = Path(__file__).resolve().parent / "data" / "music.json"
MUSIC_DIR = Path(_os.environ.get("BOT_MUSIC_DIR") or Path(__file__).resolve().parent.parent / "music")
XFADE = 3.0


def library(mood: str | None = None) -> list[dict]:
    """Manifest tracks whose file is on disk, for this mood (any mood if none matches)."""
    tracks = [t for t in _json.loads(LIBRARY.read_text(encoding="utf-8"))["tracks"] if (MUSIC_DIR / t["file"]).is_file()]
    return [t for t in tracks if t["mood"] == mood] or tracks


def assign(sb: dict, history: list[dict], seed: int, n: int = 1) -> dict:
    """Pick n tracks for the storyboard: never one of the mood's recent tracks until every other one has played.
    Writes sb["music"]["tracks"] / ["track"] and the licence credit into the description. No pack: unchanged."""
    m = sb["music"]
    pool = library(m.get("mood"))
    if not pool:
        return sb
    names = [t["file"] for t in pool]
    recent = [r["music_track"] for r in history if r.get("music_track") in names][-(len(names) - n):] if len(names) > n else []
    rng = _random.Random(seed)
    fresh = [t for t in pool if t["file"] not in recent]
    rng.shuffle(fresh)
    picked = (fresh + [t for t in pool if t not in fresh])[:n]
    m["tracks"] = [t["file"] for t in picked]
    m["track"] = m["tracks"][0]
    credit = _json.loads(LIBRARY.read_text(encoding="utf-8"))["credit"]
    lines = "\n".join("Music: " + credit.format(title=t["title"]) for t in picked)
    sb["description"] = (sb.get("description", "").rstrip() + "\n\n" + lines)[:4500]
    return sb


def _decode(path: Path) -> np.ndarray:
    """A local audio file as stereo float32 at SR (ffmpeg reads local files only)."""
    from . import config  # local: keeps this module importable without config's side effects in tests
    r = _subprocess.run([config.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file",
                         "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"], capture_output=True, timeout=120)
    if r.returncode != 0 or len(r.stdout) < SR * 8:
        raise ValueError(f"unreadable track {path.name}")
    return np.frombuffer(r.stdout, np.float32).reshape(-1, 2)


def library_bed(seconds: float, files: list[str], seed: int, drop_at: float | None = None,
                resume_at: float | None = None) -> np.ndarray:
    """The episode's music from real tracks: a fresh start point in the first track (never the same cut twice),
    the next tracks crossfaded in when the video is longer, faded in and out, the drop / resume gate kept."""
    rng = _random.Random(seed)
    n = int(seconds * SR)
    out = np.zeros((0, 2), np.float32)
    for k, f in enumerate(files):
        a = _decode(MUSIC_DIR / f)
        if k == 0 and len(a) > n + SR * 10:
            start = rng.randint(int(len(a) * 0.05), len(a) - n - SR * 4)   # skip the intro, keep room for the end
            a = a[start:]
        if len(out):
            x = min(int(XFADE * SR), len(out), len(a))
            ramp = np.linspace(0, 1, x, dtype=np.float32)[:, None]
            out[-x:] = out[-x:] * (1 - ramp) + a[:x] * ramp
            a = a[x:]
        out = np.concatenate([out, a])
        if len(out) >= n:
            break
    if len(out) < n:   # shortcut: loops the last tracks when the list is too short, add tracks to music.json
        out = np.concatenate([out] * (n // max(1, len(out)) + 1))
    mix = out[:n].copy()
    t = np.arange(n) / SR
    if drop_at is not None:
        gate = np.clip((drop_at - t) / 0.06, 0, 1)
        if resume_at is not None:
            gate = np.maximum(gate, np.clip((t - resume_at) / 1.2, 0, 1) * 0.8)
        mix *= gate[:, None]
    mix *= (np.clip(t / 0.8, 0, 1) * np.clip((seconds - t) / 1.8, 0, 1))[:, None]
    mix /= np.max(np.abs(mix)) + 1e-9
    return (mix * 0.85).astype(np.float32)
