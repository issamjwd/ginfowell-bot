"""The long video: a 16:9 "dream tour" (1920x1080, about 2 minutes), one chapter per dream of the CURRENT game.

Same bar as the Shorts (owner feedback 2026-11-04, bot/edits.py):
    cold open: a scare clip at its peak with a hit, the big hook over it (a monster on screen in the first second)
    -> a whip montage of monsters from the other dreams -> one held shot with the intro line (the "0:00" chapter is
       >= 10 s, so YouTube shows chapters)
    chapter x N: the chapter title rides the first shot for 1.5 s (no static title cards); 9 full-frame shots of
                 1.5-3.5 s from that dream's clips (each clip's most dynamic segment, a camera push on every shot, no
                 moment shown twice), a line at reading speed after the title, and the dream's scare / chase clip at
                 the end with the impact and the turn line
    end: the "liminality / free on roblox" tag rides the last 5 s of footage (room for YouTube's end-screen
         elements), no black card

Footage: full-frame 16:9 from the wide pack (release footage-v2-wide, tools/make_footage_release.py --wide). Only
dreams with tagged clips of the upgraded game are used (pre-upgrade footage never appears). If only the 8:9 Shorts
pack is on disk, shots fall back to the 8:9 panel layout (sharp, never upscaled).

Text is a caption track (bot/captions.py: <= 2.6 words a second, >= 1.2 s held), drawn by the overlay like the
Shorts' (bot/overlay.py _edit_props); chapter titles come from the shots.

Rotation (its own, separate from the Shorts): the fingerprint (chapter dreams in order + lines) never repeats; the
lead dream, the hook line and the title differ from the last few long videos; the music seed is new for WINDOW_DAYS.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from datetime import datetime, timezone

from . import bank, captions, footage, metadata, storyboard, variety
from .templates import beats

log = logging.getLogger(__name__)

CHAPTERS = (6, 6)       # chapters per video. Every current-game dream is in the rotation (13 since footage-v3, re-shot photoreal in footage-v4);
                        # 6 a video keeps it ~2 minutes and inside the free runner minutes (README "COSTS / LIMITS")
SHOTS_PER_CHAPTER = 9
MIN_SHOT, MAX_SHOT = 1.2, 3.5
CHAPTER_TITLE = 1.5     # seconds the chapter title rides its first shot
END_TAG = 5.0           # the end tag rides the last 5 s (YouTube end-screen elements need >= 5 s)
MIN_CLIPS = 4           # a dream needs this many current-game clips to get a chapter
TIGHT = (1.12, 1.15)    # zoom of a "tight" shot (every 3rd shot of a chapter): a new framing, not a blurry crop
LONG_RECENT = 3
HOOKS = ("you fell asleep again.", "{count} dreams. one way home.", "this is where you go when you sleep.",
         "stay quiet. take the tour.", "welcome back, dreamer.", "you've been here before.")
INTRO_LINES = ("pov: you fell asleep", "close your eyes.", "you're dreaming again.")


class LongPlanError(RuntimeError):
    """No fresh long video could be planned."""


def fingerprint(dream_ids: list[str], lines: dict[str, str]) -> str:
    blob = json.dumps({"k": "long", "d": list(dream_ids), "l": sorted(lines.items())}, sort_keys=True,
                      ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _fill(raw: str, d: bank.Dream) -> str:
    return bank.fill(raw, d, d.monster)


def current_clips(d: bank.Dream, clips: dict[str, float], tagged: dict[str, dict]) -> list[str]:
    """The dream's clips from the upgraded game: on disk and tagged (pre-upgrade clips carry no tags)."""
    return [c for c in d.footage if c in clips and tagged.get(c, {}).get("tags")]


def _has(c: str, tagged: dict, *names: str) -> bool:
    return any(n in tagged.get(c, {}).get("tags", []) for n in names)


class _Cutter:
    """Makes shots: the most dynamic free segment of a clip, a camera push on every shot, no moment twice."""

    def __init__(self, clips: dict[str, float], rng: random.Random, layout: str):
        self.clips, self.rng, self.layout = clips, rng, layout
        self.used: dict[str, list] = {}

    def times_used(self, clip: str) -> int:
        return len(self.used.get(clip, []))

    def free(self, clip: str, secs: float) -> bool:
        """Is there still a fresh `secs`-long moment in this clip?"""
        taken = sorted(self.used.get(clip, []))
        edges = [0.0] + [b for _, b in taken]
        ends = [a for a, _ in taken] + [self.clips[clip] - 0.15]
        return any(e - s0 >= secs - 1e-6 for s0, e in zip(edges, ends))

    def shot(self, d: bank.Dream, clip: str, secs: float, *, fx: list | None = None, transition: str = "cut",
             prefer: float | None = None, chapter: dict | None = None, alts: list[str] | None = None,
             tight: bool = False, avoid: str | None = None) -> dict:
        if clip == avoid:               # it just played (a stand-in took it earlier): use another of the clips
            clip = next((c for c in (alts or []) if c != avoid and self.free(c, secs)), clip)
        if not self.free(clip, secs):   # this clip is used up: another of the dream's clips, else a shorter shot
            # `avoid` = the clip just shown: a stand-in is never the same clip twice in a row
            clip = next((c for c in (alts or []) if c != avoid and self.free(c, secs)),
                        next((c for c in (alts or []) if self.free(c, secs)), clip))
            while not self.free(clip, secs) and secs - 0.25 >= MIN_SHOT:
                secs = round(secs - 0.25, 3)
            prefer = None
            if not self.free(clip, secs):  # never show a moment twice or freeze past a clip's end: plan again
                raise captions.EditError(f"no fresh moment left in {clip} for a {secs:.2f} s shot")
        taken = self.used.setdefault(clip, [])
        start = footage.best_start(clip, secs, self.clips[clip], avoid=taken, rng=self.rng, prefer=prefer)
        taken.append((start, start + secs))
        pan = 0.0
        if tight:   # a closer framing off to one side: reads as a new angle on the same place (<= 1.24x stays sharp)
            z0 = round(self.rng.uniform(TIGHT[0], TIGHT[1]), 3)
            push = round(self.rng.uniform(0.05, 0.08), 3)
            pan = round(self.rng.choice((-1, 1)) * self.rng.uniform(0.35, 0.7), 2)
        else:
            z0 = round(self.rng.uniform(1.0, 1.05), 3)
            push = round(self.rng.uniform(0.07, 0.14), 3)
        zoom = [z0, round(z0 + push, 3)] if self.rng.random() < 0.7 else [round(z0 + push, 3), z0]
        s = {"kind": "clip", "seconds": round(secs, 3), "clip": clip, "from": start, "speed": 1.0, "pan": pan,
             "zoom": zoom, "caption": None, "fx": fx or [], "transition": transition}
        if self.layout == "panel":
            s.update(layout="panel", backdrop=d.id)
        if chapter:
            s["chapter"] = chapter
        return s


def _beats_secs(rng: random.Random, bpm: int, lo: int = 2, hi: int = 4) -> float:
    return max(MIN_SHOT, min(MAX_SHOT, beats(rng.randint(lo, hi), bpm)))


def _motion(c: str, tagged: dict) -> str:
    return next((t for t in tagged.get(c, {}).get("tags", []) if t.startswith("motion=")), "")


def _vary(body: list[str], need: int, tagged: dict, cut: _Cutter) -> list[str]:
    """`need` clips from `body` in its order of preference, but never the same clip twice in a row, and the camera
    move (dolly / handheld / pan) changes from shot to shot where the dream has the clips for it."""
    count = {c: 0 for c in body}
    out: list[str] = []
    for _ in range(need):
        prev = out[-1] if out else None
        pool = [c for c in body if c != prev] or body

        def rank(c, prev=prev):
            return (count[c],                                         # the least used in this chapter first
                    cut.times_used(c) > 0,                            # then clips not yet used in this video
                    prev is not None and _motion(c, tagged) == _motion(prev, tagged),   # then a new camera move
                    body.index(c))
        c = min(pool, key=rank)
        count[c] += 1
        out.append(c)
    return out


def chapter(n: int, d: bank.Dream, bpm: int, cut: _Cutter, tagged: dict, own: list[str],
            last: bool = False) -> list[dict]:
    """One dream's chapter: SHOTS_PER_CHAPTER shots, the title over the first, the scare at the end."""
    rng = cut.rng
    scares = [c for c in own if _has(c, tagged, "scare", "chase")]
    finale = rng.choice(scares) if scares else None
    body = [c for c in own if c != finale]
    rng.shuffle(body)
    # calm establishing shots first, monsters later in the chapter (it builds), every clip before any repeat
    body.sort(key=lambda c: _has(c, tagged, "monster"))
    # clips not used yet in this video first; a small dream cycles through its clips (other segments) before any
    # clip comes back
    body.sort(key=lambda c: cut.times_used(c) > 0)
    need = SHOTS_PER_CHAPTER - (1 if finale else 0)
    order = _vary(body, need, tagged, cut) if body else []
    shots = []
    for i, c in enumerate(order):
        secs = max(_beats_secs(rng, bpm, 3, 4), CHAPTER_TITLE + 0.4) if i == 0 else _beats_secs(rng, bpm)
        if last and i >= len(order) - (1 if finale else 2):
            secs = max(secs, END_TAG / 2 + 0.05)   # the end tag rides the last two shots (>= 5 s)
        shots.append(cut.shot(d, c, secs, transition="fade" if i == 0 else rng.choice(["cut", "cut", "whip"]),
                              chapter={"n": n, "name": d.name} if i == 0 else None, alts=body or own,
                              tight=i % 3 == 2, avoid=shots[-1]["clip"] if shots else None))
    if finale:
        secs = 3.0 if last else _beats_secs(rng, bpm, 3, 4)
        shots.append(cut.shot(d, finale, secs, fx=["impact"], prefer=tagged[finale].get("peak"), alts=scares,
                              avoid=shots[-1]["clip"] if shots else None))
    return _no_repeats(shots, fixed_tail=2 if last else 1)


def _no_repeats(shots: list[dict], fixed_tail: int) -> list[dict]:
    """When a small dream ran out of fresh clips and the same clip landed twice in a row, move the earlier one to a
    spot where it touches neither neighbour. The titled first shot and the last `fixed_tail` shots stay put."""
    def bad(k):
        return 0 < k < len(shots) and shots[k - 1]["clip"] == shots[k]["clip"]

    for k in range(1, len(shots)):
        if not bad(k):
            continue
        i = k - 1 if k - 1 >= 1 and k - 1 < len(shots) - fixed_tail else k
        if not (1 <= i < len(shots) - fixed_tail):
            continue
        for j in range(1, len(shots) - fixed_tail):
            if j == i:
                continue
            shots[i], shots[j] = shots[j], shots[i]
            if not any(bad(x) for x in range(1, len(shots))):
                break
            shots[i], shots[j] = shots[j], shots[i]
    return shots


def _pick_dreams(rng: random.Random, longs: list[dict], ready: list[bank.Dream]) -> list[bank.Dream]:
    """CHAPTERS dreams from every current-game dream, least recently featured first (all of them come round every
    two or three videos), shuffled, with a new lead each time."""
    leads = {(r.get("dream_ids") or [None])[0] for r in longs[-LONG_RECENT:]}
    n = min(len(ready), rng.choice(CHAPTERS))
    last_seen = {}
    for age, r in enumerate(reversed(longs)):
        for d in r.get("dream_ids") or []:
            last_seen.setdefault(d, age)
    order = list(ready)
    rng.shuffle(order)
    order.sort(key=lambda d: -last_seen.get(d.id, 10**6))   # never shown, or shown longest ago, first
    pick = order[:n]
    rng.shuffle(pick)
    lead = sorted(pick, key=lambda d: d.id in leads)[0]
    return [lead] + [d for d in pick if d is not lead]


def _lines(rng: random.Random, dreams: list[bank.Dream], longs: list[dict]) -> dict[str, str]:
    """Raw (unfilled) lines: hook, intro, and per chapter a short 'see' line and the 'turn' line. No line twice."""
    lb = bank.lines()
    used_hooks = {(r.get("lines") or {}).get("hook") for r in longs[-LONG_RECENT:]}
    taken: set[str] = set()

    def take(pool) -> str:
        fresh = [x for x in pool if x not in taken] or list(pool)
        x = rng.choice(fresh)
        taken.add(x)
        return x

    out = {"hook": take([h for h in HOOKS if h not in used_hooks] or HOOKS), "intro": take(INTRO_LINES)}
    for i, _ in enumerate(dreams, 1):
        out[f"c{i}_see"] = take([x for x in lb["middle"] if "{" not in x and len(x.split()) <= 6])
        out[f"c{i}_turn"] = take([x for x in lb["turn"] if len(x.split()) <= 4])
    return out


def plan_long(history: list[dict], used: set[str], clips: dict[str, float], seed: int | None = None,
              now: datetime | None = None, tries: int = 200) -> dict:
    """Return a validated long-video storyboard."""
    tagged = footage.tags()
    ready = [d for d in bank.dreams() if len(current_clips(d, clips, tagged)) >= MIN_CLIPS]
    if len(ready) < min(CHAPTERS):
        raise LongPlanError(f"the long video needs {min(CHAPTERS)} dreams of current-game footage "
                            f"(release footage-v2-wide); found {len(ready)}")
    layout = "full" if footage.is_wide() else "panel"
    if layout == "panel":
        log.warning("footage is not the 16:9 pack (footage-v2-wide): the long video falls back to 8:9 panels")
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed)
    longs = variety.of_kind(history, "long")
    recent_titles = {r.get("title") for r in longs[-2:]}
    window = variety.within(longs, now, variety.WINDOW_DAYS)
    for _ in range(tries):
        dreams = _pick_dreams(rng, longs, ready)
        lines = _lines(rng, dreams, longs)
        fp = fingerprint([d.id for d in dreams], lines)
        if fp in used:
            continue
        bpm = rng.randint(80, 90)
        seed_ = rng.randrange(1, 2**31)
        if any(r.get("music_seed") == seed_ % 100000 for r in window):
            continue
        try:
            sb = assemble(dreams, lines, bpm, seed_, fp, clips, rng, now, layout)
        except (storyboard.StoryboardError, captions.EditError):
            continue
        if sb["title"] in recent_titles:
            continue
        return sb
    raise LongPlanError("no fresh long video; add lines or dreams to bot/data")


def assemble(dreams: list[bank.Dream], lines: dict[str, str], bpm: int, seed: int, fp: str,
             clips: dict[str, float], rng: random.Random, now: datetime, layout: str = "full") -> dict:
    b = bpm
    lead = dreams[0]
    tagged = footage.tags()
    cut = _Cutter(clips, rng, layout)
    own = {d.id: current_clips(d, clips, tagged) for d in dreams}
    shots: list[dict] = []
    # cold open: a scare clip at its peak (the hook lands over it), then monsters from the other dreams
    peaked = [c for d in dreams for c in own[d.id] if _has(c, tagged, "scare") and tagged[c].get("peak") is not None]
    if not peaked:
        raise captions.EditError("no scare clip with a peak for the cold open")
    ok = set(footage.cold_open_ok())     # the Shorts' by-eye list of big, well lit monsters: never a dark opener
    cold = rng.choice([c for c in peaked if c in ok] or peaked)
    owner = next(d for d in dreams if cold in own[d.id])
    # (best_start backs up 35% of the shot from the asked moment: +0.3 puts the peak ~0.5 s in)
    shots.append(cut.shot(owner, cold, beats(3, b), fx=["impact"], prefer=tagged[cold]["peak"] + 0.3))
    for d in [x for x in dreams if x is not owner][:4]:
        monsters = [c for c in own[d.id] if _has(c, tagged, "monster") and c != cold] or own[d.id]
        shots.append(cut.shot(d, rng.choice(monsters), beats(2, b), transition="whip"))
    intro_at = sum(s["seconds"] for s in shots)
    calm = [c for c in own[lead.id] if not _has(c, tagged, "scare", "chase")] or own[lead.id]
    shots.append(cut.shot(lead, rng.choice(calm), max(_beats_secs(rng, b, 4, 4), 10.4 - intro_at),
                          transition="fade"))
    marks = [(0.0, "falling asleep")]
    plan: list[tuple[str, float, str]] = [(_fill(lines["intro"], lead), intro_at + 0.2, "rise")]
    for i, d in enumerate(dreams, 1):
        start = sum(s["seconds"] for s in shots)
        marks.append((start, d.name))
        ch = chapter(i, d, b, cut, tagged, own[d.id], last=i == len(dreams))
        plan.append((_fill(lines[f"c{i}_see"], d), start + CHAPTER_TITLE + 0.25, "rise"))
        if "impact" in ch[-1]["fx"]:
            turn_at = start + sum(s["seconds"] for s in ch[:-1])
            plan.append((_fill(lines[f"c{i}_turn"], d), turn_at - 0.3, "slam"))
        shots += ch
    if sum(s["seconds"] for s in shots[-2:]) < END_TAG - 1e-6:
        raise captions.EditError("the last shots are too short for the end tag")
    _edit_motion(shots, seed)
    total = sum(s["seconds"] for s in shots)
    hook = bank.fill(lines["hook"], lead)
    hook_secs = captions.hold_for(hook)
    track = captions.track(plan, hook_end=hook_secs, end=total)
    meta = metadata.build_long(dreams, marks, lines, rng)
    day = now.strftime("%Y%m%d")
    sb = {
        "version": 1, "kind": "long", "episode_id": f"ep-{day}-long-{fp[:8]}", "format": "footage",
        "template": "long_tour", "dream_ids": [d.id for d in dreams], "monster": None, "fingerprint": fp,
        "lines": dict(lines),
        "hook": {"title": hook, "label": f"{len(dreams)} dreams", "seconds": round(hook_secs, 3), "kind": "stretch"},
        "captions": track,
        "end_tag": {"seconds": END_TAG, "text": "liminality: free on roblox"},
        "look": "clean",
        "grade": "long",
        "music": {"bpm": bpm, "seed": seed % 100000, "mood": "tense", "bed": "pulse", "drop_at": None},
        "synthetic": False, "shots": shots, **meta,
    }
    return storyboard.validate(sb, footage=set(clips))


HAND = 0.006           # the long video's handheld drift (a calmer camera than the Shorts')
RAMP_S, RAMP_SPEED = 0.6, 0.45   # slow motion into each chapter's scare


def _edit_motion(shots: list[dict], seed: int) -> None:
    """Edit v4 motion for the long video, in place (its own Random, so the plan itself is unchanged): a handheld
    camera on every shot, chapters open alternately out of black (dip: a sub drop) or through a light-leak burn (a
    reversed swell), a third of the whips become zoom-throughs, and the shot before each chapter's scare slows to
    RAMP_SPEED over its last RAMP_S seconds (a riser) and snaps back on the impact cut."""
    r = random.Random(seed ^ 0x5EED)
    n_ch = 0
    for k, s in enumerate(shots):
        if s["kind"] != "clip":
            continue
        s["hand"] = HAND
        if k == 0:
            continue
        if s.get("chapter"):
            s["transition"] = ("dip", "leak")[n_ch % 2]
            n_ch += 1
        elif s["transition"] == "whip" and r.random() < 0.34:
            s["transition"] = "zoom"
        prev = shots[k - 1]
        if "impact" in (s.get("fx") or []) and prev["kind"] == "clip" and not prev.get("chapter")                 and float(prev["seconds"]) >= RAMP_S + 0.6:
            prev["ramp"] = [[round(float(prev["seconds"]) - RAMP_S, 3), RAMP_SPEED]]


def chapter_marks(sb: dict) -> list[tuple[float, str]]:
    """(start seconds, name) of each chapter, from a storyboard (for tests and the description)."""
    out, t = [], 0.0
    for s in sb["shots"]:
        if s.get("chapter"):
            out.append((t, s["chapter"]["name"]))
        t += float(s["seconds"])
    return out


__all__ = ["plan_long", "fingerprint", "chapter_marks", "LongPlanError", "current_clips"]
