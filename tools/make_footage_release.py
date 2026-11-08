"""Make the small gameplay-footage pack the owner attaches to the GitHub Release `footage-v1`. Runs on the owner's PC,
once, by hand (the only local step; nothing is scheduled on the PC).

    python tools/make_footage_release.py <source folder of .mp4> <output folder> [--only name1,name2]
        [--keep-from <old release folder> --keep name1,name2]   (old clips carried over unchanged)

For each clip: centre-crop to 960x1080 (the 9:16 window the bot uses, plus room to pan left/right), no upscaling,
H.264 CRF 26, 24 fps, no audio, +faststart. Then writes manifest.json (name, file, seconds, sha256, bytes, and from
the source folder's own manifest.json, when it has one: tags, peak, level). Clips tagged "hud" (game UI on screen)
are skipped.
Clip names must be lowercase letters/digits/underscore (they become ffmpeg arguments in CI).

Then: GitHub repo -> Releases -> Draft a new release -> tag `footage-v1` -> drag in every .mp4 AND manifest.json ->
Publish. The workflow downloads it with the built-in GITHUB_TOKEN (SETUP.md step 9).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SAFE = re.compile(r"^[a-z0-9_]{1,40}$")


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found: install ffmpeg or `pip install imageio-ffmpeg`")


def seconds(path: Path) -> float:
    err = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if not m:
        raise ValueError(f"no duration for {path}")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def transcode(src: Path, dst: Path, wide: bool = False) -> None:
    """Shorts pack: the centre 8:9 strip at full height. Wide pack (--wide, the 16:9 long video): the whole frame,
    at most 1920x1080, a little higher quality (it is shown full screen)."""
    if wide:
        vf, crf = "scale='min(1920,iw)':-2:flags=lanczos,fps=24", "23"
    else:
        vf, crf = "crop=w='min(iw,trunc(ih*8/9/2)*2)':h=ih:x='(iw-min(iw,trunc(ih*8/9/2)*2))/2':y=0,fps=24", "26"
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-i", str(src), "-vf", vf, "-an", "-c:v", "libx264",
                    "-preset", "slow", "-crf", crf, "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)],
                   check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("out")
    ap.add_argument("--only", default="")
    ap.add_argument("--keep-from", default="")
    ap.add_argument("--keep", default="")
    ap.add_argument("--wide", action="store_true", help="16:9 pack for the long video (no crop)")
    ap.add_argument("--peaks", default="", help="JSON {\"peaks\": {clip: seconds}} for clips the recording has none")
    a = ap.parse_args()
    src, out = Path(a.source), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    only = {x for x in a.only.split(",") if x}
    source_meta = {}
    if (src / "manifest.json").exists():
        source_meta = json.loads((src / "manifest.json").read_text(encoding="utf-8")).get("clips", {})
    manifest = {"version": 2, "wide": bool(a.wide), "clips": {}}
    extra_peaks = json.loads(Path(a.peaks).read_text(encoding="utf-8")).get("peaks", {}) if a.peaks else {}
    for p in sorted(src.glob("*.mp4")):
        name = p.stem.lower()
        if only and name not in only:
            continue
        if not SAFE.match(name):
            print(f"skip {p.name}: rename it to lowercase letters/digits/underscore")
            continue
        meta = source_meta.get(p.stem) or source_meta.get(name) or {}
        tags = [t for t in meta.get("tags", []) if isinstance(t, str) and re.fullmatch(r"[a-z0-9=_-]{1,32}", t)]
        if "hud" in tags:
            print(f"skip {p.name}: game UI on screen (hud)")
            continue
        dst = out / f"{name}.mp4"
        transcode(p, dst, wide=a.wide)
        data = dst.read_bytes()
        manifest["clips"][name] = {"file": dst.name, "seconds": round(seconds(dst), 3),
                                   "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        if tags:
            manifest["clips"][name]["tags"] = tags
        peak = meta.get("peak") if isinstance(meta.get("peak"), (int, float)) else extra_peaks.get(name)
        if isinstance(peak, (int, float)):
            manifest["clips"][name]["peak"] = float(peak)
        if isinstance(meta.get("level"), str) and SAFE.match(meta["level"]):
            manifest["clips"][name]["level"] = meta["level"]
        print(f"{name}: {p.stat().st_size / 1e6:.1f} MB -> {len(data) / 1e6:.1f} MB")
    if a.keep_from:
        old_dir = Path(a.keep_from)
        old = json.loads((old_dir / "manifest.json").read_text(encoding="utf-8")).get("clips", {})
        for name in [x for x in a.keep.split(",") if x]:
            info = old.get(name)
            if not info or not SAFE.match(name) or name in manifest["clips"]:
                print(f"skip kept clip {name!r}")
                continue
            shutil.copyfile(old_dir / info["file"], out / info["file"])
            manifest["clips"][name] = {k: v for k, v in info.items() if k in ("file", "seconds", "sha256", "bytes")}
            print(f"{name}: kept from {old_dir.name}")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(manifest['clips'])} clips + manifest.json to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
