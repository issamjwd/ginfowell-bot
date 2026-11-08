"""Storyboard: the validated JSON contract between planning (bank or Claude) and rendering.

Hand-written validator (no jsonschema dependency) so every rule can carry a plain-words reason. Anything that comes
from an LLM goes through validate() before it can touch the renderer; a failure falls back to the content bank.

Shape (all times in seconds):
{
  "version": 1, "episode_id": "ep-...", "format": "footage"|"animated", "template": "dont_stare",
  "dream_ids": ["halls"], "monster": "Mannequin"|null,
  "title": "...", "description": "...", "tags": ["..."],
  "music": {"bpm": 60-140, "seed": int, "mood": "soft"|"tense", "drop_at": float|null},
  "thumbnail": {"dream": "halls", "text": "..."},
  "synthetic": false,
  "shots": [ {"kind": "clip"|"scene"|"card", "seconds": float, "caption": str|null, "fx": [...],
              "transition": "cut"|"fade"|"whip", ...kind fields} ]
}
Long video ("kind": "long", 1920x1080, LONG_MIN..LONG_MAX seconds, up to 80 shots) adds:
  top level "hook": {"title": str, "label": str}; clip "layout": "full"|"panel" + "backdrop": dream id;
  card "chapter": {"n": 1-20, "name": str} (a chapter title card)
clip:  "clip": footage name, "from": float >= 0, "speed": 0.25-2, "pan": -1..1, "zoom": [a, b] (1-1.6)
scene: "dream": id, "night": bool, "orb": bool, "camera": {"zoom": [a, b], "pan": [a, b]},
       "actors": [{"character", "action", "x": [from, to], "height": 0.1-1.2, "flip": bool, "start": float,
                   "depth": 0-1 (0 = front)}]
card:  "big": str|null, "end": bool
"""
from __future__ import annotations

import re
from typing import Any

from . import bank, config, grade, motion, sprites

FX = {"impact", "shake", "speed_lines", "flash", "freeze", "drawn", "zoom_punch"}
KINDS = {"clip", "scene", "card"}
TRANSITIONS = set(motion.TRANSITIONS)  # cut fade whip flash leak dip zoom glitch (edit v4)
HOOK_KINDS = ("rise", "slam", "stretch", "type")
MOODS = {"soft", "tense"}
MIN_SECONDS, MAX_SECONDS = 8.0, 58.0  # 2 s under the Shorts limit, so encoder rounding never crosses 60
LONG_MIN, LONG_MAX = 105.0, 250.0     # inside QA's long limits (config.LONG_MIN/MAX_SECONDS) with room to spare
KINDS_OF_VIDEO = ("short", "long")
LAYOUTS = ("full", "panel")
CAPTION_STYLES = ("rise", "slam", "type", "rule")
LOOKS = ("clean", "vhs")
MIN_IMPACT_GAP = 1.5  # seconds between flashing fx (photosensitivity)

# Safe characters only: the game's font rule (no tofu glyphs). ASCII printable + these.
SAFE_EXTRA = set("◀▶·…—’‘“”")
BANNED = re.compile(r"\b(blood|bloody|gore|gory|kill|killed|killing|murder|corpse|dead body|suicide|stab|knife)\b",
                    re.IGNORECASE)
ALLOWED_ROOM_NAMES = ("room of windows", "waiting room")  # proper names, not dreams called rooms


class StoryboardError(ValueError):
    """The storyboard breaks a rule. `errors` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _text_problems(label: str, s: Any, max_len: int, lowercase: bool) -> list[str]:
    if s is None:
        return []
    if not isinstance(s, str):
        return [f"{label}: must be text"]
    errs = []
    if len(s) > max_len:
        errs.append(f"{label}: longer than {max_len} characters")
    bad = {c for c in s if not (32 <= ord(c) < 127 or c in SAFE_EXTRA or c == "\n")}
    if bad:
        errs.append(f"{label}: unsafe characters {sorted(bad)!r} (render as boxes)")
    if BANNED.search(s):
        errs.append(f"{label}: gory word (liminality is never gory)")
    low = s.lower()
    for name in ALLOWED_ROOM_NAMES:
        low = low.replace(name, "")
    if re.search(r"\brooms?\b", low):
        errs.append(f"{label}: say 'dreams', never 'rooms'")
    if lowercase and s != s.lower():
        errs.append(f"{label}: captions are lowercase")
    return errs


def _num(label: str, v: Any, lo: float, hi: float) -> list[str]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return [f"{label}: must be a number"]
    return [] if lo <= v <= hi else [f"{label}: {v} not in [{lo}, {hi}]"]


def _pair(label: str, v: Any, lo: float, hi: float) -> list[str]:
    if not (isinstance(v, list) and len(v) == 2):
        return [f"{label}: must be [from, to]"]
    return _num(label + "[0]", v[0], lo, hi) + _num(label + "[1]", v[1], lo, hi)


def _motion_problems(p: str, s: dict) -> list[str]:
    """Edit v4 camera fields (bot/motion.py): small, finite numbers only, so no storyboard can shake the frame off."""
    errs: list[str] = []
    secs = s.get("seconds")
    secs = float(secs) if isinstance(secs, (int, float)) and not isinstance(secs, bool) else 0.0
    if "hand" in s:
        errs += _num(p + ".hand", s["hand"], 0.0, 0.03)
    if "bob" in s:
        errs += _num(p + ".bob", s["bob"], 0.0, 0.02)
    if "punch" in s:
        errs += _num(p + ".punch", s["punch"], 0.0, 0.2)
    if "punches" in s:
        ps = s["punches"]
        if not isinstance(ps, list) or len(ps) > 8:
            errs.append(f"{p}.punches: a list of up to 8 times")
        else:
            for j, t in enumerate(ps):
                errs += _num(f"{p}.punches[{j}]", t, 0.0, secs)
    if "ramp" in s:
        r = s["ramp"]
        if not isinstance(r, list) or not 1 <= len(r) <= 4 or not all(isinstance(x, list) and len(x) == 2 for x in r):
            errs.append(f"{p}.ramp: 1 to 4 [time, speed] pairs")
        else:
            for j, (t, v) in enumerate(r):
                errs += _num(f"{p}.ramp[{j}][0]", t, 0.0, secs)
                # slow motion and freezes only: a ramp never plays MORE clip than the shot's length (no overrun)
                errs += _num(f"{p}.ramp[{j}][1]", v, 0.0, 1.0)
    return errs


def _shot(i: int, s: dict, fmt: str, footage: set[str] | None, dream_ids: set[str]) -> list[str]:
    p = f"shots[{i}]"
    if not isinstance(s, dict):
        return [f"{p}: must be an object"]
    errs: list[str] = []
    kind = s.get("kind")
    if kind not in KINDS:
        return [f"{p}.kind: must be one of {sorted(KINDS)}"]
    errs += _num(p + ".seconds", s.get("seconds"), 0.5, 15.0)
    errs += _text_problems(p + ".caption", s.get("caption"), 60, lowercase=True)
    fx = s.get("fx", [])
    if not isinstance(fx, list) or not set(fx) <= FX:
        errs.append(f"{p}.fx: only {sorted(FX)}")
    tr = s.get("transition", "cut")
    if not isinstance(tr, str) or tr not in TRANSITIONS:
        errs.append(f"{p}.transition: one of {sorted(TRANSITIONS)}")
    if kind == "clip":
        if fmt != "footage":
            errs.append(f"{p}: clip shots belong to footage episodes")
        clip = s.get("clip")
        if not isinstance(clip, str) or (footage is not None and clip not in footage):
            errs.append(f"{p}.clip: unknown footage {clip!r}")
        errs += _num(p + ".from", s.get("from", 0.0), 0.0, 600.0)
        errs += _num(p + ".speed", s.get("speed", 1.0), 0.25, 2.0)
        errs += _num(p + ".pan", s.get("pan", 0.0), -1.0, 1.0)
        errs += _pair(p + ".zoom", s.get("zoom", [1.0, 1.0]), 1.0, 1.6)
        if s.get("chapter") is not None:
            errs += _chapter(p, s["chapter"])
        errs += _motion_problems(p, s)
        if s.get("layout", "full") not in LAYOUTS:
            errs.append(f"{p}.layout: one of {LAYOUTS}")
        if "backdrop" in s and s["backdrop"] not in dream_ids:
            errs.append(f"{p}.backdrop: unknown dream {s['backdrop']!r}")
    elif kind == "scene":
        if s.get("dream") not in dream_ids:
            errs.append(f"{p}.dream: unknown dream {s.get('dream')!r}")
        cam = s.get("camera", {})
        if not isinstance(cam, dict):
            errs.append(f"{p}.camera: must be an object")
        else:
            errs += _pair(p + ".camera.zoom", cam.get("zoom", [1.0, 1.0]), 1.0, 1.8)
            errs += _pair(p + ".camera.pan", cam.get("pan", [0.0, 0.0]), -1.0, 1.0)
        actors = s.get("actors", [])
        if not isinstance(actors, list) or len(actors) > 3:
            errs.append(f"{p}.actors: a list of at most 3")
            actors = []
        for j, a in enumerate(actors):
            q = f"{p}.actors[{j}]"
            if not isinstance(a, dict):
                errs.append(f"{q}: must be an object")
                continue
            if a.get("character") not in sprites.CHARACTERS:
                errs.append(f"{q}.character: one of {sprites.CHARACTERS}")
            if a.get("action") not in sprites.ACTIONS:
                errs.append(f"{q}.action: one of {sorted(sprites.ACTIONS)}")
            errs += _pair(q + ".x", a.get("x", [0.5, 0.5]), -0.5, 1.5)
            errs += _num(q + ".height", a.get("height", 0.45), 0.1, 1.6)
            errs += _num(q + ".start", a.get("start", 0.0), 0.0, 15.0)
            errs += _num(q + ".depth", a.get("depth", 0.0), 0.0, 1.0)
            errs += _num(q + ".speed", a.get("speed", 1.0), 0.5, 3.0)
            if "ground" in a:
                errs += _num(q + ".ground", a["ground"], 0.4, 2.2)
    else:
        if fmt == "footage" and s.get("big") is None and not s.get("end"):
            pass  # plain text card is fine in both formats
        errs += _text_problems(p + ".big", s.get("big"), 24, lowercase=True)
        if s.get("chapter") is not None:
            errs += _chapter(p, s["chapter"])
    return errs


def _chapter(p: str, ch: Any) -> list[str]:
    """A chapter mark: on a title card (older long videos) or riding a clip shot (the title over footage)."""
    if not isinstance(ch, dict):
        return [f"{p}.chapter: must be an object"]
    errs = _num(p + ".chapter.n", ch.get("n"), 1, 20)
    if not isinstance(ch.get("name"), str) or not ch["name"].strip():
        errs.append(f"{p}.chapter.name: required")
    return errs + _text_problems(p + ".chapter.name", ch.get("name"), 40, lowercase=True)


def validate(sb: Any, footage: set[str] | None = None) -> dict:
    """Return the storyboard unchanged if valid, else raise StoryboardError with every problem.

    footage: the clip names available this run (None = don't check names, e.g. offline tests)."""
    if not isinstance(sb, dict):
        raise StoryboardError(["storyboard must be an object"])
    errs: list[str] = []
    dream_ids = {d.id for d in bank.dreams()}
    if sb.get("version") != 1:
        errs.append("version: must be 1")
    video = sb.get("kind", "short")
    if video not in KINDS_OF_VIDEO:
        errs.append(f"kind: one of {KINDS_OF_VIDEO}")
    long_ = video == "long"
    hook = sb.get("hook")
    if hook is not None:
        if not isinstance(hook, dict):
            errs.append("hook: must be an object")
        else:
            errs += _text_problems("hook.title", hook.get("title"), 60, lowercase=True)
            errs += _text_problems("hook.label", hook.get("label"), 40, lowercase=True)
            if hook.get("kind", "rise") not in HOOK_KINDS:
                errs.append(f"hook.kind: one of {HOOK_KINDS}")
    fmt = sb.get("format")
    if fmt not in ("footage", "animated"):
        errs.append("format: footage or animated")
    if not isinstance(sb.get("episode_id"), str) or not re.fullmatch(r"ep-[a-z0-9-]{4,64}", sb.get("episode_id", "")):
        errs.append("episode_id: ep-<lowercase letters, digits, dashes>")
    if not isinstance(sb.get("template"), str):
        errs.append("template: required")
    ds = sb.get("dream_ids")
    if not isinstance(ds, list) or not ds or not set(ds) <= dream_ids:
        errs.append("dream_ids: non-empty list of known dreams")
    if sb.get("monster") not in (None, *sprites.CHARACTERS[1:]):
        errs.append("monster: null or a cast monster")
    title = sb.get("title")
    errs += _text_problems("title", title, 100, lowercase=False)
    if not isinstance(title, str) or not title.strip():
        errs.append("title: required")
    errs += _text_problems("description", sb.get("description", ""), 4500, lowercase=False)
    track = sb.get("captions")
    if track is not None:
        if not isinstance(track, list) or len(track) > 20:
            errs.append("captions: a list of at most 20 chunks")
            track = []
        last_end = 0.0
        for j, c in enumerate(track):
            q = f"captions[{j}]"
            if not isinstance(c, dict):
                errs.append(f"{q}: must be an object")
                continue
            errs += _text_problems(q + ".text", c.get("text"), 60, lowercase=True)
            errs += _num(q + ".start", c.get("start"), 0.0, 300.0) + _num(q + ".end", c.get("end"), 0.0, 300.0)
            if isinstance(c.get("start"), (int, float)) and isinstance(c.get("end"), (int, float)):
                if c["end"] - c["start"] < 1.0 or c["start"] < last_end - 1e-6:
                    errs.append(f"{q}: on screen under 1 s or overlapping the previous chunk")
                last_end = c["end"]
            if c.get("style") not in CAPTION_STYLES:
                errs.append(f"{q}.style: one of {CAPTION_STYLES}")
    if "grade" in sb and not (isinstance(sb["grade"], str) and sb["grade"] in grade.LOOKS):
        errs.append(f"grade: one of {sorted(grade.LOOKS)}")
    if sb.get("look", "clean") not in LOOKS:
        errs.append(f"look: one of {LOOKS}")
    tag = sb.get("end_tag")
    if tag is not None:
        if not isinstance(tag, dict):
            errs.append("end_tag: must be an object")
        else:
            errs += _num("end_tag.seconds", tag.get("seconds"), 0.8, 6.0 if long_ else 3.0)
            errs += _text_problems("end_tag.text", tag.get("text"), 40, lowercase=True)
    tags = sb.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags) or sum(len(t) for t in tags) > 450:
        errs.append("tags: list of text, under 450 characters in total")
    music = sb.get("music", {})
    if not isinstance(music, dict):
        errs.append("music: must be an object")
    else:
        errs += _num("music.bpm", music.get("bpm"), 60, 140)
        if music.get("mood") not in MOODS:
            errs.append("music.mood: soft or tense")
        if not isinstance(music.get("seed"), int):
            errs.append("music.seed: integer")
        if music.get("bed", "full") not in ("full", "drone", "pulse", "box"):
            errs.append("music.bed: full, drone, pulse or box")
        d0, r0 = music.get("drop_at"), music.get("resume_at")
        if d0 is not None and r0 is not None and not (isinstance(d0, (int, float)) and isinstance(r0, (int, float))
                                                      and 0 <= d0 <= r0):
            errs.append("music: drop_at must come before resume_at")
    th = sb.get("thumbnail", {})
    if not isinstance(th, dict) or th.get("dream") not in dream_ids:
        errs.append("thumbnail.dream: a known dream")
    else:
        errs += _text_problems("thumbnail.text", th.get("text"), 40, lowercase=True)
    if not isinstance(sb.get("synthetic", False), bool):
        errs.append("synthetic: true or false")
    shots = sb.get("shots")
    most = 80 if long_ else 30
    if not isinstance(shots, list) or not (2 <= len(shots) <= most):
        errs.append(f"shots: 2 to {most} shots")
        shots = []
    total = 0.0
    last_flash = -99.0
    for i, s in enumerate(shots):
        errs += _shot(i, s, fmt, footage, dream_ids)
        if isinstance(s, dict) and isinstance(s.get("seconds"), (int, float)):
            if {"impact", "flash"} & set(s.get("fx", []) or []) or s.get("transition") == "flash":
                if total - last_flash < MIN_IMPACT_GAP:
                    errs.append(f"shots[{i}]: flashes closer than {MIN_IMPACT_GAP}s apart (photosensitivity)")
                last_flash = total
            total += float(s["seconds"])
    if isinstance(sb.get("captions"), list) and shots:
        if any(isinstance(c, dict) and isinstance(c.get("end"), (int, float)) and c["end"] > total + 1e-6
               for c in sb["captions"]):
            errs.append("captions: a chunk runs past the end of the video")
    lo, hi = (LONG_MIN, LONG_MAX) if long_ else (MIN_SECONDS, MAX_SECONDS)
    if shots and not (lo <= total <= hi):
        errs.append(f"total length {total:.1f}s not in [{lo}, {hi}]")
    if errs:
        raise StoryboardError(errs)
    return sb


def total_seconds(sb: dict) -> float:
    return sum(float(s["seconds"]) for s in sb["shots"])


def max_seconds() -> float:
    return min(MAX_SECONDS, config.MAX_SECONDS - 2)
