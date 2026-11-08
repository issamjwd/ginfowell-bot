"""Planner: picks template x dream(s) x monster x caption lines, never repeating a combination.

Rotation (keeps the channel from looking like a repetitive mass-produced feed; three Shorts a day):
  * never the same template twice in a row, nor twice on the same day; in "auto" mode the format alternates
    footage / animated
  * dreams used in the last 3 Shorts are avoided (relaxed only if nothing else fits)
  * caption lines used in the last 6 Shorts are avoided (same relaxation)
  * the (template, dreams, monster, lines) fingerprint is never reused (state/episodes.jsonl)
  * everything in bot/variety.py: same-day Shorts differ in template, lead dream, hook line and music; hooks, tempos
    and (template, dream) pairs rotate over days
Only Shorts count here: the daily long video (bot/longform.py) keeps its own rotation.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

from . import bank, captions, edits, metadata, storyboard, templates, variety
from .state import fingerprint
from .templates import Pick

RECENT_DREAMS, RECENT_LINES = variety.RECENT_DREAMS, variety.RECENT_LINES


class PlanError(RuntimeError):
    """No fresh episode could be planned."""


def _category(slot: str) -> str:
    return slot.rstrip("0123456789")


def _weighted(rng: random.Random, items: list, weights: list[float]):
    return rng.choices(items, weights=weights, k=1)[0]


def _choose_template(rng, fmt, history, footage_ok, today: set[str] | None = None, force: str | None = None):
    tpls = [t for t in bank.templates() if t.enabled]
    if force:
        # a dry run reviewing one style: that style whatever the variety rules say (the ledger is never written)
        pool = [t for t in tpls if t.id == force and (footage_ok or t.format != "footage")]
        if not pool:
            raise PlanError(f"template {force!r} is not an enabled style that can be made now")
        return pool
    if not footage_ok:
        tpls = [t for t in tpls if t.format != "footage"]
    want = fmt
    if fmt == "auto":
        last_fmt = history[-1].get("format") if history else "animated"
        want = "animated" if last_fmt == "footage" or not footage_ok else "footage"
    pool = [t for t in tpls if t.format == want] or tpls
    last = history[-1].get("template") if history else None
    pool = [t for t in pool if t.id != last] or pool
    # a template already used today is out (variety rule); in auto mode the other format may step in
    today = today or set()
    fresh = [t for t in pool if t.id not in today]
    if not fresh and fmt == "auto":
        fresh = [t for t in tpls if t.id not in today and t.id != last]
    if not fresh:
        raise PlanError("no enabled template left for format " + fmt + " today")
    return fresh


def _dreams_for(tpl: str, rng: random.Random, recent: set[str], footage: dict[str, float]) -> tuple[list, str | None]:
    all_d = list(bank.dreams())
    has_clip = [d for d in all_d if d.footage and d.footage[0] in footage]
    with_monster = [d for d in all_d if d.monster]

    def fresh(pool):
        f = [d for d in pool if d.id not in recent]
        return f or pool

    _, n, rule = templates.NEEDS[tpl]
    if tpl in ("dream_tour",):
        pool = fresh(has_clip)
        if len(pool) < n:
            pool = has_clip
        if len(pool) < n:
            raise PlanError("not enough footage for a tour")
        return rng.sample(pool, n), None
    if tpl in ("one_dream", "did_you_see_it"):
        if not has_clip:
            raise PlanError("no footage")
        return [rng.choice(fresh(has_clip))], None
    if rule == "neighbour":
        hills = [d for d in all_d if d.monster == "Neighbour"]
        if not hills:
            raise PlanError("no dream with the neighbour")
        return [rng.choice(hills)], "Neighbour"
    if rule == "required":
        if not with_monster:
            raise PlanError("no dream has a monster")
        d = rng.choice(fresh(with_monster))
        return [d], d.monster
    if rule == "last_required":
        last = rng.choice(fresh(with_monster))
        others = [d for d in fresh(all_d) if d.id != last.id]
        if len(others) < n - 1:
            others = [d for d in all_d if d.id != last.id]
        return rng.sample(others, n - 1) + [last], last.monster
    d = rng.choice(fresh(all_d))
    return [d], d.monster


# The edit styles keep lines short (reading speed, big text): at most this many words per slot category.
MAX_WORDS = {"hook": 5, "pov": 6, "cold": 4, "turn": 4, "middle": 6, "rules": 4}


def _lines_for(tpl: str, rng: random.Random, recent_lines: set[str], gentle: bool = False) -> dict[str, str]:
    bank_lines = bank.lines()
    slots, _, rule = templates.NEEDS[tpl]
    chosen: dict[str, str] = {}
    for slot in slots:
        c = _category(slot)
        cat = bank_lines["memory" if gentle and c == "turn" else c]
        if rule == "montage":
            # no {detail}/{dream} lines here: the montage is many dreams, and short lines read at a glance
            short = [x for x in cat if len(x.split()) <= MAX_WORDS.get(c, 6) and "{" not in x]
            cat = short or cat
        fresh = [x for x in cat if x not in recent_lines and x not in chosen.values()]
        pool = fresh or [x for x in cat if x not in chosen.values()] or list(cat)
        chosen[slot] = rng.choice(pool)
    return chosen


def plan(history: list[dict], used: set[str], fmt: str = "auto", footage: dict[str, float] | None = None,
         seed: int | None = None, synthetic: bool = False, tries: int = 300,
         now: datetime | None = None, template: str | None = None) -> tuple[Pick, dict]:
    """Return (pick, validated storyboard). history = ledger.history() oldest first; used = fingerprints.

    Two passes: the first keeps every variety rule (bot/variety.py), the second relaxes only the soft ones. A pick
    that breaks a hard rule is never made."""
    footage = footage or {}
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed)
    footage_ok = any(d.footage and d.footage[0] in footage for d in bank.dreams())
    if fmt == "footage" and not footage_ok:
        raise PlanError("footage format asked for but no footage is available")
    shorts = variety.of_kind(history, "short")
    recent_d, recent_l = variety.recent_dreams(shorts), variety.recent_lines(shorts)
    pool = _choose_template(rng, fmt, shorts, footage_ok, variety.todays_templates(shorts, now), force=template)
    for strict in (True, False):
        for _ in range(tries):
            tpl = _weighted(rng, pool, [t.weight for t in pool])
            order = None
            try:
                if tpl.id in edits.STYLES:
                    dreams, order = edits.cast(tpl.id, rng, footage, recent_d)
                    monster = None
                else:
                    dreams, monster = _dreams_for(tpl.id, rng, recent_d, footage)
            except (PlanError, captions.EditError):
                continue
            gentle = len(dreams) == 1 and dreams[0].gentle
            lines = _lines_for(tpl.id, rng, recent_l, gentle)
            fp = fingerprint(tpl.id, [d.id for d in dreams], monster, lines)
            if fp in used:
                continue
            mood = "tense" if monster or tpl.id in ("did_you_see_it", "dream_tour") else "soft"
            bpm = rng.randint(84, 96) if mood == "tense" else rng.randint(72, 84)
            if order is not None:
                bpm = edits.bpm_for(tpl.id, rng)
            seed_ = rng.randrange(1, 2**31)
            cand = variety.Candidate(tpl.id, tuple(d.id for d in dreams), lines, fp, seed_ % 100000, bpm)
            if variety.hard_problems(cand, shorts, used, now):
                continue
            if strict and variety.soft_problems(cand, shorts, now):
                continue
            pick = Pick(tpl.id, tpl.format, dreams, monster, lines, bpm, seed_, dict(footage))
            try:
                sb = assemble(pick, rng, fp, synthetic, order)
            except storyboard.StoryboardError:
                continue
            return pick, sb
    raise PlanError("no fresh Short passes the variety rules; add lines or dreams to bot/data")


def assemble(pick: Pick, rng: random.Random, fp: str, synthetic: bool = False, order: list[str] | None = None) -> dict:
    if order is not None:
        return _assemble_edit(pick, rng, fp, synthetic, order)
    shots = templates.BUILDERS[pick.template](pick, rng)
    if len(pick.dreams) == 1 and pick.dreams[0].gentle:  # nothing hunts here: keep it soft
        for s in shots:
            s["fx"] = [f for f in s.get("fx", []) if f not in ("impact", "shake", "speed_lines", "flash")]
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    meta = metadata.build(pick.template, pick.dreams, pick.fmt, rng, synthetic)
    drop = None
    t = 0.0
    for s in shots:
        if "impact" in s.get("fx", []):
            drop = round(t - 0.05, 3)
            break
        t += s["seconds"]
    sb = {
        "version": 1, "kind": "short", "episode_id": f"ep-{day}-{fp[:8]}", "format": pick.fmt,
        "template": pick.template,
        "dream_ids": [d.id for d in pick.dreams], "monster": pick.monster, "fingerprint": fp,
        "lines": dict(pick.lines),
        "music": {"bpm": pick.bpm, "seed": pick.seed % 100000, "mood": "tense" if pick.monster else "soft",
                  "drop_at": drop},
        "synthetic": synthetic, "shots": shots, **meta,
    }
    return storyboard.validate(sb, footage=set(pick.clip_seconds) if pick.fmt == "footage" else None)


def _assemble_edit(pick: Pick, rng: random.Random, fp: str, synthetic: bool, order: list[str]) -> dict:
    """A Short in one of the footage edit styles (bot/edits.py): shots, caption track, hook, end tag, look."""
    ed = edits.build(pick, rng, order)
    shots = ed["shots"]
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    meta = metadata.build(pick.template, pick.dreams, pick.fmt, rng, synthetic)
    turn = sum(float(s["seconds"]) for s in shots[:-1])
    sb = {
        "version": 1, "kind": "short", "episode_id": f"ep-{day}-{fp[:8]}", "format": "footage",
        "template": pick.template, "dream_ids": [d.id for d in pick.dreams], "monster": None, "fingerprint": fp,
        "lines": dict(pick.lines), "clips": list(order),
        "music": {"bpm": pick.bpm, "seed": pick.seed % 100000, "mood": ed["mood"], "bed": ed["bed"],
                  # a breath of silence right before the reveal, back on the hit
                  "drop_at": round(turn - 0.3, 3), "resume_at": round(turn, 3)},
        "captions": ed["captions"], "hook": ed["hook"], "end_tag": ed["end_tag"], "look": ed["look"],
        "grade": ed["grade"],
        "synthetic": synthetic, "shots": shots, **meta,
    }
    return storyboard.validate(sb, footage=set(pick.clip_seconds))
