"""The Shorts' edit styles (v2, owner feedback 2026-11-04). Footage only: every shot is moving gameplay with a camera
move on top; nothing is ever a still image.

Rules every style keeps:
  * the first frame is the scariest one: a clip tagged monster/scare (bot/data/footage_tags.json) at its peak, with a
    flash cut, a zoom punch, real shake and a hard hit on the score; the big hook line lands over it
  * a cut every 1-2.5 s (whole beats of the score), never a shot over MAX_SHOT seconds, never the same clip twice
  * a camera move on every shot (zoom ramp and / or pan), the most dynamic bright segment of each clip
  * the turn: a second scary clip (or the most dynamic one left) with the impact and the turn line; a 1.3 s end tag
    rides the top of that same shot: no black end card, it ends on a strong frame and loops back into the cold open
  * text moves at reading speed (bot/captions.py): short chunks, held, fewer words rather than faster ones

Styles (rotated by the planner and the variety guard, never twice in a row):
  chase_cut       fast: 1-1.5 s cuts on a quick tempo, whips, shakes, slam captions, pulse bed
  slow_dread      slow: 2.3-2.5 s shots, fades, slow push-ins, captions that rise, drone bed
  found_footage   VHS tape look, handheld wobble, hard cuts, typed captions, drone bed
  rules_list      "rules of the dream": numbered rule cards over the montage, music-box bed
  monster_reveal  cold open, then calm dreams, then the hard reveal, drone bed into a hit
  pov_walk        forward push-ins like walking, "pov:" captions, pulse bed
"""
from __future__ import annotations

import random

from . import bank, captions, footage
from .templates import Pick, beats

MAX_SHOT = 2.5
END_TAG = 1.3
COLD_LEAD = 0.3   # the cold open starts this long before the scare's peak: the monster is big from frame 0,
                  # its peak lands ~0.3 s in

STYLES: dict[str, dict] = {
    "chase_cut": {"bpm": (104, 116), "beats": (2, 3), "shots": 9, "trans": ("whip", "cut", "cut"), "zoom": 0.14,
                  "pan": 0.45, "wobble": True, "look": "clean", "caption": "slam", "bed": "pulse", "mood": "tense",
                  "slots": ("hook", "turn")},
    "slow_dread": {"bpm": (72, 78), "beats": (3, 3), "shots": 6, "trans": ("fade",), "zoom": 0.10, "pan": 0.2,
                   "wobble": False, "look": "clean", "caption": "rise", "bed": "drone", "mood": "soft",
                   "monster_gap": 2.0,
                   "slots": ("hook", "middle", "turn")},
    "found_footage": {"bpm": (84, 92), "beats": (2, 3), "shots": 7, "trans": ("cut",), "zoom": 0.08, "pan": 0.3,
                      "wobble": True, "look": "vhs", "caption": "type", "bed": "drone", "mood": "tense",
                      "slots": ("hook", "middle")},
    "rules_list": {"bpm": (88, 96), "beats": (3, 3), "shots": 7, "trans": ("cut", "whip"), "zoom": 0.10, "pan": 0.3,
                   "wobble": False, "look": "clean", "caption": "rule", "bed": "box", "mood": "soft",
                   "slots": ("rules1", "rules2", "rules3")},
    "monster_reveal": {"bpm": (80, 88), "beats": (3, 3), "shots": 6, "trans": ("fade", "cut"), "zoom": 0.10,
                       "pan": 0.25, "wobble": False, "look": "clean", "caption": "rise", "bed": "drone",
                       "mood": "tense", "slots": ("cold", "middle", "turn")},
    "pov_walk": {"bpm": (84, 92), "beats": (3, 3), "shots": 6, "trans": ("cut",), "zoom": 0.18, "pan": 0.1,
                 "wobble": True, "look": "clean", "caption": "rise", "bed": "pulse", "mood": "tense",
                 "slots": ("pov", "middle", "turn")},
}
# Edit v4: each style's own motion and cut vocabulary (bot/motion.py, bot/grade.py, bot/sfx.py), so two Shorts
# in a row never feel like the same template:
#   trans    the cuts it uses inside the montage (the cold open and the turn are always hard cuts on an impact)
#   hand     handheld drift (fraction of the frame); bob: walking bob (pov)
#   punch    snap-zoom depth on the beat (0 = none); every: a punch every N beats
#   ramp     slow-motion speed over the last `ramp_s` seconds before the reveal (None = no ramp), snapping back to 1x
#            on the impact cut
#   freeze   a freeze frame (hit-stop) on the monster in the reveal shot, with a punch-in and a boom
#   hook     the hook's kinetic type: slam / stretch / type / rise
MOTION: dict[str, dict] = {
    "chase_cut": {"trans": ("whip", "flash", "zoom", "whip", "cut"), "hand": 0.012, "punch": 0.12, "every": 2,
                  "ramp": 0.4, "ramp_s": 0.6, "freeze": True, "hook": "slam"},
    "slow_dread": {"trans": ("dip", "leak", "fade"), "hand": 0.006, "punch": 0.0, "every": 0,
                   "ramp": 0.5, "ramp_s": 0.9, "freeze": False, "hook": "stretch"},
    "found_footage": {"trans": ("glitch", "cut", "glitch"), "hand": 0.022, "punch": 0.07, "every": 3,
                      "ramp": None, "ramp_s": 0.0, "freeze": False, "hook": "type"},
    "rules_list": {"trans": ("whip", "leak", "cut"), "hand": 0.006, "punch": 0.1, "every": 0,
                   "ramp": None, "ramp_s": 0.0, "freeze": True, "hook": "slam"},
    "monster_reveal": {"trans": ("dip", "leak", "cut"), "hand": 0.008, "punch": 0.0, "every": 0,
                       "ramp": 0.3, "ramp_s": 0.9, "freeze": True, "hook": "stretch"},
    "pov_walk": {"trans": ("zoom", "cut", "zoom"), "hand": 0.004, "bob": 0.007, "punch": 0.06, "every": 4,
                 "ramp": None, "ramp_s": 0.0, "freeze": True, "hook": "rise"},
}
FLASHY = ("flash",)        # transitions that count as a flash (photosensitivity: >= MIN_FLASH_GAP apart)
MIN_FLASH_GAP = 1.6        # a little over the validator's 1.5 s
FREEZE_HOLD = 0.3          # seconds the reveal's freeze frame holds
HOOK_SLOT = {"rules_list": None, "monster_reveal": "cold", "pov_walk": "pov"}  # default: "hook"
RULES_TITLE = "rules of the dream"


def style(template: str) -> dict:
    return STYLES[template]


def bpm_for(template: str, rng: random.Random) -> int:
    lo, hi = STYLES[template]["bpm"]
    return rng.randint(lo, hi)


def scary_clips(clips: dict[str, float]) -> list[str]:
    tagged = footage.tags()
    return sorted(c for c in clips if footage.is_scary(c, tagged))


def _owner(clip: str) -> bank.Dream | None:
    return next((d for d in bank.dreams() if clip in d.footage), None)


def vertical_ok(clip: str, tagged: dict[str, dict]) -> bool:
    """Shorts crop to 9:16: a tagged clip must be marked vertical-safe (its subject stays in the middle strip)."""
    t = tagged.get(clip, {}).get("tags")
    return not t or "vertical-safe" in t


def cast(template: str, rng: random.Random, clips: dict[str, float], recent: set[str]) -> tuple[list, list[str]]:
    """(dreams in shot order, clips in shot order) for a Short: the cold-open scare clip first, the montage, the turn
    clip last. Dream ids are listed montage first (the lead dream for the variety guard), the scare dreams after."""
    st = STYLES[template]
    tagged = footage.tags()
    usable = {c for c in clips if vertical_ok(c, tagged) and _owner(c) is not None}

    def has(c, *names):
        return any(n in tagged.get(c, {}).get("tags", []) for n in names)

    scares = sorted(c for c in usable if has(c, "scare"))
    monsters = sorted(c for c in usable if has(c, "monster"))
    if not (scares or monsters):
        raise captions.EditError("no clip tagged monster/scare in the footage bank")
    # cold open: a clip scored by eye as a big, sharp monster at its opening frame (bot/data/footage_tags.json
    # cold_open_ok: no flicker, dark or empty openers); else a scare at its peak; else any monster clip
    approved = sorted(c for c in footage.cold_open_ok() if c in usable and tagged.get(c, {}).get("peak") is not None)
    peaked = [c for c in scares if tagged[c].get("peak") is not None]
    cold = rng.choice(approved or peaked or scares or monsters)
    # the turn: a chase or scare clip from ANOTHER dream when there is one, never the cold open's clip
    turn_pool = sorted(c for c in usable if c != cold and has(c, "chase", "scare"))
    other = [c for c in turn_pool if _owner(c) is not _owner(cold)]
    turn_pool = other or turn_pool or [c for c in monsters if c != cold]
    if not turn_pool:
        turn_pool = sorted((c for c in usable if c != cold), key=lambda c: -_motion(c))[:3]
    if not turn_pool:
        raise captions.EditError("no clip left for the turn")
    turn = rng.choice(turn_pool)
    middle_n = st["shots"] - 2
    # the montage: one clip per dream (variety inside the Short); monster clips welcome, the big scares are kept
    # for the cold open and the turn
    pool = [c for c in sorted(usable) if c not in (cold, turn) and not has(c, "scare", "chase")]
    owners = {c: _owner(c) for c in pool}
    rng.shuffle(pool)
    fresh = [c for c in pool if owners[c].id not in recent]
    stale = [c for c in pool if c not in fresh]
    chosen, seen = [], {_owner(cold).id, _owner(turn).id}
    for c in fresh + stale:
        d = owners[c].id
        if d in seen:
            continue
        chosen.append(c)
        seen.add(d)
        if len(chosen) == middle_n:
            break
    if len(chosen) < middle_n:          # fewer dreams than shots: a second clip of a dream (never the same clip)
        for c in fresh + stale:
            if c not in chosen:
                chosen.append(c)
                if len(chosen) == middle_n:
                    break
    if len(chosen) < middle_n:
        raise captions.EditError("not enough distinct clips for this edit")
    if st.get("monster_gap"):
        # never two empty shots in a row: monster clips and empty ones alternate (the empty ones are capped at the
        # gap in build), so a monster is on screen at least every monster_gap seconds
        mons = [c for c in chosen if has(c, "monster")]
        empty = [c for c in chosen if not has(c, "monster")]
        extra = [c for c in fresh + stale if c not in chosen and has(c, "monster")]
        while len(empty) > len(mons) + 1 and extra:      # swap empties for more monster clips
            empty.pop()
            mons.append(extra.pop(0))
        if len(empty) > len(mons) + 1:
            raise captions.EditError("not enough monster clips to keep the dread on screen")
        # the cold open shows a monster, so the montage starts with an empty shot at most, then alternates
        chosen = [x for pair in zip(empty, mons) for x in pair] + mons[len(empty):] + empty[len(mons):]
    order = [cold] + chosen + [turn]
    dreams = []
    for c in chosen + [cold, turn]:
        o = _owner(c)
        if o not in dreams:
            dreams.append(o)
    return dreams, order


def _motion(clip: str) -> float:
    prof = footage.profile(clip)
    return sum(m for m, _ in prof) / len(prof) if prof else 0.0


def build(p: Pick, rng: random.Random, order: list[str]) -> dict:
    """Shots + caption track + hook + end tag + look for one Short of style p.template."""
    st = STYLES[p.template]
    mv = MOTION[p.template]
    b = p.bpm
    tagged = footage.tags()
    shots = []
    lo, hi = st["beats"]
    n = len(order)
    for i, clip in enumerate(order):
        first, last = i == 0, i == n - 1
        k = 2 if first else (max(3, hi) if last else rng.randint(lo, hi))
        secs = min(MAX_SHOT, beats(k, b))
        if last:
            secs = min(MAX_SHOT, max(secs, END_TAG + 1.0))
        clip_s = p.clip_seconds.get(clip, 9.5)
        peak = tagged.get(clip, {}).get("peak")
        is_monster = "monster" in tagged.get(clip, {}).get("tags", [])
        if st.get("monster_gap") and not (first or last) and not is_monster:
            secs = min(secs, st["monster_gap"])          # an empty corridor never holds past the gap
        if first and peak is not None:
            # the cold open: COLD_LEAD before the peak, so the monster is big and sharp from frame 0
            start = max(0.0, min(peak - COLD_LEAD, clip_s - secs - 0.15))
        elif st.get("monster_gap") and is_monster and peak is not None:
            start = footage.best_start(clip, secs, clip_s, rng=rng, prefer=peak)   # where the monster is
        elif last and peak is not None:
            # the turn: the reveal moment itself, not an empty stretch of the clip
            start = footage.best_start(clip, secs, clip_s, rng=rng, prefer=peak)
        else:
            start = footage.best_start(clip, secs, clip_s, rng=rng)
        z0 = round(rng.uniform(1.0, 1.06), 3)
        push = st["zoom"] * (1.4 if last else 1.0)
        zoom = [z0, round(min(1.6, z0 + push), 3)] if rng.random() < 0.75 or first else [round(min(1.6, z0 + push), 3), z0]
        pan = round(rng.uniform(-st["pan"], st["pan"]), 2)
        fx = []
        if first or last:
            fx = ["impact"]
        elif st["wobble"]:
            fx = ["shake"]
        tr = "cut" if first or last else rng.choice(mv["trans"])
        shot = {"kind": "clip", "seconds": round(secs, 3), "clip": clip, "from": round(start, 2), "speed": 1.0,
                "pan": pan, "zoom": zoom, "caption": None, "fx": fx, "transition": tr, "hand": mv["hand"]}
        if mv.get("bob"):
            shot["bob"] = mv["bob"]
        if last and mv["freeze"] and peak is not None:
            tpk = round(peak - start, 3)            # the monster's peak, shot-local
            if 0.35 <= tpk <= secs - FREEZE_HOLD - 0.4:
                shot["ramp"] = [[tpk, 0.0], [round(tpk + FREEZE_HOLD, 3), 1.0]]
                shot["punches"], shot["punch"] = [tpk], 0.14
        shots.append(shot)
    _beat_motion(shots, mv, b, rng)
    starts, t = [], 0.0
    for s in shots:
        starts.append(t)
        t += s["seconds"]
    total = t

    def line(slot: str, d: bank.Dream | None = None) -> str | None:
        raw = p.lines.get(slot)
        return bank.fill(raw, d or (p.dreams[0] if p.dreams else None), p.monster) if raw else None

    hook_slot = HOOK_SLOT.get(p.template, "hook")
    hook_text = RULES_TITLE if hook_slot is None else line(hook_slot)
    hook_secs = captions.hold_for(hook_text)
    plan: list[tuple[str, float, str]] = []    # (text, earliest start, style)
    cap_style = st["caption"]
    if p.template == "rules_list":
        for j, slot in enumerate(("rules1", "rules2", "rules3"), 1):
            if p.lines.get(slot):
                plan.append((f"rule {j}: {line(slot)}", starts[min(len(starts) - 2, 1 + 2 * (j - 1))], "rule"))
    else:
        for slot in st["slots"]:
            if slot == hook_slot:
                continue
            text = line(slot)
            if not text:
                continue
            # the turn line slams just before the reveal cut and holds through it (the end tag waits for it)
            at = starts[-1] - 0.35 if slot == "turn" else starts[min(2, n - 2)]
            plan.append((text, at, "slam" if slot == "turn" else cap_style))
    # the end tag sits in the upper band (where the hook was), so the turn line may run under it to the last frame
    track = captions.track(plan, hook_end=hook_secs, end=total)
    if p.template == "rules_list":
        _punch_on_captions(shots, starts, track, mv["punch"])
    return {
        "shots": shots,
        "captions": track,
        "hook": {"title": hook_text, "label": "", "seconds": round(hook_secs, 3), "kind": mv["hook"]},
        "end_tag": {"seconds": END_TAG, "text": "liminality: free on roblox"},
        "look": st["look"],
        "grade": p.template,
        "bed": st["bed"],
        "mood": st["mood"],
    }


def _beat_motion(shots: list[dict], mv: dict, bpm: int, rng: random.Random) -> None:
    """The style's beat-level motion, in place: punch-ins on every `every` beats of the montage shots, the slow-motion
    ramp into the reveal (the shot before the turn), and no flash cut closer than MIN_FLASH_GAP to another flash or
    impact (photosensitivity: a flash too close becomes a whip)."""
    beat = 60.0 / bpm
    n = len(shots)
    if mv["punch"] and mv["every"]:
        for s in shots[1:-1]:
            step = beat * mv["every"]
            ts = [round(step * j, 3) for j in range(1, 8) if step * j < s["seconds"] - 0.25]
            if ts:
                s["punches"], s["punch"] = ts, mv["punch"]
    if mv["ramp"] and n >= 3:
        pre = shots[-2]
        at = round(max(0.3, pre["seconds"] - mv["ramp_s"]), 3)
        pre["ramp"] = [[at, mv["ramp"]]]
        pre.pop("punches", None)                # the slow-motion breath has no punch in it
        pre.pop("punch", None)
    # impacts are fixed (cold open, turn): a flash cut too close to one on EITHER side becomes a whip, and two
    # flash cuts too close keep the first
    t, starts = 0.0, []
    for s in shots:
        starts.append(t)
        t += s["seconds"]
    hits = [t0 for t0, s in zip(starts, shots) if "impact" in s["fx"]]
    last_flash = -99.0
    for t0, s in zip(starts, shots):
        if s["transition"] not in FLASHY:
            continue
        if t0 - last_flash < MIN_FLASH_GAP or any(abs(t0 - h) < MIN_FLASH_GAP for h in hits):
            s["transition"] = "whip"
        else:
            last_flash = t0


def _punch_on_captions(shots: list[dict], starts: list[float], track: list[dict], depth: float) -> None:
    """rules_list: the camera punches in as each rule lands (one per rule, on the shot it starts in)."""
    for c in track:
        if not c["text"].startswith("rule "):
            continue
        k = max(i for i, s0 in enumerate(starts) if s0 <= c["start"] + 1e-9)
        if k in (0, len(shots) - 1):
            continue
        tl = round(c["start"] - starts[k], 3)
        if shots[k].get("ramp") or len(shots[k].get("punches", [])) >= 8:
            continue                            # the slow-motion breath stays still; the validator caps punches at 8
        if tl < shots[k]["seconds"] - 0.2:
            shots[k].setdefault("punches", []).append(tl)
            shots[k]["punch"] = depth
