"""Character pose sprites: rendered by headless Blender (Cycles) from the rigged cast, cached by content hash.

Animation is "on twos": each drawing is held for 2 output frames at 24 fps (12 drawings/s), which reads as hand-drawn
and halves the number of renders. An action is a short list of (pose, weight) drawings from the cast's pose library;
weight scales the pose angles, so 0.35 is an in-between and -1.0 swings the limbs the other way (the passing step of a
walk). One fixed camera per character keeps cycles from jittering.

If Blender is missing or fails, the 12 seed sprites in assets/sprites/seed (the specialist's renders) are used: a walk
becomes walk_contact + its mirror, other actions hold their one drawing with a tiny squash. The video still ships.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

from . import config

log = logging.getLogger(__name__)

CHARACTERS = ("Dreamer", "Mannequin", "Bear", "Neighbour")

# action -> drawings (pose, weight). Cycles loop; one-shots hold their last drawing.
ACTIONS: dict[str, list[tuple[str, float]]] = {
    "idle": [("idle", 1.0), ("idle", 0.82)],
    "walk": [("walk_contact", 1.0), ("walk_contact", 0.35), ("walk_contact", -1.0), ("walk_contact", -0.35)],
    "wave": [("wave", 0.35), ("wave", 0.7), ("wave", 1.0), ("wave", 0.8)],
    "peek": [("peek", 0.4), ("peek", 0.8), ("peek", 1.0)],
    "reach": [("reach", 0.3), ("reach", 0.65), ("reach", 1.0)],
    "sit": [("sit", 1.0)],
}
LOOPING = {"idle", "walk", "wave"}

SEED = {  # what the seed folder holds (weight 1.0 only)
    "Dreamer": ("idle", "peek", "sit"),
    "Mannequin": ("idle", "walk_contact", "reach"),
    "Bear": ("idle", "sit", "peek"),
    "Neighbour": ("idle", "wave", "walk_contact"),
}


def sprite_file(character: str, pose: str, weight: float) -> str:
    return f"{character}_{pose}_{weight:+.2f}.png"


def request(res: int = 720, samples: int = 24) -> dict:
    frames = []
    for ch in CHARACTERS:
        seen = set()
        for drawings in ACTIONS.values():
            for pose, wgt in drawings:
                f = sprite_file(ch, pose, wgt)
                if f not in seen:
                    seen.add(f)
                    frames.append({"character": ch, "pose": pose, "weight": wgt, "file": f})
    return {"res": res, "samples": samples, "frames": frames}


def cache_key(req: dict) -> str:
    h = hashlib.sha256()
    for p in (config.ROOT / "blender" / "liminality_cast.py", config.ROOT / "blender" / "render_sprites.py"):
        h.update(p.read_bytes())
    h.update(json.dumps(req, sort_keys=True).encode())
    return h.hexdigest()[:16]


def cache_dir(req: dict) -> Path:
    return config.SPRITE_CACHE / cache_key(req)


def ensure_sprites(blender: str, res: int = 720, samples: int = 24, timeout: int = 3600) -> Path | None:
    """Render every sprite into the content-addressed cache once. Returns the folder, or None if unavailable."""
    req = request(res, samples)
    out = cache_dir(req)
    marker = out / "DONE"
    if marker.exists():
        return out
    if not blender:
        log.warning("no Blender: animated shorts will use the seed sprites")
        return None
    if out.exists():  # a half-finished folder from an earlier failure: start clean
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    req_path = out / "request.json"
    req_path.write_text(json.dumps(req, indent=1), encoding="utf-8")
    cmd = [blender, "-b", "--factory-startup", "-P", str(config.ROOT / "blender" / "render_sprites.py"), "--",
           str(req_path), str(out)]
    log.info("rendering %d sprites with Blender (Cycles CPU)...", len(req["frames"]))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error("Blender failed to run: %s", e)
        shutil.rmtree(out, ignore_errors=True)  # never leave a partial folder for the cache to save
        return None
    missing = [f["file"] for f in req["frames"] if not (out / f["file"]).exists()]
    if proc.returncode != 0 or missing:
        log.error("Blender sprite render failed (rc=%s, %d missing). tail:\n%s", proc.returncode, len(missing),
                  (proc.stdout + proc.stderr)[-3000:])
        shutil.rmtree(out, ignore_errors=True)
        return None
    marker.write_text(cache_key(req), encoding="utf-8")
    return out


def _trim(im: Image.Image) -> Image.Image:
    box = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    return im.crop(box) if box else im


@lru_cache(maxsize=256)
def _load(path: str) -> Image.Image:
    return Image.open(path).convert("RGBA")


def drawings(character: str, action: str, folder: Path | None) -> list[Image.Image]:
    """The ordered drawings of an action as uncropped RGBA images (same canvas => same feet line)."""
    if character not in CHARACTERS or action not in ACTIONS:
        raise ValueError(f"unknown sprite {character}/{action}")
    if folder is not None:
        paths = [folder / sprite_file(character, p, w) for p, w in ACTIONS[action]]
        if all(p.exists() for p in paths):
            return [_load(str(p)) for p in paths]
    return _seed_drawings(character, action)


def _seed_drawings(character: str, action: str) -> list[Image.Image]:
    have = SEED[character]

    def seed(pose: str) -> Image.Image:
        return _load(str(config.SEED_SPRITES / f"{character}_{pose}.png"))

    if action == "walk":
        if "walk_contact" in have:
            a = seed("walk_contact")
            return [a, a, ImageOps.mirror(a), ImageOps.mirror(a)]
        a = seed("idle")
        return [a, _squash(a, 0.985)]
    pose = {"idle": "idle", "wave": "wave", "peek": "peek", "reach": "reach", "sit": "sit"}[action]
    base = seed(pose) if pose in have else seed("idle")
    return [base, _squash(base, 0.985)] if action in LOOPING else [base]


def _squash(im: Image.Image, k: float) -> Image.Image:
    """Tiny breathing squash, feet kept on the same line."""
    w, h = im.size
    s = im.resize((int(w * (2 - k)), int(h * k)), Image.LANCZOS)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.alpha_composite(s, ((w - s.width) // 2, h - s.height))
    return out


def trimmed(im: Image.Image) -> Image.Image:
    return _trim(im)
