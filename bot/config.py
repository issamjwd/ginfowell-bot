"""Paths and settings. Everything secret comes from the environment (GitHub Actions secrets), never from files."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "data"
ASSETS = ROOT / "assets"
STILLS = ASSETS / "stills"
SEED_SPRITES = ASSETS / "sprites" / "seed"
SPRITE_CACHE = ASSETS / "sprites" / "cache"
FONTS = ASSETS / "fonts"
FOOTAGE = ROOT / "footage"  # filled in CI from the footage-v1 release (never committed)
STATE = ROOT / "state" / "episodes.jsonl"
OUT = ROOT / "out"

W, H = 1080, 1920  # YouTube Shorts frame
FPS = 24           # output rate; animation is drawn "on twos" (12 drawings per second)
MAX_SECONDS = 60.0
# The daily long video (normal 16:9 upload, not a Short): 1920x1080, about 2-4 minutes.
LONG_W, LONG_H = 1920, 1080
LONG_MIN_SECONDS, LONG_MAX_SECONDS = 100.0, 260.0

VALID_PRIVACY = ("private", "unlisted", "public")
VALID_FORMATS = ("footage", "animated", "auto")
VALID_KINDS = ("short", "long")  # BOT_KIND: a 9:16 Short or the 16:9 long video
VALID_OVERLAYS = ("auto", "off")  # auto: Remotion text overlay when node + render/node_modules + Chrome exist


class ConfigError(ValueError):
    """Bad or missing configuration."""


def ffmpeg_exe() -> str:
    """System ffmpeg (CI: apt), else the imageio-ffmpeg wheel's binary (local dev)."""
    exe = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
    except ImportError as e:  # pragma: no cover - depends on machine
        raise ConfigError("ffmpeg not found: install it (apt-get install ffmpeg) or pip install imageio-ffmpeg") from e
    return imageio_ffmpeg.get_ffmpeg_exe()


def ffprobe_exe() -> str | None:
    """ffprobe if present. QA falls back to parsing `ffmpeg -i` when it is not (imageio wheel has no ffprobe)."""
    return os.environ.get("FFPROBE") or shutil.which("ffprobe")


@dataclass(frozen=True)
class Settings:
    privacy: str = "private"
    dry_run: bool = True
    fmt: str = "auto"
    ai_video_provider: str = ""
    ai_video_key_set: bool = False
    anthropic_key_set: bool = False
    blender: str = ""
    low_res: bool = False
    enabled: bool = False          # repo variable BOT_ENABLED: the master switch for anything live
    in_actions: bool = False       # GITHUB_ACTIONS=true: live work only ever runs on GitHub's runners
    overlay: str = "auto"          # BOT_OVERLAY: auto (Remotion text overlay, burned captions as fallback) | off
    kind: str = "short"            # BOT_KIND: short (1080x1920 Short) | long (1920x1080 video)
    template: str = ""             # BOT_TEMPLATE: force a Short style (the workflow sets it on dry runs only)
    extra: dict = field(default_factory=dict)

    @staticmethod
    def from_env(env: dict | None = None) -> "Settings":
        e = os.environ if env is None else env
        privacy = (e.get("YT_PRIVACY") or "private").strip().lower()
        if privacy not in VALID_PRIVACY:
            raise ConfigError(f"YT_PRIVACY must be one of {VALID_PRIVACY}, got {privacy!r}")
        fmt = (e.get("BOT_FORMAT") or "auto").strip().lower()
        if fmt not in VALID_FORMATS:
            raise ConfigError(f"BOT_FORMAT must be one of {VALID_FORMATS}, got {fmt!r}")
        overlay = (e.get("BOT_OVERLAY") or "auto").strip().lower()
        if overlay not in VALID_OVERLAYS:
            raise ConfigError(f"BOT_OVERLAY must be one of {VALID_OVERLAYS}, got {overlay!r}")
        kind = (e.get("BOT_KIND") or "short").strip().lower()
        if kind not in VALID_KINDS:
            raise ConfigError(f"BOT_KIND must be one of {VALID_KINDS}, got {kind!r}")
        dry = (e.get("DRY_RUN") or "true").strip().lower() not in ("0", "false", "no")
        template = (e.get("BOT_TEMPLATE") or "").strip().lower()
        if template and not re.fullmatch(r"[a-z_]{1,40}", template):
            raise ConfigError(f"BOT_TEMPLATE must be a style id, got {template!r}")
        return Settings(
            privacy=privacy,
            dry_run=dry,
            fmt=fmt,
            ai_video_provider=(e.get("AI_VIDEO_PROVIDER") or "").strip().lower(),
            ai_video_key_set=bool(e.get("AI_VIDEO_API_KEY")),
            anthropic_key_set=bool(e.get("ANTHROPIC_API_KEY")),
            blender=e.get("BLENDER") or shutil.which("blender") or "",
            low_res=(e.get("BOT_LOW_RES") or "").lower() in ("1", "true", "yes"),
            enabled=(e.get("BOT_ENABLED") or "").strip().lower() == "true",
            in_actions=(e.get("GITHUB_ACTIONS") or "").strip().lower() == "true",
            overlay=overlay,
            kind=kind,
            template=template,
        )
