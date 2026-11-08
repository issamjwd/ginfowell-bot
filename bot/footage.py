"""Gameplay footage: discovery (release manifest or folder scan) and a streaming vertical-crop reader.

Footage never lives in git. CI downloads the `footage-v1` GitHub Release into ./footage (see SETUP.md), made on the
owner's PC by tools/make_footage_release.py. BOT_FOOTAGE_DIR overrides the folder (local dry runs).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)
SAFE_NAME = re.compile(r"^[a-z0-9_]{1,40}$")
SAFE_FILE = re.compile(r"^[a-z0-9_]{1,40}\.mp4$")  # a bare file name: no folders, no "..", no URLs


def folder() -> Path:
    return Path(os.environ.get("BOT_FOOTAGE_DIR") or config.FOOTAGE)


def probe_seconds(path: Path) -> float:
    out = subprocess.run([config.ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    if not m:
        raise ValueError(f"cannot read duration of {path.name}")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def available() -> dict[str, float]:
    """clip name -> seconds. Names are restricted to [a-z0-9_] (they end up in ffmpeg arguments)."""
    d = folder()
    if not d.is_dir():
        return {}
    man = d / "manifest.json"
    clips: dict[str, float] = {}
    if man.exists():
        data = json.loads(man.read_text(encoding="utf-8"))
        for name, info in data.get("clips", {}).items():
            f = str(info.get("file", ""))
            if not (SAFE_NAME.match(name) and SAFE_FILE.match(f)):
                log.warning("manifest entry %r ignored (unsafe name or file)", name)
                continue
            if (d / f).exists():
                clips[name] = float(info["seconds"])
        return clips
    for p in sorted(d.glob("*.mp4")):
        if SAFE_NAME.match(p.stem):
            try:
                clips[p.stem] = probe_seconds(p)
            except ValueError as e:
                log.warning("%s", e)
    return clips


SCARY = ("monster", "scare", "chase")
STEP = 0.25  # seconds per profile sample
BLACK_PIXEL = 16  # 0..255: a pixel darker than this in every channel is black (QA's blackdetect: pix_th 0.04 + margin)


def is_wide() -> bool:
    """True when the footage on disk is the 16:9 pack (manifest "wide": true, tools/make_footage_release.py --wide)."""
    man = folder() / "manifest.json"
    try:
        data = json.loads(man.read_text(encoding="utf-8")) if man.exists() else {}
        return isinstance(data, dict) and bool(data.get("wide"))
    except (OSError, ValueError):
        return False


def tags() -> dict[str, dict]:
    """clip -> {"tags": [...], "peak": seconds or None}: bot/data/footage_tags.json merged with the manifest's own."""
    out: dict[str, dict] = {}
    try:
        data = json.loads((config.DATA / "footage_tags.json").read_text(encoding="utf-8")).get("clips", {})
    except (OSError, ValueError):
        data = {}
    man = folder() / "manifest.json"
    if man.exists():
        try:
            for name, info in json.loads(man.read_text(encoding="utf-8")).get("clips", {}).items():
                if not isinstance(info, dict) or not isinstance(info.get("tags", []), list):
                    continue
                if info.get("tags") or info.get("peak") is not None:
                    data.setdefault(name, {})
                    data[name] = {**data[name], "tags": sorted(set(data[name].get("tags", [])) | set(info.get("tags", []))),
                                  **({"peak": info["peak"]} if info.get("peak") is not None else {})}
        except (OSError, ValueError):
            pass
    for name, info in data.items():
        if SAFE_NAME.match(name):
            peak = info.get("peak")
            ok = isinstance(peak, (int, float)) and not isinstance(peak, bool) and math.isfinite(peak)
            out[name] = {"tags": [str(t) for t in info.get("tags", []) if isinstance(t, str)],
                         "peak": float(peak) if ok else None}
    return out


def cold_open_ok() -> list[str]:
    """Clips scored by eye as a big, sharp monster at the cold-open frame (bot/data/footage_tags.json)."""
    try:
        data = json.loads((config.DATA / "footage_tags.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ok = data.get("cold_open_ok", []) if isinstance(data, dict) else []
    return [c for c in ok if isinstance(c, str) and SAFE_NAME.match(c)]


def is_scary(name: str, tagged: dict[str, dict] | None = None) -> bool:
    return any(t in SCARY for t in (tagged if tagged is not None else tags()).get(name, {}).get("tags", []))


# Clip profiles kept between runs (BOT_PROFILE_CACHE, a JSON file the workflow restores with actions/cache): the long
# video's plan decodes ~60 full 1080p clips just to measure them (~1 minute of the 2-core runner), and a clip's
# numbers never change within a footage release. Keyed by clip name + byte size; anything malformed is ignored.
# Plain module state, no lock: planning is single-threaded.
_DISK: dict[str, list] | None = None
_DISK_NEW = False
_STATS = {"cached": 0, "measured": 0, "seconds": 0.0}


def _disk_path() -> Path | None:
    p = os.environ.get("BOT_PROFILE_CACHE", "")
    return Path(p) if p else None


PROFILE_CACHE_MAX = 5_000_000   # bytes: a release of ~130 clips takes ~200 kB


def _valid_profile(v: object) -> bool:
    return (isinstance(v, list) and len(v) >= 2 and all(
        isinstance(x, list) and len(x) == 2 and all(isinstance(y, (int, float)) and not isinstance(y, bool)
                                                    and math.isfinite(y) and 0.0 <= y <= 1.0 for y in x) for x in v))


def _disk() -> dict[str, list]:
    global _DISK
    if _DISK is None:
        _DISK = {}
        path = _disk_path()
        if path is not None:
            (path.parent / "dirty").unlink(missing_ok=True)
            try:
                ok = path.exists() and path.stat().st_size <= PROFILE_CACHE_MAX
                data = json.loads(path.read_text(encoding="utf-8")) if ok else {}
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict):
                _DISK = {k: v for k, v in data.items() if isinstance(k, str) and _valid_profile(v)}
    return _DISK


def save_profiles() -> None:
    """Write the profile cache back when this run measured new clips (and leave a `dirty` marker next to it, so the
    workflow saves the cache only then)."""
    if _STATS["measured"] or _STATS["cached"]:
        log.info("clip profiles: %d measured (%.0fs), %d from the cache", _STATS["measured"], _STATS["seconds"],
                 _STATS["cached"])
    path = _disk_path()
    if path is None:
        return
    if not _DISK_NEW or _DISK is None:
        (path.parent / "dirty").unlink(missing_ok=True)   # a restored marker must not make the workflow save again
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_DISK, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
        (path.parent / "dirty").write_text("1", encoding="utf-8")
    except OSError as e:
        log.warning("profile cache not saved: %s", e)


@lru_cache(maxsize=256)   # every clip of a release fits (126 in footage-v4): no re-decoding in the long plan
def profile(name: str) -> tuple[tuple[float, float], ...] | None:
    """(motion, brightness) per STEP seconds of a clip, both 0..1, from a tiny greyscale decode (fast). None when the
    clip is not on disk (offline tests). Motion = mean absolute change between samples: the planner uses the most
    dynamic segments and skips dark ones (some clips open on a second of black)."""
    global _DISK_NEW
    try:
        path = clip_path(name)
    except ValueError:
        return None
    if not path.exists():
        return None
    key = f"{name}:{path.stat().st_size}"
    hit = _disk().get(key)
    if hit is not None:
        _STATS["cached"] += 1
        return tuple((float(m), float(b)) for m, b in hit)
    t0 = time.perf_counter()
    out = _measure(path)
    _STATS["measured"] += 1
    _STATS["seconds"] += time.perf_counter() - t0
    if out is not None:
        _disk()[key] = [list(x) for x in out]
        _DISK_NEW = True
    return out


def _measure(path: Path) -> tuple[tuple[float, float], ...] | None:
    w, h = 32, 36
    p = subprocess.run([config.ffmpeg_exe(), "-loglevel", "error", "-i", str(path), "-vf",
                        f"fps={1 / STEP:.0f},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=60)
    raw = p.stdout
    n = len(raw) // (w * h)
    if p.returncode != 0 or n < 2:
        return None
    frames = [np.frombuffer(raw[i * w * h:(i + 1) * w * h], np.uint8).astype(np.float32) / 255 for i in range(n)]
    out = []
    for i, fr in enumerate(frames):
        prev = frames[i - 1] if i else frames[1]
        out.append((float(np.abs(fr - prev).mean()), float(fr.mean())))
    return tuple(out)


def best_start(name: str, seconds: float, clip_s: float, avoid: list[tuple[float, float]] | None = None,
               rng=None, prefer: float | None = None) -> float:
    """Where a `seconds`-long segment of the clip should start: the most dynamic bright window that does not overlap
    `avoid` (segments already used). Falls back to an even spread when the clip has no profile."""
    avoid = avoid or []
    hi = max(0.0, clip_s - seconds - 0.15)
    grid = [round(x * STEP, 2) for x in range(int(hi / STEP) + 1)] or [0.0]
    # the edges of used segments too: a free gap narrower than the grid step still fits a shot
    grid += [math.ceil(round(b, 6) * 1000) / 1000 for _, b in avoid if 0.0 <= b <= hi]
    grid += [math.floor(round(a - seconds, 6) * 1000) / 1000 for a, _ in avoid if 0.0 <= a - seconds <= hi]
    want = None if prefer is None else round(min(hi, max(0.0, prefer - seconds * 0.35)), 2)
    prof = profile(name)

    def free(s0):
        return all(s0 + seconds <= a + 1e-6 or s0 >= b - 1e-6 for a, b in avoid)

    median = sorted(b for _, b in prof)[len(prof) // 2] if prof else 0.0

    def score(s0):
        if not prof:
            return (rng.random() if rng else 0.0)
        i0, i1 = int(s0 / STEP), max(int(s0 / STEP) + 1, int((s0 + seconds) / STEP) + 1)
        seg = prof[i0:i1] or prof[-1:]
        # a fade from black (well under the clip's own brightness) is dead footage, whatever its "motion"
        faded = min(b for _, b in seg) < 0.6 * median
        motion = sum(m for m, _ in seg) / len(seg)
        return motion - (1.0 if faded else 0.0) + (rng.random() * 0.004 if rng else 0.0)

    if want is not None and free(want):
        return want                      # the asked-for moment (a scare's peak), when it is still unused
    ok = [c for c in grid if free(c)] or ([want] if want is not None else grid)
    return max(ok, key=score)


def clip_path(name: str) -> Path:
    if not SAFE_NAME.match(name):
        raise ValueError(f"bad clip name {name!r}")
    d = folder()
    man = d / "manifest.json"
    if man.exists():
        info = json.loads(man.read_text(encoding="utf-8"))["clips"].get(name)
        if info:
            f = str(info.get("file", ""))
            if not SAFE_FILE.match(f):
                raise ValueError(f"unsafe file in footage manifest for {name!r}")
            return d / f
    return d / f"{name}.mp4"


def crop_filter(w: int, h: int, pan: float = 0.0) -> str:
    """ffmpeg crop to the output's aspect (w:h), centred, panned sideways by `pan` (-1..1) when the source is wider.
    9:16 Shorts crop the 960x1080 clips to a vertical strip; the long video's 8:9 panel keeps them whole."""
    pan = max(-1.0, min(1.0, float(pan)))
    cw, ch = f"min(iw,ih*{w}/{h})", f"min(ih,iw*{h}/{w})"
    return f"crop=w='{cw}':h='{ch}':x='(iw-{cw})/2*(1+{pan:.3f})':y='(ih-{ch})/2'"


def is_black(f: np.ndarray) -> bool:
    """A frame QA's blackdetect would call black: 98% of its pixels under 6% (every channel)."""
    return float(np.mean(f.max(axis=2) < BLACK_PIXEL)) >= 0.98


class ClipReader:
    """Streams frames (H x W x 3 uint8, cropped to W:H) of a clip from `start` at `speed`; holds the last frame at the
    end."""

    def __init__(self, name: str, start: float, speed: float, w: int, h: int, fps: int, pan: float = 0.0,
                 freeze: bool = False):
        vf = f"setpts=PTS/{float(speed):.4f},fps={fps},{crop_filter(w, h, pan)},scale={w}:{h}:flags=lanczos"
        self.shape, self.size = (h, w, 3), w * h * 3
        cmd = [config.ffmpeg_exe(), "-loglevel", "error", "-ss", f"{float(start):.3f}", "-i", str(clip_path(name)),
               "-vf", vf, "-an"]
        if freeze:
            cmd += ["-frames:v", "1"]
        self.p = subprocess.Popen(cmd + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        self.last: np.ndarray | None = None
        self.held: np.ndarray | None = None

    def skip_black(self, max_frames: int) -> int:
        """Drop up to max_frames black frames from the start (a recording that opens on black); the first frame with a
        picture is what the next next() returns. Returns how many were dropped."""
        for i in range(max_frames):
            f = self.next()
            if not is_black(f):
                self.held = f
                return i
        return max_frames

    def next(self) -> np.ndarray:
        if self.held is not None:
            f, self.held = self.held, None
            return f
        buf = self.p.stdout.read(self.size) if self.p else b""
        if len(buf) == self.size:
            self.last = np.frombuffer(buf, np.uint8).reshape(self.shape)
        if self.last is None:
            raise RuntimeError("footage produced no frames")
        return self.last

    def close(self) -> None:
        if self.p:
            self.p.kill()
            self.p.wait()
            self.p = None
