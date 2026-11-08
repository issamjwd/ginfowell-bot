"""On-screen text at reading speed (owner: "talks too fast").

  * a line is cut into chunks of at most CHUNK_WORDS words (at punctuation first), one chunk on screen at a time
  * a chunk stays up for max(MIN_HOLD + its word-by-word reveal, words / WPS): about 2.6 words a second at most,
    and at least 1.2 s after it is fully up
  * lines never overlap the hook or the end tag and never squeeze: a line that does not fit is dropped whole
    (fewer words, never faster ones)

Stdlib only; the planner and the tests use it, the overlay and the burned fallback read its track.
"""
from __future__ import annotations

import re

WPS = 2.6           # words per second, the fastest the text may go
MIN_HOLD = 1.2      # seconds a chunk stays fully up
REVEAL = 0.12       # seconds per word for the word-by-word entrance
GAP = 0.12          # seconds between two chunks
CHUNK_WORDS = 3


class EditError(RuntimeError):
    """An edit cannot be built from this footage / these lines."""


def words(text: str | None) -> list[str]:
    return (text or "").split()


def hold_for(text: str | None) -> float:
    """Seconds a chunk (or the hook) needs on screen."""
    n = len(words(text))
    if not n:
        return 0.0
    return round(max(MIN_HOLD + REVEAL * n, n / WPS), 3)


def chunks(text: str, most: int = CHUNK_WORDS) -> list[str]:
    """Split at sentence / clause punctuation first, then into runs of at most `most` words. A "rule 1:" style label
    stays with its first words."""
    parts = [p.strip() for p in re.split(r"(?<=[.?!,;])\s+", text.strip()) if p.strip()]
    out: list[str] = []
    for part in parts:
        ws = part.split()
        label = []
        if len(ws) > 2 and ws[0] == "rule" and ws[1].endswith(":"):
            label, ws = ws[:2], ws[2:]
        while ws:
            take = ws[:most]
            ws = ws[most:]
            if len(ws) == 1:          # never leave one orphan word on its own
                take, ws = take + ws, []
            out.append(" ".join(label + take))
            label = []
    return out


def track(plan: list[tuple[str, float, str]], hook_end: float, end: float) -> list[dict]:
    """plan = [(line, earliest start s, style)] in order -> [{"text", "start", "end", "style"}] chunks."""
    out: list[dict] = []
    t = hook_end + GAP
    for line, at, style in plan:
        cs = chunks(line)
        start = max(at, t)
        need = sum(hold_for(c) for c in cs) + GAP * (len(cs) - 1)
        if start + need > end + 1e-6:
            continue                   # does not fit at reading speed: drop the line, never rush it
        for c in cs:
            d = hold_for(c)
            out.append({"text": c, "start": round(start, 3), "end": round(start + d, 3), "style": style})
            start += d + GAP
        t = start
    return out


def words_per_second(track_: list[dict]) -> float:
    """The fastest chunk in a track (tests and QA info)."""
    return max((len(words(c["text"])) / (c["end"] - c["start"]) for c in track_), default=0.0)
