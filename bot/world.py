"""World Shorts: the channel's Shorts about things other than Roblox, made only from pictures the bot may reuse.

Four kinds, in turn:
  * world_clips    real video clips from NASA's video library (launches, spacewalks, Earth from orbit...), public
                   domain, the busiest few seconds of each; same credit rules as world_space
  * world_space    real NASA photos of one subject (NASA's image library: NASA media is not copyrighted in the US;
                   any item credited to someone other than NASA is skipped)
  * world_history  "what happened on <today>": events from Wikipedia's "on this day" feed (text CC BY-SA, credited),
                   each with its article's picture, ONLY when that picture is on Wikimedia Commons under public
                   domain / CC0 / CC BY / CC BY-SA (author + licence shown on screen and in the description)
  * world_nature   unusual animals or natural wonders from a hand-picked list (bot/data/world_nature.json): the
                   article's summary sentence (CC BY-SA, credited) and its picture, on the same Commons terms
Never clips or footage from other creators.

Network: https only (NASA's http asset links are upgraded), only the hosts in HOSTS, redirects never followed,
size-capped, and every image is opened by Pillow before use. The User-Agent names the bot and its public page.

The video: each picture in a solid frame with a slow push-in, one caption under it, a credit line, a short outro;
the bot's own synthesized music (bot/music.py). Same QA (bot/qa.py) and hand-off files as every other Short.
"""
from __future__ import annotations

import html
import io
import os
import json
import logging
import random
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import config, fonts, music, palette, qa, style, voice
from .state import fingerprint

log = logging.getLogger(__name__)

HOSTS = {"images-api.nasa.gov", "images-assets.nasa.gov", "en.wikipedia.org", "commons.wikimedia.org",
         "upload.wikimedia.org", "thumb.wikimedia.org"}
UPGRADE_HTTP = {"images-api.nasa.gov", "images-assets.nasa.gov"}
UA = "PIXELHELM-channel-bot/1.0 (https://issamjwd.github.io/pixelhelm-channel-bot-site)"
MAX_JSON = 4 * 1024 * 1024
MAX_IMAGE = 15 * 1024 * 1024
GAME_URL = "https://www.roblox.com/games/120735295431220/Liminality"
SCENES = 5
MIN_SCENES = 3

# (search words, how the subject reads on screen, words one of which must appear in the item)
SPACE_TOPICS = [
    ("saturn cassini", "Saturn", ("saturn",)),
    ("jupiter juno", "Jupiter", ("jupiter",)),
    ("mars surface rover", "Mars", ("mars",)),
    ("nebula hubble", "nebulas", ("nebula",)),
    ("galaxy hubble", "galaxies", ("galaxy", "galaxies")),
    ("aurora from space station", "auroras seen from space", ("aurora",)),
    ("earth from space station", "Earth from orbit", ("earth",)),
    ("apollo moon surface", "the Moon", ("moon", "lunar")),
    ("solar flare sun", "the Sun", ("sun", "solar")),
    ("star cluster", "star clusters", ("cluster",)),
    ("pluto new horizons", "Pluto", ("pluto",)),
    ("comet", "comets", ("comet",)),
    ("supernova remnant", "supernova remnants", ("supernova",)),
    ("hurricane from space", "hurricanes from space", ("hurricane",)),
    ("webb telescope", "the Webb telescope's view", ("webb",)),
]
SPACE_HOOKS = ("Real NASA photos of {name}", "{name}, as NASA actually saw it", "{n} real images of {name}",
               "{name}, photographed by NASA")
# grim events are left out: tragedies don't suit the channel and limit ads
GRIM = re.compile(r"(?i)\b(kill|killed|killing|massacre|bomb|bombing|shoot|shooting|shot|murder|terror|genocide|"
                  r"execut\w*|assassinat\w*|attack\w*|died|death|deaths|dead|disaster|crash\w*|hostage|riot\w*|"
                  r"lynch\w*|rape\w*|holocaust|nazi\w*|suicide|war crime\w*|casualt\w*|explosion|sank|sinking)\b")
# labelled maps, charts and drawings read badly on a phone: photos only
NOT_PHOTO = re.compile(r"(?i)\b(map|maps|mapped|illustration|diagram|chart|graph|plot|names|labeled|labelled|"
                       r"artist'?s? (concept|rendering|impression)|animation|infographic|logo|poster|patch|"
                       r"simulation|model|schematic|data|stereo|anaglyph|3-d|labels)\b")
FREE_LICENCE = re.compile(r"^(public domain|pd[- ].*|cc0.*|cc by(-sa)? \d(\.\d)?.*)$", re.I)
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


class WorldError(RuntimeError):
    """No world Short could be made this run (the caller falls back to a liminality Short)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only to another allowed host (Wikipedia sends renamed articles to the new title)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            newurl = check_url(urllib.parse.urljoin(req.full_url, newurl))
        except WorldError:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url: str) -> str:
    u = urllib.parse.urlparse(url)
    if u.scheme == "http" and u.netloc in UPGRADE_HTTP:
        u = u._replace(scheme="https")
    if u.scheme != "https" or u.netloc not in HOSTS or u.username or u.password or u.port:
        raise WorldError(f"URL host {u.netloc!r} is not allowed")
    return urllib.parse.urlunparse(u)


def fetch(url: str, max_bytes: int = MAX_JSON, timeout: int = 30) -> bytes:
    req = urllib.request.Request(check_url(url), headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as r:  # noqa: S310 - checked
            data = r.read(max_bytes + 1)
    except OSError as e:
        raise WorldError(f"fetch failed: {type(e).__name__}: {e}"[:300]) from e
    if len(data) > max_bytes:
        raise WorldError("response too big")
    return data


def fetch_json(url: str) -> dict:
    try:
        data = json.loads(fetch(url).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise WorldError("not JSON") from e
    if not isinstance(data, dict):
        raise WorldError("unexpected JSON")
    return data


def fetch_image(url: str) -> Image.Image:
    data = fetch(url, MAX_IMAGE, timeout=60)
    try:
        im = Image.open(io.BytesIO(data))
        if im.format not in ("JPEG", "PNG", "WEBP"):
            raise WorldError(f"not a photo ({im.format})")
        im.load()
    except (OSError, Image.DecompressionBombError) as e:
        raise WorldError(f"unreadable image: {e}"[:200]) from e
    im = im.convert("RGB")
    if min(im.size) < 320:
        raise WorldError("image too small")
    im.thumbnail((1600, 1600), Image.LANCZOS)
    return im


def clean(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s*\((pictured|shown|depicted|illustrated)[^)]*\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s+(?=[A-Z])", clean(text), maxsplit=1)[0].strip()


def words(text: str) -> int:
    return len(text.split())


@dataclass
class Scene:
    image: Image.Image | None
    text: str
    big: str = ""          # the year on history scenes
    credit: str = ""
    seconds: float = 4.5
    media_id: str = ""
    clip: Path | None = None   # a video scene: this file from clip_start plays in the frame instead of a still
    clip_start: float = 0.0
    clip_dur: float = 0.0


def read_seconds(text: str) -> float:
    return round(min(6.5, max(3.8, 1.6 + words(text) * 0.3)), 2)


# ---------------------------------------------------------------------------------------------------------- space
def nasa_ok(data: dict, need: tuple[str, ...]) -> str | None:
    """The caption for a NASA library item, or None when the item can't be used (credited to someone else,
    off-subject, or no usable sentence)."""
    desc = clean(data.get("description") or data.get("description_508") or "")
    blob = " ".join([desc, str(data.get("title") or ""), " ".join(map(str, data.get("keywords") or []))]).lower()
    if not any(w in blob for w in need):
        return None
    if re.search(r"(?i)©|copyright|courtesy", desc) or NOT_PHOTO.search(f"{desc} {data.get('title') or ''}"):
        return None
    for who in (data.get("photographer"), data.get("secondary_creator")):
        if who and "nasa" not in str(who).lower():
            return None
    for credit in re.findall(r"(?i)credits?\s*:\s*([^\n]{0,80})", desc):
        if "nasa" not in credit.lower():
            return None
    s = re.sub(r"^[A-Z][A-Z .,'()-]{3,60}?\s+-+\s+", "", first_sentence(desc))  # "KENNEDY SPACE CENTER, FLA. -"
    if "http" in s or not 6 <= words(s) <= 26 or not s.endswith((".", "!", "?")):
        return None
    return s


def nasa_image_url(nasa_id: str) -> str:
    q = urllib.parse.quote(nasa_id)
    items = fetch_json(f"https://images-api.nasa.gov/asset/{q}").get("collection", {}).get("items", [])
    hrefs = [str(i.get("href", "")) for i in items if isinstance(i, dict)]
    for size in ("~large.jpg", "~medium.jpg", "~orig.jpg"):
        for h in hrefs:
            if h.lower().endswith(size):
                return h
    raise WorldError(f"no usable picture size for {nasa_id}")


def plan_space(rng: random.Random, recent_ids: set[str], recent_topics: set[str]) -> dict:
    topics = [t for t in SPACE_TOPICS if t[1] not in recent_topics] or list(SPACE_TOPICS)
    rng.shuffle(topics)
    for query, name, need in topics[:4]:
        q = urllib.parse.urlencode({"q": query, "media_type": "image", "page_size": 100})
        try:
            items = fetch_json(f"https://images-api.nasa.gov/search?{q}").get("collection", {}).get("items", [])
        except WorldError as e:
            log.warning("NASA search %r failed: %s", query, e)
            continue
        cands = []
        for it in items:
            data = (it.get("data") or [{}])[0] if isinstance(it, dict) else {}
            nid = str(data.get("nasa_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,80}", nid) or nid in recent_ids:
                continue
            cap = nasa_ok(data, need)
            if cap:
                cands.append((nid, cap))
        rng.shuffle(cands)
        scenes, seen = [], set()
        for nid, cap in cands:
            if len(scenes) == SCENES:
                break
            if cap in seen:
                continue
            try:
                im = fetch_image(nasa_image_url(nid))
            except WorldError as e:
                log.info("skip %s: %s", nid, e)
                continue
            seen.add(cap)
            scenes.append(Scene(im, cap, credit=f"NASA  ·  {nid}", seconds=read_seconds(cap), media_id=nid))
        if len(scenes) < MIN_SCENES:
            continue
        hook = rng.choice(SPACE_HOOKS).format(name=name, n=len(scenes))
        hook = hook[0].upper() + hook[1:]
        ids = [s.media_id for s in scenes]
        return {
            "template": "world_space", "topic": name, "hook": hook, "label": "FROM NASA'S ARCHIVE",
            "scenes": scenes, "title": f"{hook} #shorts", "category_id": "28",
            "tags": ["space", "nasa", "astronomy", name.lower(), "science", "shorts"],
            "credits": "Images: NASA (public domain), image library ids: " + ", ".join(ids)
                       + ".\nNot affiliated with or endorsed by NASA.",
        }
    raise WorldError("no space subject had enough usable NASA pictures")


# -------------------------------------------------------------------------------------------------------- history
def commons_file(url: str) -> str | None:
    u = urllib.parse.urlparse(url)
    parts = u.path.split("/")
    if u.netloc != "upload.wikimedia.org" or parts[1:3] != ["wikipedia", "commons"]:
        return None  # an English-Wikipedia-only file may be non-free: never used
    name = parts[6] if len(parts) > 6 and parts[3] == "thumb" else (parts[5] if len(parts) > 5 else "")
    name = urllib.parse.unquote(name)
    return name if re.fullmatch(r"[^/|#<>\[\]{}]{1,200}\.(jpe?g|png)", name, re.I) else None


def commons_info(names: list[str]) -> dict[str, dict]:
    """File name -> {"url", "licence", "artist"} for the files under a free licence."""
    out: dict[str, dict] = {}
    for i in range(0, len(names), 40):
        chunk = names[i:i + 40]
        q = urllib.parse.urlencode({"action": "query", "format": "json", "prop": "imageinfo",
                                    "iiprop": "extmetadata|url|mime", "iiurlwidth": 1400,
                                    "titles": "|".join("File:" + n for n in chunk)})
        data = fetch_json(f"https://commons.wikimedia.org/w/api.php?{q}").get("query", {})
        norm = {n["to"]: n["from"] for n in data.get("normalized", []) if isinstance(n, dict)}
        for page in (data.get("pages") or {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            licence = clean(str((meta.get("LicenseShortName") or {}).get("value", "")))
            if not FREE_LICENCE.match(licence) or info.get("mime") not in ("image/jpeg", "image/png"):
                continue
            artist = clean(str((meta.get("Artist") or {}).get("value", ""))) or "unknown author"
            title = str(page.get("title", ""))
            name = norm.get(title, title).removeprefix("File:").replace(" ", "_")
            url = str(info.get("thumburl") or info.get("url") or "").split("?")[0]
            out[name] = {"url": url, "licence": licence, "artist": artist[:60]}
    return out


def history_events(feed: dict) -> list[dict]:
    """Usable events: short, not grim, with a Commons picture. Each: year, text, file, page."""
    out = []
    for e in list(feed.get("selected") or []) + list(feed.get("events") or []):
        text, year = clean(str(e.get("text", ""))), e.get("year")
        if not isinstance(year, int) or not 8 <= words(text) <= 22 or GRIM.search(text):
            continue
        for p in e.get("pages") or []:
            src = (p.get("originalimage") or p.get("thumbnail") or {}).get("source", "")
            name = commons_file(str(src))
            if name:
                out.append({"year": year, "text": text if text.endswith((".", "!", "?")) else text + ".",
                            "file": name.replace(" ", "_"), "page": str(p.get("title", ""))})
                break
    return out


def plan_history(rng: random.Random, today: datetime) -> dict:
    mm, dd = today.month, today.day
    selected = fetch_json(f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{mm:02d}/{dd:02d}")
    events = history_events(selected)
    if len(events) < SCENES + 2:
        try:
            more = fetch_json(f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{mm:02d}/{dd:02d}")
            events += history_events({"events": more.get("events")})
        except WorldError as e:
            log.warning("on-this-day events feed failed: %s", e)
    seen_years, uniq = set(), []
    for e in events:
        if e["year"] not in seen_years and e["file"] not in {u["file"] for u in uniq}:
            seen_years.add(e["year"])
            uniq.append(e)
    rng.shuffle(uniq)
    info = commons_info([e["file"] for e in uniq[:30]])
    scenes = []
    for e in uniq[:30]:
        if len(scenes) == SCENES:
            break
        meta = info.get(e["file"])
        if not meta:
            continue
        try:
            im = fetch_image(meta["url"])
        except WorldError as err:
            log.info("skip %s: %s", e["file"], err)
            continue
        scenes.append((e, meta, im))
    if len(scenes) < MIN_SCENES:
        raise WorldError("not enough on-this-day events with free pictures")
    scenes.sort(key=lambda x: x[0]["year"])
    date = f"{MONTHS[mm - 1]} {dd}"
    hook = f"What happened on {date}?"
    out = [Scene(im, e["text"], big=str(e["year"]) if e["year"] > 0 else f"{-e['year']} BC",
                 credit=f"{meta['artist'][:34]}  ·  {meta['licence']}",
                 seconds=read_seconds(e["text"]), media_id=e["file"][:60]) for e, meta, im in scenes]
    credit_lines = [f"- {e['year']}: \"{e['file'].replace('_', ' ')}\" by {m['artist']}, {m['licence']}, "
                    f"https://commons.wikimedia.org/wiki/File:{urllib.parse.quote(e['file'])}" for e, m, _ in scenes]
    return {
        "template": "world_history", "topic": date, "hook": hook, "label": f"ON THIS DAY  ·  {date.upper()}",
        "scenes": out, "title": f"What happened on {date} in history #shorts", "category_id": "27",
        "tags": ["history", "on this day", date.lower(), "facts", "today in history", "shorts"],
        "credits": "Event text from Wikipedia's \"On this day\" (CC BY-SA 4.0), shortened.\nPictures from Wikimedia "
                   "Commons:\n" + "\n".join(credit_lines),
    }


# ---------------------------------------------------------------------------------------------------------- clips
CLIP_TOPICS = [
    ("rocket launch", "rocket launches", ("launch", "liftoff", "lifts off")),
    ("spacewalk", "spacewalks", ("spacewalk",)),
    ("earth views space station", "Earth from the space station", ("earth",)),
    ("aurora space station", "auroras from orbit", ("aurora",)),
    ("mars rover", "Mars missions", ("mars",)),
    ("artemis", "the Artemis missions", ("artemis",)),
    ("solar dynamics observatory", "the Sun up close", ("sun", "solar")),
    ("hurricane space station", "hurricanes from orbit", ("hurricane",)),
    ("rocket engine test", "rocket engine tests", ("engine", "test")),
]
CLIP_HOOKS = ("Real NASA footage: {name}", "{name}, filmed for real", "You've never seen {name} like this",
              "{name}, straight from NASA")
NOT_CLIP = re.compile(r"(?i)\b(interview|briefing|conference|press|podcast|webinar|panel|talk|lecture|ceremony|"
                      r"livestream|broadcast|coverage|trailer|replay|news|music video|explainer|countdown clock|"
                      r"animated|graphic|nutrition|lab)\b")
MAX_CLIP = 250 * 1024 * 1024
CLIP_SECONDS = 4.8


def fetch_file(url: str, dest: Path, max_bytes: int = MAX_CLIP) -> Path:
    req = urllib.request.Request(check_url(url), headers={"User-Agent": UA, "Accept": "*/*"})
    got = 0
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=60) as r, open(dest, "wb") as f:  # noqa: S310
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > max_bytes:
                    raise WorldError("video too big")
                f.write(chunk)
    except OSError as e:
        raise WorldError(f"download failed: {type(e).__name__}: {e}"[:300]) from e
    with open(dest, "rb") as f:
        head = f.read(12)
    if head[4:8] != b"ftyp":
        dest.unlink(missing_ok=True)
        raise WorldError("not an MP4")
    return dest


def _ff(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    """ffmpeg on a local MP4 only (never the network)."""
    return subprocess.run([config.ffmpeg_exe(), "-hide_banner", "-protocol_whitelist", "file", *args],
                          capture_output=True, timeout=timeout)


def clip_seconds(path: Path) -> float:
    err = _ff(["-f", "mp4", "-i", str(path)]).stderr.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        raise WorldError("unreadable video")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def busiest_start(path: Path, seconds: float, length: float) -> float:
    """Where the most movement is: 2 small grey frames a second, the window of `length` with the largest change."""
    # a long video: key frames only, so the scan stays fast on the 2-core runner (15-minute make step)
    fast = ["-skip_frame", "nokey"] if seconds > 120 else []
    r = _ff([*fast, "-f", "mp4", "-i", str(path), "-vf", "fps=2,scale=64:36,format=gray",
             "-f", "rawvideo", "-"], 240)
    a = np.frombuffer(r.stdout, np.uint8)
    n = len(a) // (64 * 36)
    if n < 4:
        return max(0.0, seconds / 3)
    a = a[: n * 64 * 36].reshape(n, 36, 64).astype(np.int16)
    motion = np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))
    motion[motion > 60] = 0          # a hard cut or a flash is not movement
    win = max(1, int(length * 2))
    sums = np.convolve(motion, np.ones(win), "valid")
    skip = 2                        # never the first second (titles, fades)
    if len(sums) <= skip:
        return 0.0
    best = int(np.argmax(sums[skip:])) + skip
    return round(min(best / 2, max(0.0, seconds - length - 0.2)), 2)


def clip_frame(path: Path, t: float) -> Image.Image:
    r = _ff(["-ss", f"{t:.2f}", "-f", "mp4", "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png",
             "-"])
    try:
        return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    except OSError as e:
        raise WorldError("no frame") from e


def nasa_video_url(nasa_id: str) -> str:
    q = urllib.parse.quote(nasa_id)
    items = fetch_json(f"https://images-api.nasa.gov/asset/{q}").get("collection", {}).get("items", [])
    hrefs = [str(i.get("href", "")) for i in items if isinstance(i, dict)]
    for size in ("~medium.mp4", "~small.mp4", "~mobile.mp4"):
        for h in hrefs:
            if h.endswith(size):
                return h
    raise WorldError(f"no usable video size for {nasa_id}")


def plan_clips(rng: random.Random, recent_ids: set[str], recent_topics: set[str], work: Path) -> dict:
    topics = [t for t in CLIP_TOPICS if t[1] not in recent_topics] or list(CLIP_TOPICS)
    rng.shuffle(topics)
    for query, name, need in topics[:3]:
        q = urllib.parse.urlencode({"q": query, "media_type": "video", "page_size": 100})
        try:
            items = fetch_json(f"https://images-api.nasa.gov/search?{q}").get("collection", {}).get("items", [])
        except WorldError as e:
            log.warning("NASA video search %r failed: %s", query, e)
            continue
        cands = []
        for it in items:
            data = (it.get("data") or [{}])[0] if isinstance(it, dict) else {}
            nid = str(data.get("nasa_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,120}", nid) or nid in recent_ids:
                continue
            if NOT_CLIP.search(f"{data.get('title') or ''} {data.get('description') or ''}"):
                continue
            cap = nasa_ok(data, need)
            if cap:
                cands.append((nid, cap))
        rng.shuffle(cands)
        scenes, seen = [], set()
        for k, (nid, cap) in enumerate(cands[:7]):
            if len(scenes) == 4:
                break
            if cap in seen:
                continue
            try:
                f = fetch_file(nasa_video_url(nid), work / f"clip{k}.mp4")
                dur = clip_seconds(f)
                if not 6 <= dur <= 900:
                    raise WorldError(f"{dur:.0f}s long")
                start = busiest_start(f, dur, CLIP_SECONDS)
                im = clip_frame(f, start)
            except (WorldError, subprocess.TimeoutExpired) as e:
                log.info("skip clip %s: %s", nid, e)
                continue
            seen.add(cap)
            scenes.append(Scene(im, cap, credit=f"NASA  ·  {nid[:40]}", seconds=max(CLIP_SECONDS, read_seconds(cap)),
                                media_id=nid, clip=f, clip_start=start, clip_dur=dur))
        if len(scenes) < MIN_SCENES:
            continue
        hook = rng.choice(CLIP_HOOKS).format(name=name)
        hook = hook[0].upper() + hook[1:]
        ids = [s.media_id for s in scenes]
        return {
            "template": "world_clips", "topic": name, "hook": hook, "label": "NASA FOOTAGE", "scenes": scenes,
            "title": f"{hook} #shorts", "category_id": "28",
            "tags": ["space", "nasa", "rocket", name.lower(), "science", "shorts"],
            "credits": "Video: NASA (public domain), video library ids: " + ", ".join(ids)
                       + ".\nNot affiliated with or endorsed by NASA.",
        }
    raise WorldError("no clip subject had enough usable NASA videos")


# ----------------------------------------------------------------------------------------------------------- games
GAMES_DIR = config.ASSETS / "games"
GAME_HOOKS = ("{name}: a free Roblox game", "Have you played {name}?", "{name}, in 30 seconds",
              "A Roblox game you haven't tried: {name}")


def plan_game(rng: random.Random, recent_ids: set[str], recent_topics: set[str]) -> dict:
    """A spotlight on one of our own Roblox games: its screenshots full screen, its facts read out, its link."""
    games = json.loads((GAMES_DIR / "games.json").read_text(encoding="utf-8"))
    ready = {k: g for k, g in games.items()
             if not k.startswith("_") and g.get("published") and g.get("url") and list((GAMES_DIR / k).glob("*.jpg"))}
    if not ready:
        raise WorldError("no published game with screenshots")
    pool = [k for k, g in ready.items() if g["name"] not in recent_topics] or list(ready)
    key = rng.choice(pool)
    g = ready[key]
    shots = sorted((GAMES_DIR / key).glob("*.jpg"))
    fresh = [s for s in shots if f"{key}/{s.name}" not in recent_ids] or shots
    rng.shuffle(fresh)
    lines = [g["pitch"]] + list(g["facts"])
    scenes = []
    for k, text in enumerate(lines[:5]):   # a game with few screenshots shows them again, framed differently
        shot = fresh[k % len(fresh)]
        scenes.append(Scene(Image.open(shot).convert("RGB"), text, big=g["name"] if k == 0 else "",
                            credit="", seconds=read_seconds(text), media_id=f"{key}/{shot.name}"))
    if len(scenes) < MIN_SCENES:
        raise WorldError(f"{key}: not enough screenshots or facts")
    hook = rng.choice(GAME_HOOKS).format(name=g["name"])
    return {
        "template": "world_game", "topic": g["name"], "hook": hook, "label": "PIXELHELM ON ROBLOX",
        "scenes": scenes, "title": f"{hook} #roblox #shorts", "category_id": "20",
        "tags": ["roblox", g["name"].lower(), "roblox games", "pixelhelm", "indie game", "shorts"],
        "credits": f"Play {g['name']} free on Roblox: {g['url']}\nMade by PIXELHELM, a small Roblox studio.",
    }


# --------------------------------------------------------------------------------------------------------- nature
NATURE_FILE = config.DATA / "world_nature.json"


def summary_scene(title: str) -> tuple[str, str, str] | None:
    """(name, caption, Commons file) from a Wikipedia article summary, or None when it can't be used."""
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))
    data = fetch_json(url)
    if data.get("type") != "standard":
        return None
    name = re.sub(r"\s*\([^)]*\)", "", clean(str(data.get("title", title)))).strip()
    cap = re.sub(r"\s*\([^)]*\)", "", first_sentence(str(data.get("extract", "")))).replace(" ,", ",").strip()
    if not 6 <= words(cap) <= 26 or GRIM.search(cap) or not cap.endswith((".", "!", "?")):
        return None
    file = commons_file(str((data.get("originalimage") or data.get("thumbnail") or {}).get("source", "")))
    return (name, cap, file.replace(" ", "_")) if file else None


def plan_nature(rng: random.Random, recent_ids: set[str], recent_topics: set[str]) -> dict:
    lists = {k: v for k, v in json.loads(NATURE_FILE.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    kinds = [k for k in lists if k not in recent_topics] or list(lists)
    kind = rng.choice(kinds)
    spec = lists[kind]
    titles = [t for t in spec["titles"] if t not in recent_ids] or list(spec["titles"])
    rng.shuffle(titles)
    picked = []
    for t in titles[:14]:
        try:
            got = summary_scene(t)
        except WorldError as e:
            log.info("skip %s: %s", t, e)
            continue
        if got:
            picked.append((t, re.sub(r"\s*\([^)]*\)", "", t).strip(), got[1], got[2]))  # our short name
    info = commons_info([p[3] for p in picked]) if picked else {}
    scenes, credits = [], []
    for title, name, cap, file in picked:
        if len(scenes) == SCENES:
            break
        meta = info.get(file)
        if not meta:
            continue
        try:
            im = fetch_image(meta["url"])
        except WorldError as e:
            log.info("skip %s: %s", file, e)
            continue
        scenes.append(Scene(im, cap, big=name, credit=f"{meta['artist'][:34]}  ·  {meta['licence']}",
                            seconds=read_seconds(cap), media_id=title))
        credits.append(f"- {name}: \"{file.replace('_', ' ')}\" by {meta['artist']}, {meta['licence']}, "
                       f"https://commons.wikimedia.org/wiki/File:{urllib.parse.quote(file)}")
    if len(scenes) < MIN_SCENES:
        raise WorldError(f"not enough {kind} with free pictures")
    hook = rng.choice(spec["hook"]).format(n=len(scenes))
    return {
        "template": "world_nature", "topic": kind, "hook": hook,
        "label": spec.get("label", kind.upper()), "scenes": scenes, "title": f"{hook} #shorts",
        "category_id": str(spec.get("category_id", "27")),
        "tags": list(spec.get("tags", [kind, "facts", "shorts"])),
        "credits": "Text from Wikipedia (CC BY-SA 4.0), shortened.\nPictures from Wikimedia Commons:\n"
                   + "\n".join(credits),
    }


# --------------------------------------------------------------------------------------------------------- render
def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = f"{cur} {w}".strip()
        if draw.textlength(t, font=font) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


def _fit_lines(draw, text, size, width, max_lines, weight="bold"):
    while True:
        f = fonts.sans(size, weight)
        lines = _wrap(draw, text, f, width)
        if len(lines) <= max_lines or size <= 30:
            return f, lines[:max_lines]
        size -= 4


class Layout:
    """Full-screen Shorts (owner 2026-11-07: more watch time): the photo fills the whole 1080x1920 frame and slowly
    pushes in / pans; text sits on solid ink panels (no outlines, no see-through), left of the Shorts buttons and
    above the title area YouTube draws at the bottom. Video clips are full width in the upper half (a 16:9 clip
    cropped to 9:16 would lose two thirds of the picture; this crop keeps about two thirds of the width), between
    the hook / label panel and the caption. Everything is laid out for 1080x1920 and scaled."""

    def __init__(self, w: int, h: int, accent: str | None = None):
        self.w, self.h, self.s = w, h, w / 1080
        self.ink, self.paper, self.sub, self.accent = (style.rgb(k) for k in ("ink", "paper", "sub", "accent"))
        if accent:   # the episode's accent (bot/palette.py)
            self.accent = tuple(int(accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        self.video_top = self.px(250)
        self.caption_top = self.px(1150)
        self.caption_bottom = self.px(1560)   # YouTube's title and channel name start below this
        self.right = self.px(980)            # the like / comment / share buttons start right of this

    def px(self, v: float) -> int:
        return int(round(v * self.s))


Rect = tuple[int, int, int, int]


def _panel(d: ImageDraw.ImageDraw, lay: Layout, box: Rect, panels: list[Rect]) -> None:
    x0, y0, x1, y1 = box
    box = (max(0, x0), max(0, y0), min(lay.w, x1), min(lay.h, y1))
    d.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=lay.ink)
    panels.append(box)


def _base(lay: Layout, plan: dict, sc: Scene, first: bool) -> tuple[np.ndarray, list[Rect], Rect | None]:
    """The text layer of a scene: an ink canvas with the text panels drawn on it, the panel rectangles (copied over
    the moving photo each frame) and, for a video scene, where the clip plays."""
    im = Image.new("RGB", (lay.w, lay.h), lay.ink)
    d = ImageDraw.Draw(im)
    panels: list[Rect] = []
    x, pad = lay.px(80), lay.px(24)
    if first:   # the hook, big, top left
        f, lines = _fit_lines(d, plan["hook"], lay.px(72), lay.px(860), 2)
        step = lay.px(84)
        y0 = lay.px(150)
        width = max(int(d.textlength(ln, font=f)) for ln in lines)
        _panel(d, lay, (x - pad, y0 - pad, x + width + pad, y0 + len(lines) * step + pad // 2), panels)
        for k, ln in enumerate(lines):
            d.text((x, y0 + k * step), ln, font=f, fill=lay.paper)
    else:       # a small label
        f = fonts.sans(lay.px(32))
        y0 = lay.px(160)
        _panel(d, lay, (x - pad, y0 - pad // 2, x + int(d.textlength(plan["label"], font=f)) + pad,
                        y0 + lay.px(40) + pad // 2), panels)
        d.text((x, y0), plan["label"], font=f, fill=lay.accent)
    # the caption block: big name / year, the caption, the credit, measured first so it never runs into YouTube's UI
    big_f = None
    if sc.big:
        size = lay.px(96)
        while size > lay.px(48) and d.textlength(sc.big, font=fonts.serif(size)) > lay.px(860):
            size -= lay.px(6)
        big_f = fonts.serif(size)
    cap_f, lines = _fit_lines(d, sc.text, lay.px(50), lay.px(840), 4 if sc.big else 5)
    line_h = int(cap_f.size * 1.27)
    cred_f = fonts.sans(lay.px(24), "medium")
    height = (lay.px(112) if big_f else 0) + len(lines) * line_h + lay.px(44)
    top = min(lay.caption_top, lay.caption_bottom - height - pad)
    widths = [int(d.textlength(ln, font=cap_f)) for ln in lines] + [int(d.textlength(sc.credit[:90], font=cred_f))]
    if big_f:
        widths.append(int(d.textlength(sc.big, font=big_f)))
    _panel(d, lay, (x - pad, top - pad, min(lay.right, x + max(widths) + pad), top + height + pad // 2), panels)
    y = top
    if big_f:
        d.text((x, y - lay.px(16)), sc.big, font=big_f, fill=lay.accent)
        y += lay.px(112)
    for ln in lines:
        d.text((x, y), ln, font=cap_f, fill=lay.paper)
        y += line_h
    d.text((x, y + lay.px(10)), sc.credit[:90], font=cred_f, fill=lay.sub)
    video = None
    if sc.clip is not None:
        # from under the hook / label panel down to the caption: a 16:9 clip loses a little at the sides only
        vtop = max(lay.video_top, panels[0][3] + lay.px(20))
        dh = (top - pad - vtop - lay.px(20)) // 2 * 2
        video = (0, vtop, lay.w, dh)
    return np.asarray(im).copy(), panels, video


def _outro(lay: Layout, p: float) -> np.ndarray:
    im = Image.new("RGB", (lay.w, lay.h), lay.ink)
    d = ImageDraw.Draw(im)
    rise = lay.px(60 * (1 - p))
    # the channel's name and lines: repo variables BOT_BRAND / BOT_TAGLINE / BOT_TAGLINE2 (one bot per channel)
    brand = os.environ.get("BOT_BRAND") or "PIXELHELM"
    line1 = os.environ.get("BOT_TAGLINE") or "something new every day"
    line2 = os.environ.get("BOT_TAGLINE2", "and our games on Roblox")
    d.text((lay.px(80), lay.px(700) + rise), brand, font=fonts.serif(lay.px(130)), fill=lay.paper)
    d.text((lay.px(84), lay.px(880) + rise), line1, font=fonts.sans(lay.px(48)), fill=lay.accent)
    d.text((lay.px(84), lay.px(960) + rise), line2, font=fonts.sans(lay.px(40), "medium"), fill=lay.sub)
    return np.asarray(im)


def _ease(p: float) -> float:
    return p * p * (3 - 2 * p)


class _Frames:
    FADE = 0.35
    OUTRO = 2.2

    def __init__(self, plan: dict, w: int, h: int, rng: random.Random, work: Path | None = None):
        self.lay, self.plan = Layout(w, h, plan.get("accent")), plan
        self.scenes: list[Scene] = plan["scenes"]
        self.bases = [_base(self.lay, plan, sc, i == 0) for i, sc in enumerate(self.scenes)]
        self.video: list[np.ndarray | None] = []
        for i, sc in enumerate(self.scenes):
            rect = self.bases[i][2]
            if rect is None or work is None:
                self.video.append(None)
                continue
            _, _, dw, dh = rect
            raw = work / f"scene{i}.rgb"
            r = _ff(["-ss", f"{sc.clip_start:.2f}", "-t", f"{sc.seconds + 0.2:.2f}", "-f", "mp4", "-i", str(sc.clip),
                     "-an", "-vf", f"fps={config.FPS},scale={dw}:{dh}:force_original_aspect_ratio=increase:"
                     f"flags=lanczos,crop={dw}:{dh}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-y", str(raw)], 300)
            n = raw.stat().st_size // (dw * dh * 3) if raw.exists() else 0
            if r.returncode != 0 or n < config.FPS:
                raise WorldError(f"could not decode clip {sc.media_id}")
            self.video.append(np.memmap(raw, np.uint8, "r", shape=(n, dh, dw, 3)))
        # each photo pans its own way across the frame while it pushes in (or pulls out)
        self.moves = [(rng.choice((1, -1)), rng.uniform(-0.3, 0.3), rng.choice((1, -1))) for _ in self.scenes]
        self.starts = np.cumsum([0.0] + [s.seconds for s in self.scenes]).tolist()
        self.total = self.starts[-1] + self.OUTRO

    def close(self) -> None:
        """Release the decoded clip files (Windows can't delete a file that is still mapped)."""
        for v in self.video:
            mm = getattr(v, "_mmap", None)
            if mm is not None:
                mm.close()
        self.video = [None] * len(self.scenes)

    def _cover(self, img: Image.Image, p: float, move: tuple) -> np.ndarray:
        """The photo cropped to fill the frame, a slow push-in plus a pan along its long side."""
        w, h = self.lay.w, self.lay.h
        side, tilt, way = move
        e = _ease(p)
        aspect = w / h
        bw, bh = (img.height * aspect, img.height) if img.width / img.height > aspect else (img.width,
                                                                                           img.width / aspect)
        z = 1.0 + 0.12 * (e if way > 0 else 1 - e)
        cw, ch = bw / z, bh / z
        u = 0.5 + side * 0.35 * (2 * e - 1)          # across the spare width
        v = 0.5 + tilt * (2 * e - 1)                 # a little up or down
        x0 = (img.width - cw) * min(max(u, 0.0), 1.0)
        y0 = (img.height - ch) * min(max(v, 0.0), 1.0)
        return np.asarray(img.resize((w, h), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch)))

    def scene(self, i: int, p: float) -> np.ndarray:
        base, panels, rect = self.bases[i]
        vid = self.video[i]
        if vid is not None:   # real footage moves by itself: full width, no push-in
            out = base.copy()
            rx, ry, dw, dh = rect
            out[ry:ry + dh, rx:rx + dw] = vid[min(int(p * self.scenes[i].seconds * config.FPS), len(vid) - 1)]
            return out
        out = self._cover(self.scenes[i].image, p, self.moves[i]).copy()
        for x0, y0, x1, y1 in panels:
            out[y0:y1, x0:x1] = base[y0:y1, x0:x1]
        return out

    def at(self, t: float) -> np.ndarray:
        n = len(self.scenes)
        if t >= self.starts[-1]:
            return _outro(self.lay, min(1.0, (t - self.starts[-1]) / self.OUTRO))
        i = max(k for k in range(n) if self.starts[k] <= t)
        dur = self.scenes[i].seconds
        p = (t - self.starts[i]) / dur
        frame = self.scene(i, p)
        left = self.starts[i + 1] - t
        if left < self.FADE:  # a quick dip through the background colour: two captions never overlap
            a = 1 - left / self.FADE
            ink = np.array(self.lay.ink, np.float32)
            if a < 0.5:
                frame = (frame * (1 - 2 * a) + ink * 2 * a).astype(np.uint8)
            else:
                nxt = self.scene(i + 1, 0.0) if i + 1 < n else _outro(self.lay, 0.0)
                frame = (nxt * (2 * a - 1) + ink * (2 - 2 * a)).astype(np.uint8)
        if t < 0.25 and i == 0:  # frame 0 is never black, just a quick lift
            frame = (frame * (0.55 + 0.45 * t / 0.25)).astype(np.uint8)
        return frame


def _narrate(plan: dict) -> list[tuple[float, np.ndarray]]:
    """The narrator reads the hook, then each caption as its scene starts; a scene lasts as long as its line needs
    (a clip scene's start moves back so the footage never runs out). [] when the narrator is off."""
    if not voice.enabled():
        return []
    sr, t, out = music.SR, 0.0, []
    hook = voice.say(plan["hook"])
    for i, sc in enumerate(plan["scenes"]):
        lead = 0.25
        if i == 0 and hook is not None:
            out.append((t + 0.15, hook))
            lead = 0.15 + len(hook) / sr + 0.25
        a = voice.say(sc.text)
        if a is not None:
            out.append((t + lead, a))
            sc.seconds = round(max(sc.seconds, lead + len(a) / sr + 0.45), 2)
        if sc.clip is not None and sc.clip_dur:
            sc.clip_start = max(0.0, min(sc.clip_start, sc.clip_dur - sc.seconds - 0.3))
        t += sc.seconds
    return out


def render(plan: dict, out: Path, w: int, h: int, seed: int) -> Path:
    rng = random.Random(seed)
    bpm = 80 + seed % 12
    clips = _narrate(plan)
    with tempfile.TemporaryDirectory(prefix="world-") as td:
        fr = _Frames(plan, w, h, rng, Path(td))
        bed = None
        if plan.get("music_tracks"):   # real tracks (bot/music.py); the synth score is the fallback
            try:
                bed = music.library_bed(fr.total, list(plan["music_tracks"]), seed)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                log.warning("music track unusable (%s); synth score instead", e)
        if bed is None:
            bed = music.score(fr.total, bpm, seed % 100000, "soft", bed=("full", "box", "drone")[seed % 3])
        wav = music.write_wav(Path(td) / "music.wav", voice.place(bed, clips, music.SR))
        cmd = [config.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{w}x{h}", "-r", str(config.FPS), "-i", "-", "-i", str(wav),
               "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
               "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
               "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
               "-shortest", "-movflags", "+faststart", str(out)]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for k in range(int(round(fr.total * config.FPS))):
                p.stdin.write(fr.at(k / config.FPS).tobytes())
            p.stdin.close()
        except BrokenPipeError:
            pass
        finally:
            fr.close()
        err = p.stderr.read().decode("utf-8", "replace")
        if p.wait() != 0:
            raise WorldError(f"ffmpeg failed: {err[-400:]}")
    return out


def thumbnail(plan: dict, out: Path) -> Path:
    """1280x720: the first picture on the right, the hook on a solid panel on the left."""
    W, H = 1280, 720
    lay = Layout(1080, 1920)
    im = Image.new("RGB", (W, H), lay.ink)
    pic = plan["scenes"][0].image
    k = max(760 / pic.width, H / pic.height)
    pic = pic.resize((int(pic.width * k), int(pic.height * k)), Image.LANCZOS)
    im.paste(pic.crop(((pic.width - 760) // 2, (pic.height - H) // 2, (pic.width - 760) // 2 + 760,
                       (pic.height - H) // 2 + H)), (W - 760, 0))
    d = ImageDraw.Draw(im)
    f, lines = _fit_lines(d, plan["hook"], 76, 440, 4)
    y = (H - len(lines) * int(f.size * 1.2)) // 2
    for ln in lines:
        d.text((44, y), ln, font=f, fill=lay.paper)
        y += int(f.size * 1.2)
    im.save(out, "JPEG", quality=90)
    return out


# ----------------------------------------------------------------------------------------------------------- make
def _recent(history: list[dict], n: int = 40) -> tuple[set[str], set[str], str | None]:
    world = [r for r in history if str(r.get("template") or "").startswith("world_")][-n:]
    ids = {str(i) for r in world for i in (r.get("dream_ids") or [])}
    topics = {str((r.get("lines") or {}).get("topic", "")) for r in world[-6:]}
    return ids, topics, (world[-1].get("template") if world else None)


def plan(history: list[dict], used: set[str], seed: int, forced: str = "", now: datetime | None = None,
         work: Path | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed)
    ids, topics, last = _recent(history)
    cycle = ["world_clips", "world_space", "world_history", "world_nature"]
    if forced in cycle + ["world_game"]:
        order = [forced]
    elif (os.environ.get("BOT_WORLD") or "").strip().lower() == "games":   # the Roblox channel: our games only
        order = ["world_game"]
    else:
        k = (cycle.index(last) + 1) % len(cycle) if last in cycle else 0
        order = cycle[k:] + cycle[:k]
    errors = []
    for kind in order:
        try:
            if kind == "world_game":
                p = plan_game(rng, ids, topics)
            elif kind == "world_clips":
                if work is None:
                    raise WorldError("no work folder for clips")
                p = plan_clips(rng, ids, topics, work)
            elif kind == "world_space":
                p = plan_space(rng, ids, topics)
            elif kind == "world_nature":
                p = plan_nature(rng, ids, topics)
            else:
                p = plan_history(rng, now)
        except WorldError as e:
            errors.append(f"{kind}: {e}")
            continue
        lines = {"hook": p["hook"], "topic": p["topic"]}
        lines.update({f"s{i}": s.text for i, s in enumerate(p["scenes"])})
        media = [s.media_id for s in p["scenes"]]
        day = now.strftime("%Y-%m-%d") if kind == "world_history" else ""
        fp = fingerprint(kind, media + ([day] if day else []), None, lines)
        if fp in used:
            errors.append(f"{kind}: already posted")
            continue
        p.update(lines=lines, media=media, fingerprint=fp)
        return p
    raise WorldError("; ".join(errors) or "nothing planned")


def storyboard(p: dict, seed: int, now: datetime, w: int, h: int) -> dict:
    footer = os.environ.get("BOT_FOOTER") or (f"Made by PIXELHELM, an indie studio. Play our dreamcore game liminality "
                                              f"free on Roblox: {GAME_URL}")
    desc = f"{p['hook']}\n\n{p['credits']}\n\n{footer}"
    title = re.sub(r"[<>]", "", p["title"])[:100]
    return {
        "version": 1, "kind": "short", "format": "world", "template": p["template"],
        "episode_id": f"ep-{now.strftime('%Y%m%d')}-{p['fingerprint'][:8]}",
        "dream_ids": p["media"], "monster": None, "fingerprint": p["fingerprint"], "lines": p["lines"],
        "music": {"bpm": 80 + seed % 12, "seed": seed % 100000, "mood": "soft"},
        "synthetic": False, "title": title, "description": desc[:4900], "tags": p["tags"],
        "category_id": p["category_id"],
        "shots": [{"seconds": s.seconds, "media": s.media_id, "credit": s.credit} for s in p["scenes"]],
        "size": [w, h],
    }


def make(settings: config.Settings, history: list[dict], used: set[str], seed: int | None,
         out_root: Path) -> tuple[dict, Path, Path, Path, qa.Report]:
    now = datetime.now(timezone.utc)
    seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**31)
    forced = settings.template if settings.template.startswith("world_") else ""
    work = Path(tempfile.mkdtemp(prefix="world-clips-"))
    try:
        return _make(settings, history, used, seed, out_root, now, forced, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _make(settings: config.Settings, history: list[dict], used: set[str], seed: int, out_root: Path,
          now: datetime, forced: str, work: Path) -> tuple[dict, Path, Path, Path, qa.Report]:
    p = plan(history, used, seed, forced, now, work)
    w, h = (540, 960) if settings.low_res else (config.W, config.H)
    msb = {"music": {"mood": "soft", "seed": seed}, "description": ""}
    music.assign(msb, history, seed)                  # a real track, not a recent one (no pack: the synth)
    p["music_tracks"] = msb["music"].get("tracks")
    p["accent"] = palette.pick(history, seed)["accent"]   # a new accent colour each video
    sb = storyboard(p, seed, now, w, h)
    sb["palette"] = {"accent": p["accent"]}
    sb["music"]["track"] = msb["music"].get("track")
    sb["description"] = (sb["description"] + msb["description"])[:4900]
    log.info("planned world Short %s: %s (%s)", sb["episode_id"], sb["template"], p["topic"])
    folder = out_root / sb["episode_id"]
    folder.mkdir(parents=True, exist_ok=True)
    video = render(p, folder / "video.mp4", w, h, seed)
    report = qa.check(video, w, h)
    report.info["text"] = "world short (Pillow text)"
    (folder / "qa.txt").write_text(report.as_text() + f"\nINFO  text: {report.info['text']}\n"
                                   + json.dumps(report.info) + "\n", encoding="utf-8")
    thumb = thumbnail(p, folder / "thumb.jpg")
    (folder / "storyboard.json").write_text(json.dumps(sb, indent=2), encoding="utf-8")
    return sb, folder, video, thumb, report
