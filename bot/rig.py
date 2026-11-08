"""The one timing source (an After Effects "expression rig" in plain code).

Every hit in a Short (the picture's impact frame and shake, the overlay's caption slam, the particle burst, the
hook's word landings, the end card's letters, and every sound effect) is read from ONE cue sheet built here from the
overlay props. Nothing re-derives its own timing, so it all lands on the same frame.

A cue is {"f": absolute frame, "kind": whoosh|impact|tick, "gain": 0..1, ...}:
  whoosh  a text entrance (hook, caption, whip, outro); "dir" says which way it travels (up, left)
  impact  bot/render.py's impact frame; "shake" is the renderer's own shake track from that frame (stage px)
  tick    one end-card letter landing

Frame maths copies render/src/Short.jsx exactly (JavaScript Math.round, the same beat formula), and the tests pin it.
"""
from __future__ import annotations

import math

from . import fx

SHAKE_IMPACT = (34.0, 4.0)  # the same amp/decay bot/render.py and bot/overlay.py use for an impact
BURST_FRAMES = 20           # how long an impact's particle burst lives (and how far its shake track is carried)
GAIN = {"hook": 0.55, "caption": 0.42, "whip": 0.7, "outro": 0.35, "impact": 0.9, "tick": 0.26, "chapter": 0.5,
        "chapter_word": 0.2}
CHAPTER_WORD0, CHAPTER_WORD_GAP = 8, 3  # a chapter title's words land from frame 8, one every 3 frames


def js_round(x: float) -> int:
    """JavaScript Math.round (halves go up), not Python's banker's round."""
    return math.floor(x + 0.5)


def first_frame(seconds: float, fps: int) -> int:
    """The frame bot/render.py starts a shot on (a ceil, not a round)."""
    return math.ceil(seconds * fps - 1e-6)


def beat_frames(beat: float | None, fps: int) -> float:
    return max(6.0, (beat or 0.65) * fps)


def hook_word_frames(title: str, seconds: float, beat: float | None, fps: int) -> list[int]:
    """Sequence-local frame each hook word lands on (Short.jsx Hook)."""
    total = max(8, js_round(seconds * fps))
    words = (title or "").split()
    room = (total - 6) - 4
    gap = max(1, min(4, js_round(beat_frames(beat, fps) / 4), math.floor((room * 0.45) / max(1, len(words)))))
    return [1 + i * gap for i in range(len(words))]


def chapter_word_frames(name: str) -> list[int]:
    """Sequence-local frame each word of a chapter title lands on (Short.jsx ChapterCard)."""
    return [CHAPTER_WORD0 + i * CHAPTER_WORD_GAP for i in range(len((name or "").split()))]


def letter_frames(title: str) -> list[int]:
    """Sequence-local frame each end-card letter lands on (Short.jsx EndCard): one per frame from frame 3."""
    return [3 + i for i in range(len(title))]


def _impact_shake(shot_index: int, s0: float, f: int, fps: int) -> list[list[int]]:
    amp, decay = SHAKE_IMPACT
    track = []
    for fi in range(f, f + BURST_FRAMES):
        dx, dy = fx.shake_offset(fi / fps - s0, shot_index, amp, decay)
        track.append([-dx, -dy])
    while track and track[-1] == [0, 0]:
        track.pop()
    return track


def cue_sheet(props: dict, impacts: list[tuple[int, float]]) -> list[dict]:
    """All cues for the Remotion overlay path, sorted by frame. impacts = [(shot index, shot start s)].

    Also writes the shared frames back into props (hook.wordFrames, endCard.letterFrames, captions[i].hitAt) so the
    overlay reads them instead of computing its own."""
    fps, n = props["fps"], props["durationInFrames"]
    beat = props.get("beat")
    cues: list[dict] = []

    hook = props.get("hook")
    if hook:
        hook["wordFrames"] = hook_word_frames(hook["title"], hook["until"], beat, fps)
        first = hook["wordFrames"][0] if hook["wordFrames"] else 1
        cues.append({"f": first, "kind": "whoosh", "gain": GAIN["hook"], "dir": "up"})

    for k, s0 in impacts:
        f = first_frame(s0, fps)
        cues.append({"f": f, "kind": "impact", "gain": GAIN["impact"], "shake": _impact_shake(k, s0, f, fps)})

    for ch in props.get("chapters", []):
        f0 = js_round(ch["start"] * fps)
        ch["wordFrames"] = chapter_word_frames(ch["name"])
        cues.append({"f": f0 + 2, "kind": "whoosh", "gain": GAIN["chapter"], "dir": "up"})
        for i, wf in enumerate(ch["wordFrames"]):
            cues.append({"f": f0 + wf, "kind": "tick", "gain": GAIN["chapter_word"], "i": i})

    for c in props.get("captions", []):
        f0 = js_round(c["start"] * fps)
        if "impact" in c.get("hits", []):
            # frames from the caption's own start to the impact: always 0, because an impact caption only keeps its
            # "impact" hit when it starts on its cut (bot/overlay.py on_cut), which is the impact cue's frame
            c["hitAt"] = c.get("hitAt", 0)
            continue
        if c.get("enter") == "whip":
            cues.append({"f": f0, "kind": "whoosh", "gain": GAIN["whip"], "dir": "left"})
        else:
            lead = 4 if c.get("enter") == "fade" else 0
            cues.append({"f": f0 + lead, "kind": "whoosh", "gain": GAIN["caption"], "dir": "up"})

    end_card = props["endCard"]
    end_from = n - js_round(end_card["seconds"] * fps)
    outro = props.get("outro")
    if outro and js_round(outro["start"] * fps) < end_from:
        cues.append({"f": js_round(outro["start"] * fps) + 6, "kind": "whoosh", "gain": GAIN["outro"], "dir": "up"})
    end_card["letterFrames"] = letter_frames(end_card["title"])
    for i, lf in enumerate(end_card["letterFrames"]):
        cues.append({"f": end_from + lf, "kind": "tick", "gain": GAIN["tick"], "i": i})

    # whooshes and impacts past the end card's first frame have nothing on screen (the end card covers them)
    cues = [c for c in cues if 0 <= c["f"] < n and (c["kind"] == "tick" or c["f"] < end_from)]
    cues.sort(key=lambda c: (c["f"], c["kind"]))
    return cues


EDIT_GAIN = {"whip": 0.7, "zoom": 0.55, "flash": 0.75, "leak": 0.55, "dip": 0.8, "glitch": 0.5, "punch": 0.5,
             "riser": 0.6, "boom": 0.95}


def edit_cues(sb: dict, fps: int) -> list[dict]:
    """Edit v4 sound cues read from the storyboard's own cut and camera plan (bot/edits.py, bot/motion.py), so every
    one lands on the frame the picture does it:
      whip   -> a long whoosh cresting on the cut frame, travelling right to left
      zoom   -> a rising whoosh into the cut + a light hit on it
      flash  -> a hit on the flash frame
      leak   -> a reversed swell ending on the cut
      dip    -> a sub drop as the next shot comes up out of black
      glitch -> a digital glitch burst on the cut
      punches (snap zooms) -> a hit on each punch frame (not on an impact frame: that has its own thump)
      ramp   -> a riser over each slow-motion stretch, ending where it snaps back (or on the next cut);
                a freeze frame (speed 0) gets a boom
    Only storyboards with a "grade" (edit v4) have these; older ones keep their exact sound."""
    if "grade" not in sb:
        return []
    shots = sb["shots"]
    starts, t = [], 0.0
    for s in shots:
        starts.append(t)
        t += float(s["seconds"])
    n = max(1, int(round(t * fps)))
    impacts = {first_frame(s0, fps) for s0, s in zip(starts, shots) if "impact" in (s.get("fx") or [])}
    cues: list[dict] = []
    for k, (s0, s) in enumerate(zip(starts, shots)):
        f0 = first_frame(s0, fps)
        tr = s.get("transition", "cut")
        if k > 0:
            if tr == "whip":
                cues.append({"f": f0, "kind": "whoosh", "gain": EDIT_GAIN["whip"], "dir": "left"})
            elif tr == "zoom":
                cues.append({"f": f0, "kind": "whoosh", "gain": EDIT_GAIN["zoom"], "dir": "up"})
                cues.append({"f": f0, "kind": "hit", "gain": EDIT_GAIN["zoom"] * 0.6})
            elif tr == "flash":
                cues.append({"f": f0, "kind": "hit", "gain": EDIT_GAIN["flash"]})
            elif tr == "leak":
                cues.append({"f": f0, "kind": "swell", "gain": EDIT_GAIN["leak"], "len": 0.8})
            elif tr == "dip":
                cues.append({"f": f0, "kind": "subdrop", "gain": EDIT_GAIN["dip"]})
            elif tr == "glitch":
                cues.append({"f": f0, "kind": "glitch", "gain": EDIT_GAIN["glitch"]})
        for tp in s.get("punches") or []:
            fp = f0 + js_round(float(tp) * fps)
            if fp not in impacts and not (tr in ("flash", "zoom") and fp == f0):
                cues.append({"f": fp, "kind": "hit", "gain": EDIT_GAIN["punch"]})
        ramp = sorted((float(a), float(b)) for a, b in (s.get("ramp") or []))
        for j, (ta, sp) in enumerate(ramp):
            fa = f0 + js_round(ta * fps)
            if sp == 0:
                cues.append({"f": fa, "kind": "boom", "gain": EDIT_GAIN["boom"]})
            elif sp < 1:
                end = ramp[j + 1][0] if j + 1 < len(ramp) else float(s["seconds"])
                fe = f0 + js_round(end * fps) if j + 1 < len(ramp) else first_frame(s0 + float(s["seconds"]), fps)
                cues.append({"f": fe, "kind": "riser", "gain": EDIT_GAIN["riser"],
                             "len": round(min(2.0, max(0.3, end - ta + 0.25)), 3)})
    return sorted((c for c in cues if 0 <= c["f"] < n), key=lambda c: (c["f"], c["kind"]))


def burned_cues(sb: dict, fps: int) -> list[dict]:
    """Cues for the burned-caption fallback (no hook title, no particles, no lettered end card): a whoosh as each
    Pillow caption settles (its rise ends ~0.3 s in) and a thump on each impact frame."""
    cues, t = [], 0.0
    for shot in sb["shots"]:
        f = first_frame(t, fps)
        fxs = shot.get("fx") or []
        if "impact" in fxs:
            cues.append({"f": f, "kind": "impact", "gain": GAIN["impact"]})
        elif shot["kind"] in ("clip", "scene") and shot.get("caption"):
            cues.append({"f": f + js_round(0.25 * fps), "kind": "whoosh", "gain": GAIN["caption"], "dir": "up"})
        t += float(shot["seconds"])
    n = max(1, int(round(t * fps)))
    cues += edit_cues(sb, fps)
    return sorted((c for c in cues if 0 <= c["f"] < n), key=lambda c: (c["f"], c["kind"]))
