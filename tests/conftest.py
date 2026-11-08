"""Shared helpers. Tests use only: plain asserts, tmp_path, monkeypatch, pytest.raises, pytest.mark.parametrize."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import config  # noqa: E402

def _bank_clips() -> dict[str, float]:
    """Every clip the dreams list (bot/data/dreams.json), as if the footage release were downloaded."""
    import json
    data = json.loads((ROOT / "bot" / "data" / "dreams.json").read_text(encoding="utf-8"))
    return {c: 6.9 for d in data["dreams"] for c in d.get("footage", [])}


FAKE_CLIPS = _bank_clips()


def ffmpeg(*args: str) -> None:
    subprocess.run([config.ffmpeg_exe(), "-loglevel", "error", "-y", *args], check=True)


def tiny_clip(path: Path, seconds: float = 10, w: int = 216, h: int = 384, audio: bool = True,
              black: tuple[float, float] | None = None, freeze: tuple[float, float] | None = None) -> Path:
    """A small moving test clip; optional black run / frozen run (start, length) to trip the QA gates."""
    vf = []
    if black:
        vf.append(f"drawbox=c=black:t=fill:enable='between(t,{black[0]},{black[0] + black[1]})'")
    if freeze:
        # freeze by holding one frame: loop a still for the run via the 'select'/'setpts' trick is fiddly;
        # instead overlay a static colour that hides all motion for the run
        vf.append(f"drawbox=c=0x8060a0:t=fill:enable='between(t,{freeze[0]},{freeze[0] + freeze[1]})'")
    args = ["-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=24:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}"]
    if vf:
        args += ["-vf", ",".join(vf)]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    ffmpeg(*args, str(path))
    return path


def plan_legacy(fmt="animated", clips=None, seed=11, history=None, used=None):
    """Plan with the retired templates switched back on (bot/data/templates.json keeps them, disabled): the renderer
    and validator tests still cover the animated scenes, cards and per-shot captions the long video uses."""
    from dataclasses import replace

    from bot import bank, planner
    legacy = tuple(replace(t, enabled=True) for t in bank.templates() if t.id not in planner.edits.STYLES)
    real = bank.templates
    bank.templates = lambda data_dir=None: legacy
    try:
        return planner.plan(history or [], used or set(), fmt, clips if clips is not None else {}, seed=seed)
    finally:
        bank.templates = real
