"""CLI for making episodes (never touches YouTube; publishing is `python -m bot.publish`).

    python -m bot make [--seed N]   plan + render + QA into out/<episode>/
    python -m bot sprites           render the cast pose sprites with Blender into the cache (CI: cache miss only)
    python -m bot status            print the ledger

Settings come from the environment: DRY_RUN (default true), BOT_FORMAT (auto|footage|animated), YT_PRIVACY
(private|unlisted|public, default private), BOT_LOW_RES, BLENDER, BOT_FOOTAGE_DIR.
"""
from __future__ import annotations

import argparse
import json
import logging

from . import config
from .state import Ledger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot")
    ap.add_argument("command", choices=["make", "sprites", "status"])
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = config.Settings.from_env()
    ledger = Ledger(config.STATE)

    if args.command == "status":
        for r in ledger.history():
            print(json.dumps({k: r.get(k) for k in ("episode_id", "status", "template", "video_id", "ts")}))
        unresolved = ledger.unresolved()
        if unresolved:
            print(f"{len(unresolved)} episode(s) need a look in YouTube Studio:", [r["episode_id"] for r in unresolved])
        return 0
    if args.command == "sprites":
        from . import sprites
        res = 512 if settings.low_res else 1024
        folder = sprites.ensure_sprites(settings.blender, res=res, samples=16 if settings.low_res else 24)
        print("sprites:", folder or "Blender unavailable, seed sprites will be used")
        return 0 if folder else 6
    from . import pipeline
    made = pipeline.make(settings, ledger, seed=args.seed)
    print(f"made {made.episode_id}: {made.video} ({made.storyboard['title']})")
    print(made.qa.as_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
