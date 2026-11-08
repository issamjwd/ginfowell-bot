"""Variety guard: three Shorts a day must not look like a mass-produced, repetitive feed.

YouTube's "reused / repetitious content" checks look for near-identical uploads. Every candidate episode is checked
against the ledger (state/episodes.jsonl) before it is rendered:

HARD (never relaxed; the planner tries another pick, and gives up rather than break one)
  * the fingerprint (template + dreams + monster + lines) was never used, and not in the last WINDOW_DAYS for sure
  * a Short made the same UTC day has a different template, a different lead dream, a different hook line and a
    different music seed
  * the music seed was not used in the last WINDOW_DAYS
SOFT (preferred; relaxed only when no pick passes them)
  * the template is not one of the last RECENT_TEMPLATES Shorts' templates
  * the same (template, lead dream) pair was not used in the last WINDOW_DAYS
  * the hook line was not used in the last RECENT_HOOKS Shorts
  * no dream from the last RECENT_DREAMS Shorts, no caption line from the last RECENT_LINES Shorts
  * the tempo differs from every Short made the same day

The long video has its own rules (bot/longform.py) and never counts against the Shorts, nor they against it.
Stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

WINDOW_DAYS = 14
RECENT_TEMPLATES = 2
RECENT_HOOKS = 9      # three days of Shorts
RECENT_DREAMS = 3
RECENT_LINES = 6


@dataclass(frozen=True)
class Candidate:
    template: str
    dream_ids: tuple[str, ...]
    lines: dict
    fingerprint: str
    music_seed: int
    bpm: int

    @property
    def lead(self) -> str:
        return self.dream_ids[0] if self.dream_ids else ""

    @property
    def hook(self) -> str | None:
        return first_line(self.lines)


def first_line(lines: dict | None) -> str | None:
    """The line the Short opens on (its "hook" slot; "night", "cold", "pov" or the first rule for other styles)."""
    lines = lines or {}
    for slot in ("hook", "night", "cold", "pov", "rules1"):
        if lines.get(slot):
            return lines[slot]
    return None


def kind_of(row: dict) -> str:
    return row.get("kind") or "short"  # rows written before the long video existed are all Shorts


def _when(row: dict) -> datetime | None:
    ts = row.get("ts")
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def of_kind(history: list[dict], kind: str) -> list[dict]:
    return [r for r in history if kind_of(r) == kind]


def within(history: list[dict], now: datetime, days: float) -> list[dict]:
    """Rows from the last `days` days. A row without a time counts as recent (the safe side)."""
    cut = now - timedelta(days=days)
    return [r for r in history if (_when(r) or now) >= cut]


def same_day(history: list[dict], now: datetime) -> list[dict]:
    day = now.astimezone(timezone.utc).date()
    return [r for r in history if (w := _when(r)) is not None and w.astimezone(timezone.utc).date() == day]


def hard_problems(c: Candidate, shorts: list[dict], used: set[str], now: datetime) -> list[str]:
    errs = []
    window = within(shorts, now, WINDOW_DAYS)
    if c.fingerprint in used or any(r.get("fingerprint") == c.fingerprint for r in window):
        errs.append("fingerprint already used")
    for r in same_day(shorts, now):
        if r.get("template") == c.template:
            errs.append("template already used today")
        if (r.get("dream_ids") or [None])[0] == c.lead:
            errs.append("lead dream already used today")
        if c.hook and first_line(r.get("lines")) == c.hook:
            errs.append("hook line already used today")
    if any(r.get("music_seed") == c.music_seed for r in window):
        errs.append("music seed used in the last 14 days")
    return errs


def soft_problems(c: Candidate, shorts: list[dict], now: datetime) -> list[str]:
    errs = []
    if c.template in [r.get("template") for r in shorts[-RECENT_TEMPLATES:]]:
        errs.append("template used in the last few Shorts")
    if any(r.get("template") == c.template and (r.get("dream_ids") or [None])[0] == c.lead
           for r in within(shorts, now, WINDOW_DAYS)):
        errs.append("same template and lead dream in the last 14 days")
    if c.hook and c.hook in [first_line(r.get("lines")) for r in shorts[-RECENT_HOOKS:]]:
        errs.append("hook line used in the last three days")
    if any(r.get("bpm") == c.bpm for r in same_day(shorts, now)):
        errs.append("same tempo as another Short today")
    return errs


def recent_dreams(shorts: list[dict]) -> set[str]:
    return {d for r in shorts[-RECENT_DREAMS:] for d in r.get("dream_ids") or []}


def recent_lines(shorts: list[dict]) -> set[str]:
    return {x for r in shorts[-RECENT_LINES:] for x in (r.get("lines") or {}).values()}


def todays_templates(shorts: list[dict], now: datetime) -> set[str]:
    return {r.get("template") for r in same_day(shorts, now)}
