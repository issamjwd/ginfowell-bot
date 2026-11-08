"""Episode templates: each turns a Pick (dreams, monster, lines, bpm) into a storyboard dict.

Shot lengths are whole beats of the episode's tempo, so cuts land on the music ("beat cuts"). Animated templates stage
the rigged cast over a drawn version of the dream; footage templates cut the real gameplay clips.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import bank


@dataclass
class Pick:
    template: str
    fmt: str
    dreams: list[bank.Dream]
    monster: str | None
    lines: dict[str, str]          # slot -> raw line (unfilled)
    bpm: int
    seed: int
    clip_seconds: dict[str, float] = field(default_factory=dict)


def beats(n: float, bpm: int) -> float:
    return round(n * 60.0 / bpm, 3)


def cap(p: Pick, slot: str, d: bank.Dream | None = None) -> str | None:
    raw = p.lines.get(slot)
    return bank.fill(raw, d or p.dreams[0], p.monster) if raw else None


def _clip_from(p: Pick, rng: random.Random, clip: str, seconds: float, speed: float = 1.0) -> float:
    dur = p.clip_seconds.get(clip, 9.5)
    hi = max(1.6, dur - seconds * speed - 0.25)
    return round(rng.uniform(1.6, hi), 2) if hi > 1.6 else 1.6


def _end(p: Pick, tagline_slot: str = "outro") -> list[dict]:
    return [
        {"kind": "card", "seconds": beats(4, p.bpm), "big": "liminality", "caption": cap(p, tagline_slot),
         "fx": [], "transition": "fade"},
        {"kind": "card", "seconds": beats(3, p.bpm), "end": True, "caption": None, "fx": [], "transition": "fade"},
    ]


# ------------------------------------------------------------------------------------------------ footage
def dream_tour(p: Pick, rng: random.Random) -> list[dict]:
    shots = []
    order = ["hook", "middle", None, "middle2", None]
    for i, d in enumerate(p.dreams):
        clip = d.footage[0]
        secs = beats(rng.choice([3, 4]) if i else 4, p.bpm)
        slot = order[i] if i < len(order) else None
        shots.append({"kind": "clip", "seconds": secs, "clip": clip, "from": _clip_from(p, rng, clip, secs),
                      "speed": 1.0, "pan": round(rng.uniform(-0.4, 0.4), 2), "zoom": [1.0, 1.08],
                      "caption": cap(p, slot, d) if slot else None,
                      "fx": ["speed_lines"] if i == 2 else [], "transition": "whip" if i in (1, 3) else "cut"})
    turn_clip = "mannequin2" if "mannequin2" in p.clip_seconds else p.dreams[-1].footage[0]
    shots.append({"kind": "clip", "seconds": beats(5, p.bpm), "clip": turn_clip, "from": 1.4, "speed": 1.0,
                  "pan": 0.0, "zoom": [1.0, 1.25], "caption": cap(p, "turn"),
                  "fx": ["impact", "shake", "zoom_punch"], "transition": "cut"})
    return shots + _end(p)


def one_dream(p: Pick, rng: random.Random) -> list[dict]:
    d = p.dreams[0]
    clip = d.footage[0]
    shots = []
    for i, (slot, zoom, pan) in enumerate((("hook", [1.0, 1.06], -0.5), ("middle", [1.1, 1.2], 0.5),
                                           (None, [1.0, 1.15], 0.0))):
        secs = beats(4 if i < 2 else 3, p.bpm)
        shots.append({"kind": "clip", "seconds": secs, "clip": clip, "from": _clip_from(p, rng, clip, secs),
                      "speed": 1.0 if i < 2 else 0.75, "pan": pan, "zoom": zoom, "caption": cap(p, slot, d) if slot else None,
                      "fx": [], "transition": "fade" if i else "cut"})
    last = d.footage[1] if len(d.footage) > 1 else clip
    secs = beats(4, p.bpm)
    shots.append({"kind": "clip", "seconds": secs, "clip": last, "from": _clip_from(p, rng, last, secs, 0.6),
                  "speed": 0.6, "pan": 0.0, "zoom": [1.05, 1.3], "caption": cap(p, "turn", d),
                  "fx": ["freeze", "drawn", "impact"], "transition": "cut"})
    return shots + _end(p)


def did_you_see_it(p: Pick, rng: random.Random) -> list[dict]:
    d = p.dreams[0]
    clip = d.footage[0]
    secs = beats(6, p.bpm)
    start = _clip_from(p, rng, clip, secs + 2)
    return [
        {"kind": "clip", "seconds": secs, "clip": clip, "from": start, "speed": 1.0, "pan": 0.0, "zoom": [1.0, 1.05],
         "caption": cap(p, "hook", d), "fx": [], "transition": "cut"},
        {"kind": "clip", "seconds": beats(3, p.bpm), "clip": clip, "from": round(start + secs, 2), "speed": 1.0,
         "pan": 0.0, "zoom": [1.3, 1.45], "caption": "did you see it?", "fx": ["freeze", "drawn", "zoom_punch", "impact"],
         "transition": "cut"},
        {"kind": "clip", "seconds": beats(5, p.bpm), "clip": clip, "from": round(max(1.6, start + secs - 1.5), 2),
         "speed": 0.5, "pan": 0.3, "zoom": [1.15, 1.3], "caption": cap(p, "middle", d), "fx": [], "transition": "fade"},
        {"kind": "clip", "seconds": beats(3, p.bpm), "clip": clip, "from": round(start + secs, 2), "speed": 1.0,
         "pan": 0.0, "zoom": [1.3, 1.6], "caption": cap(p, "turn", d), "fx": ["shake", "speed_lines"],
         "transition": "cut"},
    ] + _end(p)


# ------------------------------------------------------------------------------------------------ animated
def _scene(d: bank.Dream, secs: float, actors: list[dict], caption: str | None, fx: list[str] | None = None,
           zoom=(1.0, 1.08), pan=(0.0, 0.0), night: bool = False, orb: bool = False, transition: str = "cut") -> dict:
    return {"kind": "scene", "seconds": secs, "dream": d.id, "night": night, "orb": orb,
            "camera": {"zoom": list(zoom), "pan": list(pan)}, "actors": actors, "caption": caption,
            "fx": fx or [], "transition": transition}


def actor(ch: str, action: str, x=(0.5, 0.5), height=0.42, flip=False, start=0.0, depth=0.0, speed=1.0,
          ground: float | None = None) -> dict:
    a = {"character": ch, "action": action, "x": list(x), "height": height, "flip": flip, "start": start,
         "depth": depth, "speed": speed}
    if ground is not None:
        a["ground"] = ground
    return a


def closeup(ch: str, action: str = "reach") -> dict:
    """Big in frame, feet far below the bottom edge: the face and hands fill the shot (impact close-ups)."""
    return actor(ch, action, (0.5, 0.5), 1.35, ground=1.5)


def dont_stare(p: Pick, rng: random.Random) -> list[dict]:
    d, m = p.dreams[0], p.monster
    b = p.bpm
    return [
        _scene(d, beats(6, b), [actor("Dreamer", "walk", (0.12, 0.46), 0.40)], cap(p, "hook"), pan=(-0.25, 0.0)),
        _scene(d, beats(4, b), [actor("Dreamer", "idle", (0.46, 0.46), 0.40)], cap(p, "middle"), zoom=(1.05, 1.12)),
        _scene(d, beats(5, b), [actor(m, "idle", (0.78, 0.78), 0.30, depth=0.7),
                                actor("Dreamer", "idle", (0.42, 0.42), 0.40)], None, zoom=(1.0, 1.04)),
        _scene(d, beats(3, b), [actor(m, "idle", (0.75, 0.75), 0.36, depth=0.5),
                                actor("Dreamer", "peek", (0.40, 0.40), 0.42)], cap(p, "turn"), zoom=(1.08, 1.16),
               transition="cut"),
        _scene(d, beats(3, b), [closeup(m)], None,
               fx=["impact", "shake", "speed_lines"], zoom=(1.0, 1.12)),
    ] + _end(p)


def memory_light(p: Pick, rng: random.Random) -> list[dict]:
    d, m, b = p.dreams[0], p.monster, p.bpm
    shots = [
        _scene(d, beats(6, b), [actor("Dreamer", "walk", (0.10, 0.38), 0.40)], cap(p, "hook"), orb=True,
               pan=(-0.2, 0.05)),
        _scene(d, beats(6, b), [actor("Dreamer", "reach", (0.40, 0.40), 0.42, flip=True)], cap(p, "memory"), orb=True,
               zoom=(1.05, 1.18)),
    ]
    if m:
        shots.append(_scene(d, beats(4, b), [actor("Dreamer", "idle", (0.40, 0.40), 0.42),
                                             actor(m, "peek", (0.93, 0.90), 0.34, depth=0.6)],
                            cap(p, "turn"), fx=["zoom_punch"], zoom=(1.0, 1.1), orb=True))
    else:
        shots.append(_scene(d, beats(4, b), [actor("Dreamer", "peek", (0.40, 0.40), 0.42)], cap(p, "turn"),
                            fx=["flash"], night=True, zoom=(1.1, 1.2)))
    return shots + _end(p)


def it_waves_back(p: Pick, rng: random.Random) -> list[dict]:
    d, b = p.dreams[0], p.bpm
    return [
        _scene(d, beats(6, b), [actor("Neighbour", "wave", (0.72, 0.72), 0.26, depth=0.8),
                                actor("Dreamer", "idle", (0.30, 0.30), 0.40, flip=True)], cap(p, "night"), night=True,
               pan=(0.1, -0.1)),
        _scene(d, beats(4, b), [actor("Dreamer", "wave", (0.40, 0.40), 0.44, flip=True)], cap(p, "wave"), night=True,
               zoom=(1.05, 1.12)),
        _scene(d, beats(4, b), [actor("Neighbour", "wave", (0.62, 0.62), 0.42, depth=0.4, speed=2.0),
                                actor("Dreamer", "idle", (0.25, 0.25), 0.40, flip=True)], cap(p, "middle"), night=True,
               zoom=(1.0, 1.1), fx=["shake"]),
        _scene(d, beats(3, b), [closeup("Neighbour")], None, night=True,
               fx=["impact", "shake", "speed_lines"], zoom=(1.0, 1.12)),
    ] + _end(p)


def fell_asleep(p: Pick, rng: random.Random) -> list[dict]:
    b = p.bpm
    shots = []
    *swap, last = p.dreams
    for i, d in enumerate(swap):
        shots.append(_scene(d, beats(4 if i == 0 else 2, b), [actor("Dreamer", "sit", (0.5, 0.5), 0.38)],
                            cap(p, "hook") if i == 0 else (cap(p, "middle", d) if i == 2 else None),
                            zoom=(1.0 + 0.03 * i, 1.06 + 0.03 * i), transition="whip" if i else "cut"))
    shots.append(_scene(last, beats(4, b), [actor("Dreamer", "sit", (0.42, 0.42), 0.38),
                                            actor(p.monster, "idle", (0.80, 0.80), 0.30, depth=0.7)],
                        cap(p, "turn", last), zoom=(1.0, 1.08), transition="whip"))
    shots.append(_scene(last, beats(3, b), [closeup(p.monster)], None,
                        fx=["impact", "shake", "speed_lines"], zoom=(1.0, 1.12)))
    return shots + _end(p)


BUILDERS = {
    "dream_tour": dream_tour, "one_dream": one_dream, "did_you_see_it": did_you_see_it,
    "dont_stare": dont_stare, "memory_light": memory_light, "it_waves_back": it_waves_back,
    "fell_asleep": fell_asleep,
}

# what each template needs from the planner: (slots, how many dreams, monster rule)
NEEDS = {
    # the footage edit styles (bot/edits.py): "montage" = clips chosen by bot/edits.cast
    "chase_cut": (("hook", "turn"), 0, "montage"),
    "slow_dread": (("hook", "middle", "turn"), 0, "montage"),
    "found_footage": (("hook", "middle"), 0, "montage"),
    "rules_list": (("rules1", "rules2", "rules3"), 0, "montage"),
    "monster_reveal": (("cold", "middle", "turn"), 0, "montage"),
    "pov_walk": (("pov", "middle", "turn"), 0, "montage"),
    "dream_tour": (("hook", "middle", "middle2", "turn", "outro"), 4, "none"),
    "one_dream": (("hook", "middle", "turn", "outro"), 1, "none"),
    "did_you_see_it": (("hook", "middle", "turn", "outro"), 1, "none"),
    "dont_stare": (("hook", "middle", "turn", "outro"), 1, "required"),
    "memory_light": (("hook", "memory", "turn", "outro"), 1, "optional"),
    "it_waves_back": (("night", "wave", "middle", "outro"), 1, "neighbour"),
    "fell_asleep": (("hook", "middle", "turn", "outro"), 4, "last_required"),
}
