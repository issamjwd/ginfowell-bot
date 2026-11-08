"""Optional: let Claude write the episode's words (captions, title, thumbnail line). OFF unless ANTHROPIC_API_KEY is set.

The planner still picks the template, dreams and monster and builds the shot list; Claude only writes text for the
caption slots of that plan. Its output is untrusted: it must match a JSON schema (structured outputs), then the whole
storyboard is re-validated (lowercase, length, safe glyphs, no gore, "dreams" never "rooms"). Any failure -> the
content-bank storyboard is used unchanged. No secrets and no viewer data ever go into the prompt.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import random

from . import bank, planner, storyboard
from .templates import NEEDS, Pick

log = logging.getLogger(__name__)
MODEL = "claude-opus-5-5"

STYLE = """You write on-screen captions for YouTube Shorts about "liminality", a soft dreamcore horror game on Roblox
by the studio PIXELHELM. A child in hospital falls asleep and wakes in gentle, empty dreams; something in each dream
notices you. Voice: lowercase, short (under 45 characters), gentle, sad, quietly scary. Never gory, never violent
words. Call the levels "dreams", never "rooms". No emoji, no hashtags in captions, plain ASCII punctuation only.
Each caption must make sense alone on screen over the shot it labels. Write like the examples, not like an ad."""


def _schema(slots: tuple[str, ...]) -> dict:
    props = {s: {"type": "string"} for s in slots}
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "thumbnail_text": {"type": "string"},
            "lines": {"type": "object", "properties": props, "required": list(slots), "additionalProperties": False},
        },
        "required": ["title", "thumbnail_text", "lines"],
        "additionalProperties": False,
    }


def _prompt(pick: Pick, slots: tuple[str, ...]) -> str:
    examples = {k: list(v)[:4] for k, v in bank.lines().items()}
    return json.dumps({
        "task": "write the words for one short",
        "template": pick.template,
        "dreams": [{"name": d.name, "detail": d.detail} for d in pick.dreams],
        "monster": bank.MONSTER_WORDS.get(pick.monster or "", None),
        "dream_count": bank.dream_count(),
        "caption_slots_in_order": list(slots),
        "slot_meaning": {"hook": "first line, pulls you in", "middle": "a quiet detail", "middle2": "another detail",
                         "turn": "the scare beat, still gentle", "memory": "a soft memory", "night": "about night",
                         "wave": "about something waving", "outro": "closing line on the title card"},
        "examples_from_our_bank": examples,
        "title_rules": "lowercase, under 70 characters, end with ' #shorts'",
        "thumbnail_text_rules": "lowercase, 2-4 words",
    }, ensure_ascii=True)


def write(pick: Pick, sb: dict, rng: random.Random) -> dict:
    """Return a storyboard with Claude's words, or the bank storyboard `sb` unchanged on any problem."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return sb
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed; using the bank")
        return sb
    slots, _, _ = NEEDS[pick.template]
    try:
        client = anthropic.Anthropic(max_retries=3, timeout=120)
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=STYLE,
            messages=[{"role": "user", "content": _prompt(pick, slots)}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _schema(slots)}},
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        )
        if resp.stop_reason != "end_turn":
            log.warning("claude stopped with %s; using the bank", resp.stop_reason)
            return sb
        text = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001 - optional feature: any failure falls back, logged
        log.warning("claude writer failed (%s: %s); using the bank", type(e).__name__, e)
        return sb
    return merge(pick, sb, data, rng)


def merge(pick: Pick, sb: dict, data: dict, rng: random.Random) -> dict:
    """Rebuild the storyboard with the written lines and validate it; fall back to `sb` if anything is off."""
    try:
        lines = {k: str(v).strip().lower() for k, v in data["lines"].items()}
        if set(lines) != set(NEEDS[pick.template][0]):
            raise ValueError("wrong slots")
        new_pick = copy.copy(pick)
        new_pick.lines = {k: v.replace("{", "(").replace("}", ")") for k, v in lines.items()}
        out = planner.assemble(new_pick, rng, sb["fingerprint"], sb.get("synthetic", False))
        title = str(data["title"]).strip()
        if not title.endswith("#shorts"):
            title = title + " #shorts"
        out["title"] = title[:100]
        out["thumbnail"] = {**out["thumbnail"], "text": str(data["thumbnail_text"]).strip().lower()[:40]}
        out["writer"] = "claude"
        return storyboard.validate(out, footage=set(pick.clip_seconds) if pick.fmt == "footage" else None)
    except (KeyError, ValueError, TypeError, storyboard.StoryboardError) as e:
        log.warning("claude's words failed validation (%s); using the bank", e)
        return sb
