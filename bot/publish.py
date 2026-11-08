"""Publishing: claim -> upload -> record. Stdlib only, so the jobs holding YouTube secrets or git write access run
no third-party package at all.

    python -m bot.publish claim            (job "claim":  contents: write, no YouTube secrets)
    python -m bot.publish upload           (job "upload": environment "youtube" secrets, read-only checkout)
    python -m bot.publish record <file>    (job "record": contents: write, no YouTube secrets)

Live work refuses to run unless DRY_RUN is false, BOT_ENABLED is true, and it is on GitHub Actions
(GITHUB_ACTIONS=true): nothing can post from the owner's PC.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from . import config, youtube
from .state import CLAIMED, FAILED, UNCLEAR, UPLOADED, Ledger, StateError, scrub

log = logging.getLogger(__name__)
EPISODE_ID = re.compile(r"^ep-[a-z0-9-]{4,64}$")
RESULT_KEYS = {"episode_id", "status", "video_id", "thumbnail", "privacy", "note"}


def require_live(settings: config.Settings) -> None:
    if settings.dry_run:
        raise StateError("dry run: nothing is claimed or uploaded")
    if not settings.enabled:
        raise StateError("BOT_ENABLED is not true: nothing is claimed or uploaded")
    if not settings.in_actions:
        raise StateError("live publishing only runs on GitHub Actions (GITHUB_ACTIONS=true)")


def current(out_root: Path | None = None) -> tuple[dict, Path]:
    out_root = out_root or config.OUT
    cur = json.loads((out_root / "current.json").read_text(encoding="utf-8"))
    ep = cur.get("episode_id", "")
    if not EPISODE_ID.match(ep):
        raise StateError(f"bad episode id {ep!r} in current.json")
    folder = out_root / ep
    sb = json.loads((folder / "storyboard.json").read_text(encoding="utf-8"))
    if sb.get("episode_id") != ep:
        raise StateError("current.json and storyboard disagree")
    return sb, folder


def claim(settings: config.Settings, ledger: Ledger, out_root: Path | None = None) -> dict:
    require_live(settings)
    sb, _ = current(out_root)
    info = {k: sb.get(k) for k in ("template", "format", "dream_ids", "monster", "lines", "fingerprint", "title")}
    music = sb.get("music") or {}
    # kind / music seed / tempo feed the variety guard (bot/variety.py) for the next runs
    info.update(kind=sb.get("kind") or "short", music_seed=music.get("seed"), bpm=music.get("bpm"),
                music_track=music.get("track"), accent=(sb.get("palette") or {}).get("accent"))
    # the slot's own UTC day from the gate (bot/slots.py): a cron that started late, after midnight, counts on
    # yesterday's slot, not the new day's
    sd = os.environ.get("BOT_SLOT_DAY", "")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", sd):
        try:
            datetime.strptime(sd, "%Y-%m-%d")
            info["slot_day"] = sd
        except ValueError:
            pass
    # an extra Short (the gate's budget-checked 4th / 5th Short of the day): the gate counts extras apart from the 3
    # base Shorts, so a base slot never skips because an extra went up first
    if info["kind"] == "short" and os.environ.get("BOT_SLOT_EXTRA", "") == "true":
        info["extra"] = True
    return ledger.claim(sb["episode_id"], {**info, "privacy": settings.privacy, "synthetic": sb.get("synthetic", False)})


def upload(settings: config.Settings, ledger: Ledger, client: youtube.Client, out_root: Path | None = None) -> dict:
    """Upload the claimed current episode exactly once. Always returns (and appends) a result row; never raises
    after bytes may have reached YouTube."""
    require_live(settings)
    sb, folder = current(out_root)
    ep = sb["episode_id"]
    mine = [r for r in ledger.rows() if r["episode_id"] == ep]
    if len(mine) != 1 or mine[0]["status"] != CLAIMED:
        raise StateError(f"{ep} is not freshly claimed ({[r['status'] for r in mine]}); refusing to upload")
    try:
        vid = client.upload(folder / "video.mp4", youtube.video_meta(sb, settings.privacy))
    except (youtube.UploadFailed, youtube.QuotaError, youtube.ScopeError) as e:
        return _result(ledger, folder, {"episode_id": ep, "status": FAILED, "note": str(e)})
    except Exception as e:  # noqa: BLE001 - anything else after the session may exist is "unclear", recorded
        return _result(ledger, folder, {"episode_id": ep, "status": UNCLEAR, "note": f"{type(e).__name__}: {e}"})
    try:
        thumb_ok = client.set_thumbnail(vid, folder / "thumb.jpg")
    except Exception as e:  # noqa: BLE001 - the video is up; a thumbnail problem must not lose the video id
        log.warning("thumbnail failed: %s", scrub(str(e)))
        thumb_ok = False
    return _result(ledger, folder, {"episode_id": ep, "status": UPLOADED, "video_id": vid, "thumbnail": thumb_ok,
                                    "privacy": settings.privacy})


def _result(ledger: Ledger, folder: Path, row: dict) -> dict:
    row = ledger.append(row)
    (folder.parent / "result.json").write_text(json.dumps(row), encoding="utf-8")
    return row


def record(ledger: Ledger, result_path: Path) -> dict:
    """Append the upload job's result row to the (freshly checked out) ledger, after checking it fits."""
    row = json.loads(result_path.read_text(encoding="utf-8"))
    ep = row.get("episode_id", "")
    if not EPISODE_ID.match(ep) or row.get("status") not in (UPLOADED, FAILED, UNCLEAR):
        raise StateError("result.json is not a valid result row")
    latest = ledger.latest().get(ep)
    if not latest or latest["status"] != CLAIMED:
        raise StateError(f"{ep} is not waiting for a result (latest: {latest and latest['status']})")
    clean = {k: v for k, v in row.items() if k in RESULT_KEYS}
    if clean.get("video_id") is not None and not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", str(clean["video_id"])):
        raise StateError("bad video id")
    return ledger.append(clean)


def client_from_env(settings: config.Settings | None = None) -> youtube.Client:
    settings = settings or config.Settings.from_env()
    if not settings.in_actions:
        raise config.ConfigError("the YouTube client only runs on GitHub Actions (GITHUB_ACTIONS=true)")
    missing = [k for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN") if not os.environ.get(k)]
    if missing:
        raise config.ConfigError(f"missing secrets: {', '.join(missing)} (SETUP.md step 7)")
    return youtube.Client(os.environ["YT_CLIENT_ID"], os.environ["YT_CLIENT_SECRET"], os.environ["YT_REFRESH_TOKEN"])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = config.Settings.from_env()
    ledger = Ledger(config.STATE)
    cmd = argv[0] if argv else ""
    try:
        if cmd == "claim":
            print("claimed", claim(settings, ledger)["episode_id"])
            return 0
        if cmd == "upload":
            row = upload(settings, ledger, client_from_env(settings))
            print(json.dumps(row))
            return {UPLOADED: 0, FAILED: 4, UNCLEAR: 5}[row["status"]]
        if cmd == "record" and len(argv) == 2:
            row = record(ledger, Path(argv[1]))
            print("recorded", row["episode_id"], row["status"])
            return 0 if row["status"] == UPLOADED else {FAILED: 4, UNCLEAR: 5}[row["status"]]
        if cmd == "preflight":
            client_from_env(settings).token()
            print("youtube token ok (scope: youtube.upload only)")
            return 0
    except (StateError, config.ConfigError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    print("usage: python -m bot.publish claim | upload | record <result.json> | preflight", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
