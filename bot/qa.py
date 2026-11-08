"""QA gates: nothing uploads unless every gate passes.

  * container: 1080x1920 (the long video: 1920x1080), H.264 + yuv420p, an audio stream
  * length: 8 s .. 60 s (Shorts limit); the long video 100 s .. 260 s (config.LONG_MIN/MAX_SECONDS)
  * audio not silent (mean volume above -40 dB)
  * no black run longer than 0.8 s (blackdetect; our darkest card is lifted above the threshold on purpose)
  * no frozen run longer than 2.5 s (freezedetect; animation on twos holds 2 frames, far below that)

Uses ffprobe when present (CI), else parses `ffmpeg -i` (local imageio-ffmpeg has no ffprobe). Filters run on a
downscaled copy so QA stays fast.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config

BLACK_MAX = 0.8
FREEZE_MAX = 2.5
SILENCE_DB = -40.0


@dataclass
class Report:
    ok: bool
    checks: dict[str, str] = field(default_factory=dict)
    info: dict = field(default_factory=dict)

    def as_text(self) -> str:
        return "\n".join(f"{'PASS' if v == 'ok' else 'FAIL'}  {k}: {v}" for k, v in self.checks.items())


def _probe(path: Path) -> dict:
    exe = config.ffprobe_exe()
    if exe:
        p = subprocess.run([exe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise ProbeError(f"ffprobe failed (rc={p.returncode}): {p.stderr.strip()[-300:]}")
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError as e:
            raise ProbeError("ffprobe output is not JSON") from e
        v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
        a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
        return {"width": int(v["width"]) if v else 0, "height": int(v["height"]) if v else 0,
                "vcodec": v.get("codec_name") if v else None, "pix_fmt": v.get("pix_fmt") if v else None,
                "audio": a is not None, "seconds": float(data["format"]["duration"])}
    err = subprocess.run([config.ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    vs = re.search(r"Stream #\S+: Video: (\w+)[^,]*, (\w+)(?:\([^)]*\))?[^,]*, (\d+)x(\d+)", err)
    return {"width": int(vs[3]) if vs else 0, "height": int(vs[4]) if vs else 0,
            "vcodec": vs[1] if vs else None, "pix_fmt": vs[2] if vs else None,
            "audio": bool(re.search(r"Stream #\S+: Audio:", err)),
            "seconds": int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0}


class ProbeError(RuntimeError):
    """ffprobe/ffmpeg could not read the file."""


DETECT = "scale=270:-2,blackdetect=d=0.1:pix_th=0.04:pic_th=0.98,freezedetect=n=0.002:d=0.5"


def _filters(path: Path, grab: tuple[list[int], int, Path] | None = None) -> str:
    """The analysis pass (black / frozen runs, mean volume). grab = (frame numbers, width, folder): the same decode
    also writes those frames as <folder>/<i>.png (the review contact sheet), instead of a second full decode."""
    if grab:
        frames, width, folder = grab
        pick = "+".join(f"eq(n,{n})" for n in sorted(set(frames)))
        cmd = ["-filter_complex", f"[0:v]split=2[q][g];[q]{DETECT}[qo];[g]select='{pick}',scale={width}:-2[go]",
               "-map", "[qo]", "-f", "null", "-",
               "-map", "[go]", "-fps_mode", "passthrough", "-frames:v", str(len(set(frames))), str(folder / "%d.png"),
               "-map", "0:a:0?", "-af", "volumedetect", "-f", "null", "-"]
    else:
        cmd = ["-vf", DETECT, "-af", "volumedetect", "-f", "null", "-"]
    p = subprocess.run([config.ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), *cmd],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise ProbeError(f"ffmpeg analysis failed (rc={p.returncode}): {p.stderr.strip()[-300:]}")
    return p.stderr


def runs(log: str) -> tuple[list[float], list[float], float | None]:
    blacks = [float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", log)]
    freezes = [float(x) for x in re.findall(r"lavfi\.freezedetect\.freeze_duration:\s*([\d.]+)", log)]
    mv = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", log)
    mean = None if not mv or mv[1] == "-inf" else float(mv[1])
    return blacks, freezes, mean


def check(path: Path, width: int = config.W, height: int = config.H, max_seconds: float = config.MAX_SECONDS,
          min_seconds: float = 8.0, grab: tuple[list[int], int, Path] | None = None) -> Report:
    """Shorts: 1080x1920, 8..60 s. The long video passes 1920x1080 and its own limits (config.LONG_*_SECONDS).
    grab: frames to save as PNGs from the same decode (see _filters)."""
    r = Report(ok=True)
    if not path.exists() or path.stat().st_size < 10_000:
        r.ok = False
        r.checks["file"] = "missing or tiny"
        return r
    try:
        p = _probe(path)
    except (ProbeError, KeyError, ValueError, TypeError) as e:
        r.ok = False
        r.checks["probe"] = f"unreadable ({e})"[:300]
        return r
    r.info.update(p)
    r.checks["size"] = "ok" if (p["width"], p["height"]) == (width, height) else f"{p['width']}x{p['height']}"
    r.checks["codec"] = "ok" if p["vcodec"] == "h264" and p["pix_fmt"] == "yuv420p" else f"{p['vcodec']}/{p['pix_fmt']}"
    r.checks["length"] = "ok" if min_seconds <= p["seconds"] <= max_seconds else f"{p['seconds']:.2f}s"
    r.checks["audio stream"] = "ok" if p["audio"] else "none"
    try:
        blacks, freezes, mean = runs(_filters(path, grab))
    except ProbeError as e:
        r.ok = False
        r.checks["analysis"] = str(e)[:300]
        return r
    r.info.update(black_runs=blacks, freeze_runs=freezes, mean_volume=mean)
    r.checks["audio level"] = "ok" if mean is not None and mean > SILENCE_DB else f"mean {mean} dB"
    r.checks["black runs"] = "ok" if all(b <= BLACK_MAX for b in blacks) else f"{max(blacks):.2f}s black"
    r.checks["frozen runs"] = "ok" if all(f <= FREEZE_MAX for f in freezes) else f"{max(freezes):.2f}s frozen"
    r.ok = all(v == "ok" for v in r.checks.values())
    return r
