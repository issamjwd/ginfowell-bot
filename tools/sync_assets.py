"""Re-sync the cast from the liminality project after the 3D artist changes it (run by hand, then commit).

    python tools/sync_assets.py [--models "C:/Users/User/Desktop/liminality Models"] [--cast-script <path>]

Copies liminality_cast.blend, liminality_palette.png and poses/*.png (the seed sprites) into assets/, and the cast
builder script into blender/. The sprite cache key is a hash of blender/*.py, so the next CI run re-renders every
pose sprite automatically (about 3-10 minutes once, then cached).

    python tools/sync_assets.py --stills-from <recording folder, e.g. C:/Users/Public/liminality_models/footage_v4>

Re-grabs each dream's still (assets/stills/<dream>.jpg, the plate behind drawn scenes and thumbnails) from the
current footage: the frame named in tools/still_picks.json ("clip@seconds"), centre-cropped to the still's own size.
Needs ffmpeg. Use it after a new footage release so stills and thumbnails show the same look as the clips.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = Path.home() / "Desktop" / "liminality Models"
DEFAULT_SCRIPT = Path("C:/Users/Public/liminality_work/tools/blender/liminality_cast.py")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def sync_stills(footage: Path) -> None:
    """assets/stills/<still> <- one frame of the footage (tools/still_picks.json), centre crop, same size, JPEG q90."""
    from PIL import Image

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found")
    picks = json.loads((ROOT / "tools" / "still_picks.json").read_text(encoding="utf-8"))["picks"]
    dreams = {d["id"]: d for d in json.loads((ROOT / "bot" / "data" / "dreams.json").read_text(encoding="utf-8"))["dreams"]}
    for dream, pick in picks.items():
        clip, at = pick.rsplit("@", 1)
        src = footage / f"{clip}.mp4"
        dst = ROOT / "assets" / "stills" / dreams[dream]["still"]
        if not src.exists():
            print(f"missing: {src}")
            continue
        w, h = Image.open(dst).size if dst.exists() else (1920, 710)
        tmp = dst.with_suffix(".grab.png")
        subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-ss", at, "-i", str(src), "-frames:v", "1", str(tmp)],
                       check=True)
        im = Image.open(tmp).convert("RGB")
        s = max(w / im.width, h / im.height)
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        x, y = (im.width - w) // 2, (im.height - h) // 2
        before = sha(dst) if dst.exists() else "-"
        im.crop((x, y, x + w, y + h)).save(dst, quality=90)
        tmp.unlink()
        print(f"{dst.relative_to(ROOT)}: {before} -> {sha(dst)} ({pick})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", type=Path, default=DEFAULT_MODELS)
    ap.add_argument("--cast-script", type=Path, default=DEFAULT_SCRIPT)
    ap.add_argument("--stills-from", type=Path, default=None, help="recording folder: re-grab the dream stills only")
    a = ap.parse_args()
    if a.stills_from:
        sync_stills(a.stills_from)
        print("done. Run the tests, then commit.")
        return 0
    pairs = [(a.models / "liminality_cast.blend", ROOT / "assets" / "liminality_cast.blend"),
             (a.models / "liminality_palette.png", ROOT / "assets" / "liminality_palette.png"),
             (a.cast_script, ROOT / "blender" / "liminality_cast.py")]
    pairs += [(p, ROOT / "assets" / "sprites" / "seed" / p.name) for p in sorted((a.models / "poses").glob("*.png"))]
    for src, dst in pairs:
        if not src.exists():
            print(f"missing: {src}")
            continue
        before = sha(dst) if dst.exists() else "-"
        shutil.copy2(src, dst)
        print(f"{dst.relative_to(ROOT)}: {before} -> {sha(dst)}")
    print("done. Run the tests, then commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
