"""On-screen text style shared with the Remotion overlay: assets/style.json is the single source of fonts, colours
and caption layout. Python (Pillow captions and cards, the burned fallback) and render/render.mjs both read it.
Stdlib only.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import config

STYLE_FILE = config.ASSETS / "style.json"


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(STYLE_FILE.read_text(encoding="utf-8"))


def rgb(name: str) -> tuple[int, int, int]:
    """A named colour from style.json as an (r, g, b) tuple."""
    h = load()["colours"][name].lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def head_font(folder: Path | None = None) -> Path:
    """The first heading font in style.json that exists (EB Garamond when CI fetched it, else Merriweather)."""
    folder = folder or config.FONTS
    files = load()["fonts"]["head"]["files"]
    for name in files:
        if (folder / name).exists():
            return folder / name
    return folder / files[-1]


def body_font(weight: str = "700", folder: Path | None = None) -> Path:
    return (folder or config.FONTS) / load()["fonts"]["body"]["files"][weight]


def caption() -> dict:
    return dict(load()["caption"])
