"""Content bank: dreams, caption lines and templates, loaded from bot/data (owner-editable JSON)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import config

MONSTER_WORDS = {"Mannequin": "the mannequin", "Bear": "the bear", "Neighbour": "the neighbour"}


@dataclass(frozen=True)
class Dream:
    id: str
    name: str
    still: str
    footage: tuple[str, ...]
    monster: str | None
    detail: str
    light: str
    gentle: bool = False


@dataclass(frozen=True)
class Template:
    id: str
    format: str
    weight: float
    enabled: bool


def _read(name: str, data_dir: Path | None = None) -> dict:
    p = (data_dir or config.DATA) / name
    with p.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=4)
def dreams(data_dir: Path | None = None) -> tuple[Dream, ...]:
    raw = _read("dreams.json", data_dir)["dreams"]
    out = tuple(Dream(d["id"], d["name"], d["still"], tuple(d.get("footage", [])), d.get("monster"),
                      d.get("detail", ""), d.get("light", "warm"), bool(d.get("gentle", False))) for d in raw)
    ids = [d.id for d in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate dream ids in dreams.json")
    return out


def dream(dream_id: str) -> Dream:
    for d in dreams():
        if d.id == dream_id:
            return d
    raise KeyError(f"unknown dream {dream_id!r}")


def dream_count() -> int:
    return len(dreams())


@lru_cache(maxsize=4)
def lines(data_dir: Path | None = None) -> dict[str, tuple[str, ...]]:
    raw = _read("lines.json", data_dir)
    return {k: tuple(v) for k, v in raw.items() if not k.startswith("_")}


@lru_cache(maxsize=4)
def templates(data_dir: Path | None = None) -> tuple[Template, ...]:
    raw = _read("templates.json", data_dir)["templates"]
    return tuple(Template(t["id"], t["format"], float(t.get("weight", 1)), bool(t.get("enabled", True))) for t in raw)


def fill(line: str, d: Dream | None = None, monster: str | None = None) -> str:
    """Fill placeholders. The dream count always comes from data (owner rule: no stale counts in copy)."""
    return line.format(
        dream=d.name if d else "a dream",
        detail=(d.detail if d else "it's quiet here"),
        count=dream_count(),
        monster=MONSTER_WORDS.get(monster or "", "something"),
    )
