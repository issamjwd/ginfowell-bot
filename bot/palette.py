"""One accent colour per video, never one of the last few (owner 2026-11-08: "same colors for each edit").

Ink and paper stay (text must read on any footage); only the accent changes: the underline rule, the marker bar, the
glitch echo, labels. Every accent below reads on the dark ink plate (contrast >= 4.5 against #17131D)."""
from __future__ import annotations

import random

ACCENTS = ("#F2A7C3", "#F2A23E", "#4FD1C5", "#F0605D", "#A78BFA", "#A3E635", "#7DD3FC", "#E8C46A")
RECENT = 3


def pick(history: list[dict], seed: int) -> dict:
    recent = [r.get("accent") for r in history if r.get("accent")][-RECENT:]
    pool = [a for a in ACCENTS if a not in recent] or list(ACCENTS)
    return {"accent": random.Random(seed).choice(pool)}
