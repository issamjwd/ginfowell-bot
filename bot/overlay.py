"""Animated text overlay with Remotion (render/): hook title, captions, outro line and end card over a CLEAN video.

    clean.mp4 (Renderer(text=False): no burned text)  ->  node render/render.mjs  ->  overlay video (muted)
    ->  ffmpeg muxes the clean video's own audio back in (stream copy)  ->  video.mp4

Never downloads a browser: it needs a local Chrome (REMOTION_BROWSER_EXECUTABLE, or the usual install paths; on
GitHub's ubuntu-24.04 runner that is the preinstalled Google Chrome). Any failure raises OverlayError and the
pipeline falls back to the burned Pillow/FFmpeg captions, so the episode still ships.
Stills and contact sheets are grabbed from the finished mp4 with FFmpeg (no extra browser launches).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image

from . import bank, config, fx, qa, rig

log = logging.getLogger(__name__)
PROJECT = config.ROOT / "render"
ENTRY = PROJECT / "render.mjs"
TIMEOUT = 8 * 60  # seconds for a Short's node render (the fallback still has to fit in the make step's hour)
SECONDS_PER_FRAME = 1.0  # budget per overlay frame on a 2-core runner (a long video's limit scales with its frames)
TIMEOUT_MAX = 30 * 60  # leaves room in the make step for the burned-caption fallback
LONG_HOOK_SECONDS = 4.5
CHAPTER_TITLE = 1.5  # seconds a chapter title rides its first shot (long video)
RANGE_GAP = 12      # long video: overlay ranges closer than this many frames are rendered as one
# The long video's final encode. Its film grain keeps x264 at the 10 Mbit/s cap, where the preset barely matters:
# measured 2026-11-06 on 480 graded 1080p frames vs a lossless reference, superfast 30.35 dB PSNR / SSIM 0.914 and
# veryfast 30.38 dB / 0.917 (the grain itself is what the cap cannot keep, in both), for half the x264 CPU
# (25 vs 51 ms a frame on 2 cores). (veryfast was itself 30% faster than "faster", PSNR 46 dB between the two.)
COMPOSE_PRESET = "superfast"
LENGTH_SLACK = 0.15  # seconds the overlaid video may differ from the clean one
HOOK_SECONDS = 2.0
HOOK_MIN = 1.0      # shortest hook worth showing
CAPTION_MIN = 1.4   # the first caption keeps at least this long after the hook
OUTRO_ENDCARD = {"title": "liminality", "cta": "free on roblox", "byline": "PIXELHELM"}
# Shake amplitude (stage px at 1080 wide) and decay: the SAME numbers bot/render.py uses, so text rides the shake.
SHAKE = {"impact": (34.0, 4.0), "shake": (12.0, 0.8)}
# Caption emphasis (owner: "highlights wrong words"): the ink box goes on the line's most charged word by MEANING:
# the scariest / most concrete noun or verb (the monster, the dream, alone, door, behind...). Pronouns, auxiliaries,
# filler and common verbs (see, remember, look, go...) never get it. A line where no word earns it gets none.
CHARGED = ("monster", "mannequin", "bear", "neighbour", "behind", "alone", "nobody", "noticed", "stare", "watching",
           "following", "follow", "knock", "dark", "run", "running", "smiles", "closer", "found", "listens", "hears",
           "awake", "waving", "waved", "late")                       # the threat itself: always wins
CONCRETE = ("dream", "dreams", "halls", "door", "doors", "lights", "lamps", "light", "tape", "home", "clock", "train",
            "water", "window", "windows", "eyes", "song")             # a concrete thing, when no threat word is there
STOP = frozenset((
    "a an and are at be been but by did do does for from had has have i if in into is it its just me my no not of on "
    "or so some something somewhere that the then there they this to too was we were what when where who why will "
    "with you you're your don't can't won't isn't it'll it was here still very really once again back up down out "
    "over off all any every each one two three rule "
    # common verbs and soft words that never carry the scare
    "see saw seen look looked looks remember remembers remembered go goes went come came know think get got make made take "
    "took find want feel felt used say said hum stay keep open close fell fall wake walk slowly little soft softer "
    "probably maybe quiet place times time thing things way whole forgot forget never always ticking"
).split())
BROWSER_CANDIDATES = (
    "/usr/bin/google-chrome",            # GitHub ubuntu-24.04 runner (preinstalled Google Chrome stable)
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


class OverlayError(RuntimeError):
    """The Remotion overlay could not be made (the caller falls back to burned captions)."""


# ------------------------------------------------------------------------------------------------ props
def _norm(s: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def _r(x: float) -> float:
    return round(x, 3)


def emphasis(text: str) -> int:
    """Index (in text.split()) of the word the caption leans on, or -1 when no word earns the marker.

    1. a CHARGED threat word (the last one, where the line lands)  2. else a CONCRETE thing  3. else the longest word
    of 5+ letters that is not on the STOP list  4. else none."""
    words = [re.sub(r"[^a-z']", "", w.lower()) for w in text.split()]
    if len(words) < 2:
        return -1
    for tier in (CHARGED, CONCRETE):
        hits = [i for i, w in enumerate(words) if w in tier]
        if hits:
            return hits[-1]
    best = max(range(len(words)), key=lambda i: (len(words[i]) if words[i] not in STOP else 0, i))
    return best if words[best] not in STOP and len(words[best]) >= 5 else -1


first_frame = rig.first_frame  # the frame bot/render.py starts a shot on (a ceil, not a round)


def shake_track(shot_index: int, fxs: list, s0: float, f_from: int, f_to: int, fps: int) -> list[list[int]]:
    """Per-frame (x, y) offsets in stage px that bot/render.py shakes a shot starting at s0 by (the content moves by
    -dx, -dy), for absolute frames f_from..f_to-1 with the renderer's own tl = fi / fps - s0. Trailing zeros dropped,
    so a decayed impact costs nothing."""
    kind = "impact" if "impact" in fxs else "shake" if "shake" in fxs else None
    if kind is None:
        return []
    amp, decay = SHAKE[kind]
    track = []
    for fi in range(f_from, f_to):
        dx, dy = fx.shake_offset(fi / fps - s0, shot_index, amp, decay)
        track.append([-dx, -dy])
    while track and track[-1] == [0, 0]:
        track.pop()
    return track


def props_from_storyboard(sb: dict, width: int = config.W, height: int = config.H, fps: int = config.FPS) -> dict:
    """storyboard.json (v1) -> composition props for render/src/Short.jsx. Pure function (tested). The episode's
    palette (bot/palette.py) rides along as props.colours; render.mjs lays it over style.json's colours."""
    props = _props(sb, width, height, fps)
    if sb.get("palette"):
        props["colours"] = dict(sb["palette"])
    return props


def _props(sb: dict, width: int, height: int, fps: int) -> dict:
    if sb.get("captions") is not None:
        return _edit_props(sb, width, height, fps)
    long_ = sb.get("kind") == "long"
    captions, outro, t, last_clip_end, calm_end, end_seconds = [], None, 0.0, 0.0, None, 0.0
    chapters = []  # long video: chapter title cards
    shot_of = []  # caption index -> (shot index, shot start, its fx, its transition)
    t_of_end = []  # shot index -> its end time
    impacts = []   # (shot index, shot start) of every impact frame, captioned or not
    for k, shot in enumerate(sb["shots"]):
        start = t
        t += float(shot["seconds"])
        t_of_end.append(t)
        fxs = shot.get("fx") or []
        if "impact" in fxs:
            impacts.append((k, start))
        if shot["kind"] in ("clip", "scene"):
            last_clip_end = t
            if not fxs:
                calm_end = t
            if shot.get("caption"):
                glitch = "impact" in fxs or "shake" in fxs
                captions.append({"text": shot["caption"], "start": _r(start), "end": _r(t),
                                 "style": "glitch" if glitch else "drift", "emph": emphasis(shot["caption"])})
                shot_of.append((k, start, fxs, shot.get("transition", "cut")))
        elif shot["kind"] == "card" and shot.get("end"):
            end_seconds = float(shot["seconds"])
        elif shot["kind"] == "card" and shot.get("chapter"):
            chapters.append({"start": _r(start), "seconds": _r(float(shot["seconds"])), "n": int(shot["chapter"]["n"]),
                             "name": shot["chapter"]["name"], "line": shot.get("caption") or ""})
        elif shot["kind"] == "card" and shot.get("caption") and outro is None:
            outro = {"text": shot["caption"], "start": _r(start)}
    frames = max(1, int(round(t * fps)))

    # Hook title: the thumbnail line, unless a caption already says it (then the dream's name). Never twice.
    said = {_norm(c["text"]) for c in captions} | ({_norm(outro["text"])} if outro else set())
    dream = sb.get("dream_ids", [None])[0]
    dream_name = bank.dream(dream).name if dream else ""
    # storyboard lines can still hold template slots ("this is {dream}."): fill them, never show a raw brace
    raw = ((sb.get("thumbnail") or {}).get("text"), (sb.get("lines") or {}).get("hook"))
    cands = [(c or "").replace("{dream}", dream_name) for c in raw]
    title = next((c for c in cands if _norm(c) and _norm(c) not in said and "{" not in c and "}" not in c), None)
    label = dream_name
    if title is None:
        title, label = (dream_name, "") if _norm(dream_name) not in said else ("", "")
    hook = {"title": title, "label": label if _norm(label) != _norm(title) else "", "until": HOOK_SECONDS}
    if long_ and isinstance(sb.get("hook"), dict):  # the long video's own hook (bot/longform.py)
        hook = {"title": sb["hook"].get("title") or "", "label": sb["hook"].get("label") or "",
                "until": LONG_HOOK_SECONDS}
    if not hook["title"] and not hook["label"]:
        hook = None
    # The hook and the first caption never share the screen: the caption waits for the hook (keeping its end time);
    # on a short first shot the hook is cut shorter so the caption still gets CAPTION_MIN, or it is dropped.
    if hook and captions and captions[0]["start"] < hook["until"]:
        split = min(hook["until"], captions[0]["end"] - CAPTION_MIN)
        if split >= HOOK_MIN:
            hook["until"] = _r(split)
            captions[0]["start"] = _r(split)
        else:
            hook = None
    # Sync with the picture: hits, zoom punch and whips only when the caption starts on its shot's first frame
    # (the first caption may wait for the hook); the shake track always, from the caption's own first frame.
    # Frames follow the renderer: a shot starts on ceil(start * fps). tl0 is the renderer's shot-local time on the
    # caption's first frame, so the JSX can rebuild its impact, whip and zoom-punch frames exactly.
    for c, (k, s0, fxs, tr) in zip(captions, shot_of):
        f_shot, f_end = first_frame(s0, fps), first_frame(t_of_end[k], fps)
        # a caption that starts on its cut takes the renderer's frame; one that waits for the hook keeps its own
        f_cap = f_shot if c["start"] == _r(s0) else max(f_shot, int(round(c["start"] * fps)))
        on_cut = f_cap == f_shot
        if on_cut:
            c["start"] = round(f_cap / fps, 5)
        c["end"] = round(f_end / fps, 5)
        c["tl0"] = round(f_cap / fps - s0, 5)
        c["hits"] = [h for h in ("impact", "zoom_punch") if h in fxs and on_cut]
        c["enter"] = tr if on_cut and tr in ("whip", "fade") else "cut"
        c["shake"] = shake_track(k, fxs, s0, f_cap, f_end, fps)
    freeze_at = max(0.0, (calm_end if calm_end is not None else last_clip_end) - 0.4)
    props = {
        "width": width, "height": height, "fps": fps, "durationInFrames": frames,
        "layout": "landscape" if width > height else "portrait",
        "hook": hook,
        "captions": captions,
        "chapters": chapters,
        "outro": outro,
        "endCard": {**OUTRO_ENDCARD, "seconds": _r(end_seconds or 1.5)},
        "freezeAt": _r(freeze_at),
        # the score's beat in seconds (bot/music.py puts cuts and hits on it): the overlay's reveals keep its tempo
        "beat": round(60.0 / float((sb.get("music") or {}).get("bpm") or 92), 5),
    }
    # the one timing source: particles, caption hits, hook words, end-card letters and every sound effect read it
    props["cues"] = rig.cue_sheet(props, impacts)
    if long_:
        # the long video draws text only where there is some: Remotion renders just those frame ranges (the rest of
        # the clean video is kept as it is), no drifting dust, and the film texture only from the outro on
        props["dust"] = False
        tail = outro["start"] if outro else t - (end_seconds or 1.5)
        props["filmFrom"] = int(round(tail * fps))
        props["ranges"] = text_ranges(props)
        props["alpha"] = alpha_flags(props)
    return props


def _edit_props(sb: dict, width: int, height: int, fps: int) -> dict:
    """Props for a Short in one of the edit styles (bot/edits.py): the storyboard's own caption track (reading-speed
    chunks), its hook, a short end tag over the last shot (no outro, no black end card)."""
    t, impacts, starts, chapters = 0.0, [], [], []
    for k, shot in enumerate(sb["shots"]):
        starts.append(t)
        if "impact" in (shot.get("fx") or []):
            impacts.append((k, t))
        ch = shot.get("chapter")
        if ch:  # the long video's chapter title rides the first shot of its chapter (no title cards)
            chapters.append({"start": _r(t), "seconds": _r(min(CHAPTER_TITLE, float(shot["seconds"]))),
                             "n": int(ch["n"]), "name": ch["name"], "line": ""})
        t += float(shot["seconds"])
    frames = max(1, int(round(t * fps)))
    impact_frames = [first_frame(s0, fps) for _, s0 in impacts]
    caps = []
    for c in sb["captions"]:
        f0 = int(round(c["start"] * fps))
        style = c.get("style", "rise")
        cap = {"text": c["text"], "start": _r(c["start"]), "end": _r(c["end"]), "kind": style,
               "style": "glitch" if style == "slam" else "drift", "emph": emphasis(c["text"]), "tl0": 0.0,
               "enter": "cut", "shake": [], "hits": []}
        # a slam caption lands ON the next impact frame within half a second (the reveal cut)
        hit = next((f for f in impact_frames if 0 <= f - f0 <= fps // 2), None)
        if style == "slam" and hit is not None:
            cap["hits"] = ["impact"]
            cap["hitAt"] = hit - f0
        caps.append(cap)
    hook = sb.get("hook") or {}
    tag = sb.get("end_tag") or {"seconds": 1.3}
    props = {
        "width": width, "height": height, "fps": fps, "durationInFrames": frames,
        "layout": "landscape" if width > height else "portrait",
        "hook": {"title": hook.get("title") or "", "label": hook.get("label") or "",
                 "until": _r(float(hook.get("seconds") or HOOK_SECONDS)),
                 "kind": hook.get("kind") or "rise"} if hook.get("title") else None,
        "captions": caps,
        "chapters": chapters,
        "outro": None,
        "endCard": {**OUTRO_ENDCARD, "byline": "", "seconds": _r(float(tag["seconds"])), "style": "tag"},
        "freezeAt": 0.0,
        "beat": round(60.0 / float((sb.get("music") or {}).get("bpm") or 92), 5),
        # edit v4: the clean video already carries the film grain (bot/grade.py), so the overlay's own is lighter
        "graded": "grade" in sb,
    }
    props["cues"] = sorted(rig.cue_sheet(props, impacts) + rig.edit_cues(sb, fps), key=lambda c: (c["f"], c["kind"]))
    for q in props["cues"]:
        if q["kind"] == "impact" and q["f"] < fps:   # the cold open: the subject's face fills the frame, no shards
            q["burst"] = False
    if sb.get("kind") == "long":
        # text only where there is some (Remotion renders those ranges), no drifting dust, grain from the end tag on
        props["dust"] = False
        props["filmFrom"] = frames - int(round(float(tag["seconds"]) * fps))
        props["ranges"] = text_ranges(props)
        props["alpha"] = alpha_flags(props)
    return props


def text_ranges(props: dict, gap: int = RANGE_GAP) -> list[list[int]]:
    """Inclusive [first, last] frame ranges where the overlay draws anything, merged when closer than `gap` frames.
    Everything else is the clean video, untouched. Always ends with the outro + end card."""
    fps, n = props["fps"], props["durationInFrames"]

    def fr(sec: float) -> int:
        return rig.js_round(sec * fps)

    spans = []
    hook = props.get("hook")
    if hook:
        spans.append((0, fr(hook["until"]) - 1))
    for c in props.get("captions", []):
        spans.append((fr(c["start"]), fr(c["end"]) - 1))
    for ch in props.get("chapters", []):
        spans.append((fr(ch["start"]), fr(ch["start"] + ch["seconds"]) - 1))
    for q in props.get("cues", []):
        if q["kind"] == "impact":
            spans.append((q["f"], q["f"] + rig.BURST_FRAMES))
    spans.append((min(props.get("filmFrom", n), n - fr(props["endCard"]["seconds"])), n - 1))
    out: list[list[int]] = []
    for a, b in sorted((max(0, a), min(n - 1, b)) for a, b in spans if b >= a):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def alpha_flags(props: dict) -> list[bool]:
    """One flag per text range: True = text only, rendered as transparent PNG frames (no picture is decoded, drawn
    or re-encoded by Chrome) and laid over the picture by ffmpeg; False = the range holds the full film texture,
    whose feTurbulence grain blends WITH the picture (mix-blend-mode: overlay), so Remotion renders the picture in.
    A graded video (edit v4: ffmpeg already put the grain in) uses the light film texture: a flat ink flicker and
    faint scanlines, plain alpha both. With no outro (whose freeze frame shows the picture), every range is then
    text only."""
    ranges = props.get("ranges", [])
    if props.get("graded") is True and not props.get("outro"):
        return [True] * len(ranges)
    cut = props.get("filmFrom", props["durationInFrames"])
    return [b < cut for _a, b in ranges]


# ------------------------------------------------------------------------------------------------ tools
def find_browser() -> str | None:
    env = os.environ.get("REMOTION_BROWSER_EXECUTABLE")
    if env:
        return env if Path(env).is_file() else None
    return next((c for c in BROWSER_CANDIDATES if Path(c).is_file()), None)


_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)


def _child_env() -> dict[str, str]:
    """os.environ minus anything that looks like a secret: node, its ~4k npm packages and Chrome never see the keys."""
    env = {k: v for k, v in os.environ.items() if not _SECRET_NAME.search(k)}
    env["REMOTION_BROWSER_EXECUTABLE"] = find_browser() or ""
    return env


def _scrub(text: str) -> str:
    """Blank any secret value out of child output before it lands in logs, qa.txt or artifacts."""
    for k, v in os.environ.items():
        if _SECRET_NAME.search(k) and len(v) >= 8:
            text = text.replace(v, "***")
    return text


def available(mode: str = "auto") -> tuple[bool, str]:
    """(usable, reason). mode 'off' disables the overlay."""
    if mode == "off":
        return False, "BOT_OVERLAY=off"
    if not shutil.which("node"):
        return False, "node not installed"
    if not (PROJECT / "node_modules" / "@remotion" / "renderer").is_dir():
        return False, "render/node_modules missing (npm ci --ignore-scripts in render/)"
    if not find_browser():
        return False, "no local Chrome (set REMOTION_BROWSER_EXECUTABLE)"
    return True, "ok"


def _mux(overlay_mp4: Path, clean_mp4: Path, out: Path) -> None:
    p = subprocess.run([config.ffmpeg_exe(), "-loglevel", "error", "-y", "-i", str(overlay_mp4), "-i", str(clean_mp4),
                        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", "-movflags", "+faststart",
                        str(out)], capture_output=True, text=True)
    if p.returncode != 0:
        raise OverlayError(f"mux failed: {_scrub(p.stderr.strip())[-400:]}")


def _kill_tree(p: subprocess.Popen) -> None:
    """Stop node and every Chrome it started (a plain kill would leave Chrome eating the runner)."""
    try:
        if os.name == "posix":
            os.killpg(p.pid, 9)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    except (ProcessLookupError, OSError):
        pass
    p.wait()


def _plain_names(folder: Path, a: int, b: int) -> None:
    """Remotion zero-pads frame numbers to the width of the range's last frame (element-07.png, element-187.png):
    rename to plain numbers (element-7.png) so one ffmpeg pattern fits every range, and check none is missing."""
    found = {}
    for f in folder.glob("element-*.png"):
        try:
            found[int(f.stem.split("-", 1)[1])] = f
        except ValueError:
            continue
    if sorted(found) != list(range(a, b + 1)):
        raise OverlayError(f"overlay frames {a}-{b} are incomplete ({len(found)} found)")
    for n, f in found.items():
        if f.name != f"element-{n}.png":
            f.rename(folder / f"element-{n}.png")


# the long video's one final encode (single pass or compose), capped at 10 Mbit/s (above YouTube's 8 Mbit/s 1080p24
# advice): film grain at plain crf 18 came out at ~20 Mbit/s (384 MB), more than the free plan's 500 MB of artifacts
FINAL_X264 = ["-c:v", "libx264", "-preset", COMPOSE_PRESET, "-crf", "19", "-maxrate", "10M", "-bufsize", "20M"]
BT709_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


def overlay_graph(parts: list[Path], ranges: list[list[int]], alpha: list[bool], fps: int, first: int,
                  base: str) -> tuple[list[str], list[str], str]:
    """(input args, filter_complex parts, output label) that lay each rendered overlay range over the stream `base`
    at its own frames. Overlay range i is ffmpeg input `first + i`. A text-only range is a folder of transparent
    PNGs (element-<frame>.png, alpha-blended); an opaque range is an mp4 that replaces its frames. Every input is
    streamed: memory stays flat."""
    args: list[str] = []
    for p, (a, _b), is_alpha in zip(parts, ranges, alpha):
        if is_alpha:
            args += ["-framerate", str(fps), "-start_number", str(a), "-i", str(p / "element-%d.png")]
        else:
            args += ["-i", str(p)]
    chain, last = [], base
    for i, ((a, _b), is_alpha) in enumerate(zip(ranges, alpha)):
        n = first + i
        # the PNGs are sRGB: to BT.709 limited-range YUV with the same matrix the picture and Remotion's mp4s use
        conv = "scale=out_color_matrix=bt709:out_range=tv:flags=bicubic,format=yuva420p," if is_alpha else ""
        chain.append(f"[{n}:v]{conv}setpts=PTS-STARTPTS+{a}/({fps}*TB)[s{n}]")
        chain.append(f"[{last}][s{n}]overlay=eof_action=pass:repeatlast=0:format=yuv420[v{n}]")
        last = f"v{n}"
    return args, chain, last


def _compose(clean_mp4: Path, parts: list[Path], ranges: list[list[int]], alpha: list[bool], fps: int,
             out: Path) -> None:
    """Long video with an opaque range (an outro, or an ungraded storyboard): lay each rendered overlay range over
    the clean video, keep the clean audio, one final H.264 encode."""
    args, chain, last = overlay_graph(parts, ranges, alpha, fps, 1, "0:v")
    cmd = [config.ffmpeg_exe(), "-loglevel", "error", "-y", "-i", str(clean_mp4), *args,
           "-filter_complex", ";".join(chain), "-map", f"[{last}]", "-map", "0:a:0",
           *FINAL_X264, "-pix_fmt", "yuv420p", *BT709_TAGS, "-c:a", "copy", "-movflags", "+faststart", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise OverlayError(f"compose failed: {_scrub(p.stderr.strip())[-400:]}")


def timeout_for(props: dict) -> int:
    """Seconds allowed for the node render: a Short keeps TIMEOUT; a long video scales with the frames it renders."""
    ranges = props.get("ranges")
    frames = sum(b - a + 1 for a, b in ranges) if ranges else props["durationInFrames"]
    return int(min(TIMEOUT_MAX, max(TIMEOUT, 120 + frames * SECONDS_PER_FRAME)))


class TextFrames:
    """The long video's overlay, rendered BEFORE the picture: transparent PNG frames per text range, laid over the
    picture inside its one and only encode (bot/render.py). No clean intermediate video is encoded or decoded."""

    def __init__(self, parts: list[Path], ranges: list[list[int]], fps: int, tmp: tempfile.TemporaryDirectory):
        self.parts, self.ranges, self.fps, self._tmp = parts, ranges, fps, tmp

    def graph(self, first: int, base: str) -> tuple[list[str], list[str], str]:
        return overlay_graph(self.parts, self.ranges, [True] * len(self.ranges), self.fps, first, base)

    def close(self) -> None:
        self._tmp.cleanup()


def single_pass_ok(sb: dict, width: int, height: int, fps: int = config.FPS) -> bool:
    """True when every overlay range is text only (the graded long video, no outro): then the overlay frames are
    made first and the picture is encoded once, with the text, instead of clean video -> overlay -> compose."""
    try:
        props = props_from_storyboard(sb, width, height, fps)
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    ranges, alpha = props.get("ranges") or [], props.get("alpha") or []
    return bool(ranges) and len(alpha) == len(ranges) and all(alpha)


def render_text_frames(sb: dict, width: int, height: int, fps: int = config.FPS,
                       timeout: int | None = None) -> TextFrames:
    """Render the long video's text ranges as transparent PNG frames (no picture needed). Raises OverlayError. The
    caller closes the result (its temp folder)."""
    ok, why = available()
    if not ok:
        raise OverlayError(why)
    props = props_from_storyboard(sb, width, height, fps)
    ranges, alpha = props.get("ranges") or [], props.get("alpha") or []
    if not ranges or len(alpha) != len(ranges) or not all(alpha):
        raise OverlayError("not every overlay range is text only: the single pass cannot be used")
    timeout = timeout or timeout_for(props)
    env = _child_env()
    tmp = tempfile.TemporaryDirectory(prefix="overlay-")
    try:
        tdp = Path(tmp.name)
        (tdp / "props.json").write_text(json.dumps(props), encoding="utf-8")
        cmd = [shutil.which("node"), str(ENTRY), "--props", str(tdp / "props.json"), "--out", str(tdp / "overlay.mp4")]
        log.info("remotion text frames: %d of %d frames, chrome %s", sum(b - a + 1 for a, b in ranges),
                 props["durationInFrames"], env["REMOTION_BROWSER_EXECUTABLE"])
        t_node = time.perf_counter()
        p = subprocess.Popen(cmd, cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace", start_new_session=(os.name == "posix"))
        try:
            out, err = p.communicate(timeout=timeout)
            log.info("timing: remotion frames %.0fs", time.perf_counter() - t_node)
        except subprocess.TimeoutExpired as e:
            _kill_tree(p)
            raise OverlayError(f"remotion render timed out after {timeout}s") from e
        parts = [tdp / f"overlay.part{i}" for i in range(len(ranges))]
        if p.returncode != 0 or not all(x.is_dir() for x in parts):
            tail = _scrub((err or out or "").strip())[-800:]
            raise OverlayError(f"remotion render failed (rc={p.returncode}): {tail}")
        for x, (a, b) in zip(parts, ranges):   # a missing frame would silently drop text
            _plain_names(x, a, b)
        return TextFrames(parts, ranges, fps, tmp)
    except BaseException:
        tmp.cleanup()
        raise


def render(clean_mp4: Path, sb: dict, out_mp4: Path, width: int = config.W, height: int = config.H,
           fps: int = config.FPS, timeout: int | None = None) -> Path:
    """Overlay all on-screen text onto clean_mp4 -> out_mp4 (H.264 yuv420p, clean audio copied). Raises OverlayError."""
    ok, why = available()
    if not ok:
        raise OverlayError(why)
    node = shutil.which("node")
    props = props_from_storyboard(sb, width, height, fps)
    props["videoPath"] = str(Path(clean_mp4).resolve())
    timeout = timeout or timeout_for(props)
    env = _child_env()
    with tempfile.TemporaryDirectory(prefix="overlay-") as td:
        tdp = Path(td)
        (tdp / "props.json").write_text(json.dumps(props), encoding="utf-8")
        raw = tdp / "overlay.mp4"
        cmd = [node, str(ENTRY), "--props", str(tdp / "props.json"), "--out", str(raw)]
        log.info("remotion overlay: %d frames, chrome %s", props["durationInFrames"], env["REMOTION_BROWSER_EXECUTABLE"])
        t_node = time.perf_counter()
        p = subprocess.Popen(cmd, cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace", start_new_session=(os.name == "posix"))
        try:
            out, err = p.communicate(timeout=timeout)
            log.info("timing: remotion frames %.0fs", time.perf_counter() - t_node)
        except subprocess.TimeoutExpired as e:
            _kill_tree(p)
            raise OverlayError(f"remotion render timed out after {timeout}s") from e
        ranges, alpha = props.get("ranges"), props.get("alpha") or []
        alpha = alpha if len(alpha) == len(ranges or []) else [False] * len(ranges or [])
        parts = ([tdp / (f"overlay.part{i}" if al else f"overlay.part{i}.mp4") for i, al in enumerate(alpha)]
                 if ranges else [raw])
        if p.returncode != 0 or not all(x.exists() for x in parts):
            tail = _scrub((err or out or "").strip())[-800:]
            raise OverlayError(f"remotion render failed (rc={p.returncode}): {tail}")
        if ranges:
            for x, (a, b), al in zip(parts, ranges, alpha):   # a missing frame would silently drop text
                if al:
                    _plain_names(x, a, b)
            t_comp = time.perf_counter()
            _compose(Path(clean_mp4), parts, ranges, alpha, fps, out_mp4)
            log.info("timing: compose %.0fs", time.perf_counter() - t_comp)
        else:
            _mux(raw, clean_mp4, out_mp4)
    want, got = qa._probe(Path(clean_mp4))["seconds"], qa._probe(Path(out_mp4))["seconds"]
    if abs(want - got) > LENGTH_SLACK:
        raise OverlayError(f"overlaid video is {got:.2f}s but the clean one is {want:.2f}s")
    return out_mp4


def grab(video: Path, seconds: float, out: Path, width: int | None = None) -> Path:
    """One frame of a finished mp4 as a PNG/JPG, via FFmpeg (accurate seek)."""
    vf = ["-vf", f"scale={width}:-2"] if width else []
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([config.ffmpeg_exe(), "-loglevel", "error", "-y", "-i", str(video), "-ss", f"{seconds:.3f}",
                        "-frames:v", "1", *vf, str(out)], capture_output=True, text=True)
    if p.returncode != 0 or not out.exists():
        raise OverlayError(f"could not grab a frame at {seconds:.2f}s: {p.stderr.strip()[-300:]}")
    return out


def sheet_frames(sb: dict, fps: int = config.FPS) -> list[int]:
    """The contact sheet's frame per shot: the first frame at or after 60% of the shot."""
    times, t = [], 0.0
    for shot in sb["shots"]:
        times.append(t + float(shot["seconds"]) * 0.6)
        t += float(shot["seconds"])
    return [math.ceil(s * fps - 1e-6) for s in times]


def sheet_from_folder(want: list[int], folder: Path, out: Path) -> Path:
    """Build the contact sheet from <folder>/<i>.png, the i-th (1-based) of the sorted unique frames in `want`."""
    from .render import _contact
    uniq = sorted(set(want))
    got = sorted(folder.glob("*.png"), key=lambda x: int(x.stem))
    if not got:
        raise OverlayError("no sheet frames were grabbed")
    shots = {}
    for i, n in enumerate(uniq):
        # a shot past the end of the video (rounding) takes the last frame that exists
        with Image.open(got[min(i, len(got) - 1)]) as im:
            shots[n] = im.convert("RGB")
    _contact([shots[n].copy() for n in want], out)
    return out


def contact_sheet(video: Path, sb: dict, out: Path, width: int = config.W, fps: int = config.FPS) -> Path:
    """The review contact sheet (one frame per shot at 60%), grabbed from the finished video in ONE decode pass.
    (One accurate seek per shot decodes the file from its start each time: 46 shots of a 2-minute 1080p video cost
    ~6 minutes on the runner, as much as the whole text overlay.) The long video's single pass grabs the same
    frames inside its QA decode instead (qa.check(grab=...))."""
    want = sheet_frames(sb, fps)
    uniq = sorted(set(want))
    pick = "+".join(f"eq(n,{n})" for n in uniq)
    with tempfile.TemporaryDirectory(prefix="sheet-") as td:
        p = subprocess.run([config.ffmpeg_exe(), "-loglevel", "error", "-y", "-i", str(video), "-an",
                            "-vf", f"select='{pick}',scale={width // 3}:-2", "-fps_mode", "passthrough", "-frames:v", str(len(uniq)),
                            str(Path(td) / "%d.png")], capture_output=True, text=True)
        if p.returncode != 0 or not any(Path(td).glob("*.png")):
            raise OverlayError(f"could not grab the sheet frames: {_scrub(p.stderr.strip())[-300:]}")
        return sheet_from_folder(want, Path(td), out)
