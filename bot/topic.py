"""Which kind of Short this run makes: a liminality Short or a "world" Short (space / on this day in history, see
bot/world.py). Stdlib only: the workflow runs it before any package is installed, so a world Short can skip the
footage download, Blender and the Remotion overlay (fewer runner minutes).

    python -m bot.topic      writes world=true|false to GITHUB_OUTPUT and BOT_TOPIC to GITHUB_ENV

Repo variable BOT_WORLD: "most" (default, owner 2026-11-07: 3 of every 4 Shorts are world Shorts, the 4th is
liminality), "mix" (Shorts alternate liminality / world), "only" (every Short is a world Short) or "off"
(liminality only).
A manual run's "topic" input (auto | liminality | world) and a forced dry-run template (world_space, world_history,
or a liminality style) win over the variable. The long video is always liminality.
"""
from __future__ import annotations

import os
import sys

from . import config
from .state import FAILED, Ledger

MODES = ("most", "mix", "only", "off", "games")
WORLD_RUN = {"most": 3, "mix": 1, "games": 1}   # "games": liminality Shorts alternate with our other games   # world Shorts in a row before a liminality one
WORLD_TEMPLATES = ("world_space", "world_history", "world_nature", "world_clips", "world_game")


def is_world(row: dict) -> bool:
    return str(row.get("template") or "").startswith("world_")


def mode(env: dict) -> str:
    m = (env.get("BOT_WORLD") or "most").strip().lower()
    return m if m in MODES else "most"


def choose(history: list[dict], env: dict | None = None) -> bool:
    """True = make a world Short. history = Ledger.history() (one row per episode, oldest first)."""
    e = os.environ if env is None else env
    if (e.get("BOT_KIND") or "short").strip().lower() != "short":
        return False
    template = (e.get("BOT_TEMPLATE") or "").strip().lower()
    if template:
        return template in WORLD_TEMPLATES
    forced = (e.get("BOT_TOPIC") or e.get("INPUT_TOPIC") or "").strip().lower()
    if forced in ("world", "liminality"):
        return forced == "world"
    m = mode(e)
    if m not in WORLD_RUN:
        return m == "only"
    n = WORLD_RUN[m]
    shorts = [r for r in history if (r.get("kind") or "short") == "short" and r.get("status") != FAILED]
    last = shorts[-n:]
    return not (len(last) == n and all(is_world(r) for r in last))


def main() -> int:
    world = choose(Ledger(config.STATE).history())
    for key, line in (("GITHUB_OUTPUT", f"world={'true' if world else 'false'}"),
                      ("GITHUB_ENV", f"BOT_TOPIC={'world' if world else 'liminality'}")):
        path = os.environ.get(key)
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    print("topic:", "world" if world else "liminality", f"(BOT_WORLD={mode(os.environ)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
