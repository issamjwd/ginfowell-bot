"""Titles, descriptions and tags. Lowercase and gentle like the game; the dream count comes from data."""
from __future__ import annotations

import random

from . import bank

STORE_URL = "https://www.roblox.com/games/120735295431220/Liminality"
BASE_TAGS = ["liminality", "roblox", "liminal spaces", "dreamcore", "weirdcore", "roblox horror", "backrooms",
             "animation", "pixelhelm", "shorts"]

TITLES = {
    "dream_tour": ["you've been here before #shorts", "{count} dreams. one way home. #shorts",
                   "pov: you fell asleep in liminality #shorts"],
    "one_dream": ["{dream} #shorts", "have you been to {dream}? #shorts", "it's quiet in {dream} #shorts"],
    "did_you_see_it": ["did you see it? ({dream}) #shorts", "look closer in {dream} #shorts"],
    "dont_stare": ["don't stare too long ({dream}) #shorts", "it was behind you the whole time #shorts"],
    "memory_light": ["a memory in {dream} #shorts", "find what you forgot #shorts"],
    "it_waves_back": ["don't wave back #shorts", "it waved first #shorts"],
    "fell_asleep": ["pov: you fell asleep #shorts", "where do you go when you fall asleep? #shorts"],
    "chase_cut": ["don't stop running in liminality #shorts", "it's right behind you #shorts",
                  "it found you in the dream #shorts"],
    "slow_dread": ["something is wrong in this dream #shorts", "stay very still #shorts",
                   "it's too quiet in liminality #shorts"],
    "found_footage": ["found tape from liminality #shorts", "the last recording from the dream #shorts",
                      "nobody came back from this tape #shorts"],
    "rules_list": ["the rules of the dream #shorts", "3 rules for surviving liminality #shorts",
                   "never break these rules #shorts"],
    "monster_reveal": ["wait for it... #shorts", "it was there the whole time #shorts",
                       "look closer #shorts"],
    "pov_walk": ["pov: you can't wake up #shorts", "pov: you fell asleep in liminality #shorts",
                 "pov: the dream keeps going #shorts"],
}
THUMB_TEXT = {
    "dream_tour": "you've been here", "one_dream": "it's quiet here", "did_you_see_it": "did you see it?",
    "dont_stare": "don't stare.", "memory_light": "remember.", "it_waves_back": "don't wave back.",
    "fell_asleep": "fall asleep.",
    "chase_cut": "don't stop.", "slow_dread": "stay still.", "found_footage": "found tape.",
    "rules_list": "the rules.", "monster_reveal": "look closer.", "pov_walk": "can't wake up.",
}


def build(template: str, dreams: list[bank.Dream], fmt: str, rng: random.Random, synthetic: bool) -> dict:
    d = dreams[0]
    title = rng.choice(TITLES[template]).format(dream=d.name, count=bank.dream_count())
    made = ("animated with the game's own characters" if fmt == "animated" else "all footage is in-game")
    lines = [
        f"{d.name}. {d.detail}." if len(dreams) == 1 else
        f"{bank.dream_count()} soft, empty dreams. find what you forgot.",
        "",
        f"liminality is a soft dreamcore horror game on roblox. fall asleep, wake up in {bank.dream_count()} dreams, "
        "find your memories, and try to wake up. something in each dream notices you.",
        "",
        f"play free on roblox: {STORE_URL}",
        "(on shorts the link may not be clickable: it's also on the channel page.)",
        "",
        f"a PIXELHELM short. {made}. music made for this video, no samples.",
    ]
    if synthetic:
        lines.append("some shots in this video were made with AI video tools.")
    lines += ["", "#liminality #roblox #dreamcore #liminalspaces #shorts"]
    tags = BASE_TAGS + [x.name for x in dreams][:3]
    return {"title": title[:100], "description": "\n".join(lines), "tags": tags,
            "thumbnail": {"dream": d.id, "text": THUMB_TEXT[template]}}


# ------------------------------------------------------------------------------------------------ long video
LONG_TITLES = ["{n} dreams in liminality, one after another", "a quiet tour of liminality ({n} dreams)",
               "falling asleep in liminality: {n} dreams", "where liminality takes you when you fall asleep",
               "{n} liminal dreams you can visit on roblox", "a slow walk through {n} dreams in liminality"]
LONG_THUMBS = ["{n} dreams.", "fall asleep.", "take the tour.", "one way home.", "you've been here."]
LONG_TAGS = ["liminality", "roblox", "liminal spaces", "dreamcore", "weirdcore", "roblox horror", "backrooms",
             "liminal spaces tour", "pixelhelm", "roblox game"]


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def build_long(dreams: list[bank.Dream], marks: list[tuple[float, str]], lines: dict, rng: random.Random) -> dict:
    """Title, description (with YouTube chapter timestamps: 0:00 first, every chapter 10 s or longer), tags,
    thumbnail. Not a Short: no #shorts anywhere. Counts come from the data (owner rule: no stale counts)."""
    n = len(dreams)
    titles = list(LONG_TITLES) + (["every dream in liminality"] if n == bank.dream_count() else [])
    title = rng.choice(titles).format(n=n)
    stamps = [f"{clock(t)} {name}" for t, name in marks]
    text = [
        f"{n} of the {bank.dream_count()} dreams in liminality, one chapter each. "
        "turn the sound up a little and take it slowly.",
        "",
        *stamps,
        "",
        f"liminality is a soft dreamcore horror game on roblox. fall asleep, wake up in {bank.dream_count()} dreams, "
        "find your memories, and try to wake up. something in each dream notices you.",
        "",
        f"play free on roblox: {STORE_URL}",
        "",
        "a PIXELHELM video. all footage is in-game; the drawn scenes use the game's own characters. "
        "music made for this video, no samples.",
        "",
        "#liminality #roblox #dreamcore #liminalspaces",
    ]
    tags = LONG_TAGS + [d.name for d in dreams][:4]
    while sum(len(t) for t in tags) > 440:
        tags.pop()
    return {"title": title[:100], "description": "\n".join(text), "tags": tags,
            "thumbnail": {"dream": dreams[0].id, "text": rng.choice(LONG_THUMBS).format(n=n)}}
