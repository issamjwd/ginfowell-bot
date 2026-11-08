"""Trim the CC0 sound effects into assets/sfx/ (run by hand when the source set changes, then commit).

    python tools/prepare_sfx.py [--src "C:/Users/User/Desktop/PIXELHELM Social/sfx_cc0"]

Each file is decoded to 44.1 kHz stereo, its leading silence and long quiet tail cut (a short fade-out where the tail
is cut), peak-normalised to -1 dBFS and written as Ogg Vorbis. Risers keep only the build up to their crest (plus a
little after it); bot/sfx.py cuts them exactly on the crest at render time. CREDITS.txt is copied alongside.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config  # noqa: E402

SR = 44100
DEFAULT_SRC = Path.home() / "Desktop" / "PIXELHELM Social" / "sfx_cc0"
OUT = ROOT / "assets" / "sfx"
# name: (longest kept seconds, Vorbis quality). Risers: seconds kept before the crest instead.
SPEC = {
    "whoosh_deep": (1.8, 4), "whoosh_short": (0.6, 4),
    "impact_cinematic": (3.0, 4), "impact_big": (4.5, 4), "impact_punch": (1.6, 4), "impact_downer": (4.5, 4),
    "sub_drop": (3.3, 4), "reverse_swell": (2.0, 4), "glitch_vhs": (1.0, 4), "glitch_corrupt": (1.0, 4),
    "riser_scary": (2.6, 4), "riser_tension": (2.6, 4), "room_tone": (40.0, 2),
}
RISERS = ("riser_scary", "riser_tension")


def decode(path: Path) -> np.ndarray:
    raw = subprocess.run([config.ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar",
                          str(SR), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).astype(np.float64)


def envelope(x: np.ndarray, ms: float) -> np.ndarray:
    k = max(1, int(ms / 1000 * SR))
    return np.convolve(np.abs(x).max(axis=1), np.ones(k) / k, "same")


def trim(name: str, x: np.ndarray) -> np.ndarray:
    keep, _ = SPEC[name]
    env = envelope(x, 50 if name in RISERS else 10)
    pk = env.max()
    if name == "room_tone":
        a = int(5 * SR)                      # skip the recording's first seconds (handling noise)
        return x[a: a + int(keep * SR)]
    if name in RISERS:
        crest = int(np.argmax(env >= 0.7 * pk))   # the build reaches -3 dB of its loudest: the crest
        a, b = max(0, crest - int(keep * SR)), min(len(x), crest + int(0.25 * SR))
    else:
        if pk <= 0:
            raise SystemExit(f"{name}: the source is silent")
        loud = np.nonzero(env > pk * 10 ** (-45 / 20))[0]
        a = max(0, int(loud[0]) - int(0.005 * SR))
        quiet = np.nonzero(env > pk * 10 ** (-55 / 20))[0]
        b = min(int(quiet[-1]) + 1, a + int(keep * SR), len(x))
    y = x[a:b].copy()
    fade = min(len(y) // 4, int(0.08 * SR))
    if b < len(x) and fade:
        y[-fade:] *= np.linspace(1, 0, fade)[:, None]
    return y


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (_, q) in SPEC.items():
        src = a.src / f"{name}.mp3"
        if not src.is_file():
            raise SystemExit(f"missing source file: {src}")
        y = trim(name, decode(src))
        y *= 10 ** (-1 / 20) / (np.abs(y).max() + 1e-12)
        dst = OUT / f"{name}.ogg"
        p = subprocess.run([config.ffmpeg_exe(), "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2",
                            "-i", "-", "-c:a", "libvorbis", "-q:a", str(q), str(dst)],
                           input=y.astype(np.float32).tobytes(), capture_output=True)
        if p.returncode != 0:
            raise SystemExit(f"{name}: {p.stderr.decode(errors='replace')[-300:]}")
        print(f"{name}: {len(y) / SR:.2f} s, {dst.stat().st_size // 1024} KB")
    shutil.copyfile(a.src / "CREDITS.txt", OUT / "CREDITS.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
