"""Fonts. Shipped (TTF, OFL, hashes in SHIPPED below): Montserrat for captions (the closest open font to the game's
Gotham), Merriweather as the guaranteed serif. Optional: EB Garamond (the game's title look), fetched in CI from
Google Fonts' GitHub at a pinned commit and verified by sha256.

    python -m bot.fonts pin      # ONCE, by the owner's `pin-fonts` workflow: resolve google/fonts main -> commit,
                                 # download, record sha256 in assets/fonts/fonts.lock.json, commit the lock.
                                 # Fails if ANY file can't be pinned (never writes a half-pinned lock).
    python -m bot.fonts ensure   # every run: download at the pinned commit, refuse any byte that differs.

Until the lock is pinned, `ensure` exits 0 with a warning and titles use Merriweather. This module is stdlib-only
at import time (Pillow is imported lazily) so the pin job runs without installing any package.
No Windows font paths. Local preview only: LOCAL_SERIF_FONT / LOCAL_SERIF_FONT_ITALIC may point at any serif.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

from . import config

log = logging.getLogger(__name__)
LOCK = config.FONTS / "fonts.lock.json"
RAW = "https://raw.githubusercontent.com/{repo}/{commit}/{path}"
COMMITS_API = "https://api.github.com/repos/{repo}/commits/main"
ALLOWED_HOSTS = ("raw.githubusercontent.com", "api.github.com")
SHIPPED = {  # file -> sha256 prefix (checked in tests so a swapped file is caught)
    "Montserrat-Medium.ttf": "20ebfd658f55a256",
    "Montserrat-Bold.ttf": "189aeb285be99f0b",
    "Merriweather-Regular.ttf": "7a703adc03aa1d5d",
    "Merriweather-Italic.ttf": "fc70541c44ac2415",
}
FONT_MAGIC = (bytes([0, 1, 0, 0]), b"OTTO", b"true")


class FontError(RuntimeError):
    """A font could not be pinned or did not match its pinned hash."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _lock() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _get(url: str, timeout: int = 60) -> bytes:
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.netloc not in ALLOWED_HOSTS:
        raise FontError(f"refusing to fetch fonts from {u.netloc}")
    req = urllib.request.Request(url, headers={"User-Agent": "pixelhelm-channel-bot"})
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as r:  # noqa: S310 - allow-listed
        return r.read()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def is_pinned(lock: dict | None = None) -> bool:
    lock = lock or _lock()
    return bool(lock.get("commit")) and all(f.get("sha256") for f in lock["files"].values())


def ensure() -> list[Path]:
    """Fetch/verify the pinned optional fonts. Unpinned lock -> [] (titles use Merriweather). Mismatch -> FontError."""
    lock = _lock()
    if not is_pinned(lock):
        log.warning("fonts.lock.json is not pinned yet (run the pin-fonts workflow once); using Merriweather")
        return []
    out = []
    for name, f in lock["files"].items():
        dest = config.FONTS / name
        if not dest.exists():
            data = _get(RAW.format(repo=lock["repo"], commit=lock["commit"], path=urllib.parse.quote(f["path"])))
            if sha256(data) != f["sha256"]:
                raise FontError(f"{name}: sha256 mismatch, refusing to use it")
            dest.write_bytes(data)
        elif sha256(dest.read_bytes()) != f["sha256"]:
            raise FontError(f"{name}: local file does not match the lock")
        out.append(dest)
    return out


def pin() -> dict:
    """Resolve + download + hash every file; write the lock only if ALL succeed."""
    lock = _lock()
    commit = json.loads(_get(COMMITS_API.format(repo=lock["repo"])))["sha"]
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise FontError("unexpected commit id from GitHub")
    hashes = {}
    for name, f in lock["files"].items():
        try:
            data = _get(RAW.format(repo=lock["repo"], commit=commit, path=urllib.parse.quote(f["path"])))
        except OSError as e:
            raise FontError(f"{name}: download failed ({e}); lock left unpinned") from e
        if len(data) < 10_000 or data[:4] not in FONT_MAGIC:
            raise FontError(f"{name}: not a font file; lock left unpinned")
        hashes[name] = sha256(data)
    for name, h in hashes.items():
        lock["files"][name]["sha256"] = h
    lock["commit"] = commit
    LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8", newline="\n")
    return lock


# ------------------------------------------------------------------------------------------------ use
def _load(path: Path, size: int, style: str | None = None):
    from PIL import ImageFont
    f = ImageFont.truetype(str(path), size)
    if style:
        try:
            f.set_variation_by_name(style)
        except (OSError, ValueError):
            pass  # static font: keep it as is
    return f


@lru_cache(maxsize=64)
def sans(size: int, weight: str = "bold"):
    """Montserrat Bold (captions) or Medium (small text), as named in assets/style.json. Shipped TTF."""
    from . import style
    return _load(style.body_font("700" if weight == "bold" else "500"), size)


@lru_cache(maxsize=64)
def serif(size: int, italic: bool = False):
    local = os.environ.get("LOCAL_SERIF_FONT_ITALIC" if italic else "LOCAL_SERIF_FONT")
    if local and Path(local).exists():
        return _load(Path(local), size)
    if not italic:  # the heading font from assets/style.json (shared with the Remotion overlay)
        from . import style
        head = style.head_font()
        return _load(head, size, "Regular" if head.name.startswith("EBGaramond") else None)
    garamond = config.FONTS / "EBGaramond-Italic.ttf"
    if garamond.exists():
        return _load(garamond, size, "Regular")
    return _load(config.FONTS / "Merriweather-Italic.ttf", size)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ensure"
    if cmd == "pin":
        print(json.dumps(pin(), indent=2))
    elif cmd == "ensure":
        print("fonts ok:", [p.name for p in ensure()])
    else:
        raise SystemExit("usage: python -m bot.fonts ensure|pin")
