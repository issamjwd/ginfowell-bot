"""Catch-up gate for scheduled runs. Stdlib only (it runs before anything is installed).

GitHub's cron is best-effort: a scheduled run can start late or never. So every posting slot has a main cron and a
backup cron later the same UTC day (.github/workflows/episode.yml, mirrored in SHORT_SLOTS / EXTRA_SLOTS /
LONG_SLOTS below). Every scheduled run first asks this gate, as the first step of the make job:

    python3 -m bot.slots gate        -> writes run=true|false, slot_day and extra=true|false to $GITHUB_OUTPUT
                                        (the cron string comes in $SCHEDULE)

  * a manual run (workflow_dispatch) always runs
  * base Short slot n (1, 2, 3) runs only while fewer than n BASE Shorts were posted this UTC day. A dropped slot is
    caught up by its backup or by the next slot's crons; a slot that fired turns its backup into a ~1-minute no-op;
    never more than 3 base Shorts a day. The base Shorts never wait on the minute budget.
  * extra Short slots (10:19 and 23:29 UTC, owner 2026-11-06: 3-5 Shorts a day) post an EXTRA Short only while
    fewer extras than the slot's number were posted that day AND the month's runner minutes still fit
    (bot/budget.py: projected <= 1,850 of the 2,000 free minutes) AND the YouTube quota day keeps room for the base
    videos still to come. An unreadable budget skips the extra (fail safe). The 23:29 slot is also the third base
    slot's backup: while fewer than 3 base Shorts are up that day it posts a base Short instead (no budget check).
    Never more than 2 extras a day, so never more than 5 Shorts.
  * the long-video slot runs only if no long video was posted this UTC day (LONG_DAYS is checked by the workflow's
    job-level `if`, which costs nothing)
  * "posted" = the episode's newest ledger row is claimed / uploaded / unclear (it may be on YouTube). A "failed"
    upload does not count, so a backup retries it. A claim row with "extra": true is an extra Short (the gate's
    extra output, recorded by bot/publish.py); every other Short row is a base Short.
  * the day is the SLOT's day: GitHub can start a cron hours late, past midnight (the 21:13 Short ran at 01:27 the
    next day). A scheduled run whose cron hour is later than the current UTC hour belongs to yesterday's slot, is
    counted against yesterday, and skips if yesterday's slot is already filled. The claim row records that day
    (slot_day), so a late post never uses up the new day's slot. Rows without slot_day count on their claim's UTC day.

The gate reads the NEWEST ledger (the step fetches main's state/episodes.jsonl, not the commit the run started
from), and each kind's concurrency group makes a backup wait until a running episode of that kind has claimed.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import budget
from .state import (CLAIMED, DAILY_QUOTA, INSERT_COST, THUMB_COST, UNCLEAR, UPLOADED, Ledger, StateError,
                    pacific_date, quota_used_in, utcnow)

POSTED = (CLAIMED, UPLOADED, UNCLEAR)  # any of these may be on YouTube already: never post a second one for it
SHORTS_PER_DAY = 3      # base Shorts: always posted, never wait on the minute budget
EXTRA_PER_DAY = 2       # extra Shorts on top, only when the month's minutes fit (bot/budget.py)
# owner 2026-11-07: 4 Shorts + 1 long video every day. November's free minutes were mostly used by then, so from
# FOUR_FROM the 10:19 slot is base Short slot 1 (the others shift to 2, 3, 4) and only 23:29 stays an extra slot
# (it is base slot 4's backup first). Before FOUR_FROM the 10:19 slot is the budget-checked extra it always was.
FOUR_FROM = date(2026, 12, 1)

# cron "minute hour" -> slot. The workflow's crons must match these (tests/test_slots.py checks both ways).
# Base slots and their backups (the 23:29 extra slot is the third base slot's backup).
SHORT_SLOTS = {"7 13": 1, "49 13": 1, "37 17": 2, "19 18": 2, "13 21": 3}
# Extra slots: cron -> how many extras may be up that day once it has posted (no backups: a dropped extra is just
# not made). CATCH_UP: the slot posts a BASE Short instead while the day has fewer than SHORTS_PER_DAY of them.
EXTRA_SLOTS = {"19 10": 1, "29 23": 2}
CATCH_UP = ("29 23",)
LONG_SLOTS: tuple[str, ...] = ()   # ginfowell: no long video (its crons are removed from the workflow)


@dataclass(frozen=True)
class Decision:
    """What the gate decided for one trigger, from the ledger alone."""

    run: bool
    why: str
    extra: bool = False     # True: an extra Short; it may run only if the minute budget allows (main() checks)


def _head(schedule: str) -> str:
    return " ".join((schedule or "").split()[:2])


def slot_of(schedule: str) -> tuple[str, int] | None:
    """(kind, slot) for a cron string, or None if it is not one of the bot's. kind: short | extra | long."""
    head = _head(schedule)
    if head in SHORT_SLOTS:
        return "short", SHORT_SLOTS[head]
    if head in EXTRA_SLOTS:
        return "extra", EXTRA_SLOTS[head]
    if head in LONG_SLOTS:
        return "long", 1
    return None


def shorts_per_day(day: date) -> int:
    return 4 if day >= FOUR_FROM else SHORTS_PER_DAY


def extras_per_day(day: date) -> int:
    return 1 if day >= FOUR_FROM else EXTRA_PER_DAY


def slot_on(schedule: str, day: date) -> tuple[str, int] | None:
    """slot_of() for the slot's own day: from FOUR_FROM the 10:19 cron is base Short slot 1 and the rest shift."""
    slot = slot_of(schedule)
    if slot is None or day < FOUR_FROM:
        return slot
    kind, n = slot
    if _head(schedule) == "19 10":
        return "short", 1
    if kind == "short":
        return "short", n + 1
    if kind == "extra":
        return "extra", 1
    return slot


def _day(ts: str):
    """The UTC calendar day of a ledger timestamp (a naive one is UTC)."""
    t = datetime.fromisoformat(ts)
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()


def slot_day(now: datetime, event: str, schedule: str = "") -> date:
    """The UTC day a trigger posts for. A scheduled run whose cron hour is later than the current hour started late,
    after midnight: it is yesterday's slot. Everything else (on time, manual dispatch) is today."""
    now = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    today = now.date()
    if event != "schedule":
        return today
    parts = (schedule or "").split()
    try:
        hour = int(parts[1])
    except (IndexError, ValueError):
        return today
    return today - timedelta(days=1) if hour > now.hour else today


def _episode_day(first: dict):
    """The day an episode counts on: its recorded slot day, else the UTC day of its claim."""
    sd = first.get("slot_day")
    if isinstance(sd, str):
        try:
            return datetime.strptime(sd, "%Y-%m-%d").date()
        except ValueError:
            pass
    ts = first.get("ts") or ""
    return _day(ts) if ts else None


def posted_on(rows: list[dict], day, kind: str | None = None, extra: bool | None = None) -> list[str]:
    """Episode ids (of `kind`, if given) whose newest row is claimed / uploaded / unclear and that count on `day`
    (their slot_day, else their claim's UTC day). Rows written before the long video existed have no kind: Shorts.
    extra=True / False keeps only extra / base Shorts (a claim without "extra": true is a base one)."""
    latest: dict[str, dict] = {}
    first: dict[str, dict] = {}
    for r in rows:
        latest[r["episode_id"]] = r
        first.setdefault(r["episode_id"], r)
    out = []
    for ep, r in latest.items():
        k = first[ep].get("kind") or "short"
        is_extra = first[ep].get("extra") is True
        if (r["status"] in POSTED and _episode_day(first[ep]) == day and (kind is None or k == kind)
                and (extra is None or is_extra == extra)):
            out.append(ep)
    return out


def long_skip_days(raw: str | None = None) -> set[str]:
    """Days (UTC slot days, YYYY-MM-DD) with no long video, from the repo variable LONG_SKIP ("2026-11-08,...").
    Dated, so it expires by itself (owner 2026-11-06: skip 4 November days to stay inside the free minutes).
    Anything that is not a valid date is ignored."""
    raw = os.environ.get("LONG_SKIP", "") if raw is None else raw
    out = set()
    for part in raw.split(","):
        part = part.strip()
        try:
            out.add(date.fromisoformat(part).isoformat())
        except ValueError:
            continue
    return out


def decide(rows: list[dict], now: datetime, event: str, schedule: str = "") -> Decision:
    """What this trigger should do, from the ledger alone (the minute budget for an extra Short is main()'s job)."""
    if event != "schedule":
        return Decision(True, f"{event or 'manual'} run: always runs")
    day = slot_day(now, event, schedule)
    slot = slot_on(schedule, day)
    if slot is None:
        return Decision(False, f"unknown schedule {schedule!r}: not one of the bot's slots")
    kind, n = slot
    today = slot_day(now, "", "")
    when = "today" if day == today else f"on {day} (this cron started late, after midnight)"
    if kind == "long":
        if day.isoformat() in long_skip_days():
            return Decision(False, f"the long video is skipped on {day} (repo variable LONG_SKIP, saves runner minutes)")
        done = posted_on(rows, day, "long")
        if done:
            return Decision(False, f"the long video is already posted {when} ({', '.join(done)})")
        return Decision(True, f"no long video posted {when}")
    base = posted_on(rows, day, "short", extra=False)
    if kind == "short":
        if len(base) >= min(n, shorts_per_day(day)):
            return Decision(False, f"Short slot {n}: {len(base)} base Short(s) already posted {when}")
        return Decision(True, f"Short slot {n}: {len(base)} base Short(s) posted {when}, making one")
    if _head(schedule) in CATCH_UP and len(base) < shorts_per_day(day):
        return Decision(True, f"extra slot {n}: only {len(base)} base Short(s) posted {when}, catching up a base one")
    extras = posted_on(rows, day, "short", extra=True)
    if len(extras) >= min(n, extras_per_day(day)):
        return Decision(False, f"extra slot {n}: {len(extras)} extra Short(s) already posted {when}")
    return Decision(True, f"extra slot {n}: {len(extras)} extra Short(s) posted {when}, one more if the budget allows",
                    extra=True)


def due(rows: list[dict], now: datetime, event: str, schedule: str = "") -> tuple[bool, str]:
    """(run, reason) for this trigger from the ledger alone (an extra Short still needs main()'s budget check)."""
    d = decide(rows, now, event, schedule)
    return d.run, d.why


def quota_room(rows: list[dict], now: datetime, day: date) -> tuple[bool, str]:
    """Whether an extra Short still leaves the YouTube quota day (Pacific) room for the base videos not yet up on
    the slot day (each video costs INSERT_COST + THUMB_COST; the ledger refuses a claim past DAILY_QUOTA)."""
    per = INSERT_COST + THUMB_COST
    used = quota_used_in(rows, pacific_date(now))
    left = max(0, shorts_per_day(day) - len(posted_on(rows, day, "short", extra=False)))
    left += 0 if posted_on(rows, day, "long") else 1
    need = used + per * (1 + left)
    return need <= DAILY_QUOTA, f"YouTube quota: {used} used, this Short + {left} base video(s) to come = {need} of {DAILY_QUOTA}"


def _month_used(now: datetime) -> int:
    """This month's billed minutes (bot/budget.py). Raises budget.BudgetError on any failure."""
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return budget.month_used(os.environ.get("REPO", ""), os.environ.get("GH_TOKEN", ""), now,
                             int(run_id) if run_id.isdigit() else None)


def _base_per_day(day: date | None = None) -> float:
    day = day or utcnow().date()
    return budget.base_day(os.environ.get("LONG_DAYS", "") or "1,3,5", base_shorts=shorts_per_day(day),
                           extra_slots=extras_per_day(day))


def _budget_check(rows: list[dict], now: datetime, day: date) -> tuple[bool, str]:
    """(post the extra Short, reason). Fails safe: ANY problem (quota, API, odd data) skips the extra."""
    try:
        ok, why = quota_room(rows, now, day)
        if not ok:
            return False, why + ": no extra Short"
        return budget.extra_allowed(_month_used(now), now, base=_base_per_day(day))
    except Exception as e:  # noqa: BLE001 - an extra Short is optional: unknown means no
        return False, f"budget unknown ({type(e).__name__}: {str(e)[:200]}): extra Short skipped (fail safe)"


def _base_warning(now: datetime, extra_minutes: int) -> str | None:
    """A warning when the base schedule itself would pass budget.WARN_LIMIT. Never raises: a base post never waits
    on the budget."""
    if not os.environ.get("GH_TOKEN"):
        return None
    try:
        return budget.base_warning(_month_used(now), now, extra_minutes, base=_base_per_day())
    except Exception as e:  # noqa: BLE001 - only a warning
        return f"budget: could not read this month's minutes ({type(e).__name__}: {str(e)[:200]})"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["gate"]:
        print("usage: python3 -m bot.slots gate", file=sys.stderr)
        return 2
    path = Path(os.environ.get("BOT_LEDGER") or Path(__file__).resolve().parent.parent / "state" / "episodes.jsonl")
    try:
        rows = Ledger(path).rows()
    except StateError as e:  # an unreadable ledger must stop the run, never guess
        print(f"refused: {e}", file=sys.stderr)
        return 3
    now, event, schedule = utcnow(), os.environ.get("GITHUB_EVENT_NAME", ""), os.environ.get("SCHEDULE", "")
    d = decide(rows, now, event, schedule)
    day = slot_day(now, event, schedule)
    run, why, notes = d.run, d.why, []
    if run and d.extra:
        run, verdict = _budget_check(rows, now, day)
        why = f"{why}; {verdict}"
    elif run and event == "schedule":
        # a base post: never stopped by the budget, only warned (the cost of THIS run is counted in the projection)
        kind = (slot_of(schedule) or ("short", 0))[0]
        this = budget.LONG_MAKE + budget.POST if kind == "long" else budget.SHORT_MAKE + budget.POST
        warn = _base_warning(now, this)
        if warn:
            notes.append(warn)
    print(f"run={str(run).lower()}: {why}")
    for n in notes:
        print(f"::warning::{n}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"run={str(run).lower()}\n")
            fh.write(f"slot_day={day.isoformat()}\n")     # recorded on the claim (bot/publish.py)
            fh.write(f"extra={str(run and d.extra).lower()}\n")   # recorded on the claim too
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"gate: run={str(run).lower()} ({why})\n")
            for n in notes:
                fh.write(f"WARNING: {n}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
