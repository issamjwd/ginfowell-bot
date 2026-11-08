"""Runner-minute budget for the extra Shorts. Stdlib only (it runs in the gate step, before anything is installed).

GitHub Free gives this private repo 2,000 Actions minutes a month and bills every job rounded UP to a whole minute.
The base schedule (the long video + 3 Shorts a day, their backups, CI) is sized to fit. The two extra Short slots
(bot/slots.py) post only when this check says the month still fits:

    used      = sum over this month's runs and jobs of ceil(job minutes)       (GitHub REST API, the run's own token)
    projected = used + this extra Short + the base schedule for the rest of the month
    extra Short posts only if projected <= EXTRA_LIMIT (1,850; ~150 minutes of headroom for retries and dry runs)

The base slots never wait on this check; they only log a warning when the projection passes WARN_LIMIT (1,950).
Any API problem raises BudgetError: the gate then skips the extra Short (fail safe), never a base one.

The per-run costs below are MEASURED billed minutes (README "COSTS / LIMITS"); change them when the jobs change.
"""
from __future__ import annotations

import http.client
import json
import math
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

MONTH_LIMIT = 2000   # GitHub Free, private repo
EXTRA_LIMIT = 1850   # an extra Short posts only while the month's projection stays at or under this
WARN_LIMIT = 1950    # base slots warn (never stop) when the projection passes this

# measured billed minutes per run (2026-11-06, ubuntu-24.04, 2 cores; README "COSTS / LIMITS")
SHORT_MAKE = 5       # a Short's make job
LONG_MAKE = 9        # the long video's make job (8 min 22 s with a cold clip-profile cache, dry run 42236456241)
POST = 3             # claim + upload + record on a live run (~15 s each, 1 minute each)
NOOP = 1             # a cron whose slot is already filled (or an extra slot without budget): the gate only
CI_PER_DAY = 2       # ci.yml (pull requests and code pushes to main only), averaged
BASE_SHORTS = 3
BACKUP_CRONS = 3     # 2 Short backups (13:49, 18:19) + the long backup (16:41): no-ops when the main cron fired
EXTRA_SLOTS = 2



def base_day(long_days: str = "daily", base_shorts: int = BASE_SHORTS, extra_slots: int = EXTRA_SLOTS) -> float:
    """The base schedule's cost per day, what the rest of the month is projected at. The long video and its backup
    count on the LONG_DAYS share of the week (repo variable: "daily", or weekday numbers; unset = 1,3,5)."""
    v = (long_days or "1,3,5").strip()
    if v == "daily":
        share = 1.0
    else:
        days = {d.strip() for d in v.split(",") if d.strip() in ("0", "1", "2", "3", "4", "5", "6")}
        share = len(days) / 7 if days else 3 / 7
    return (share * (LONG_MAKE + POST + NOOP) + base_shorts * (SHORT_MAKE + POST) + (BACKUP_CRONS - 1) * NOOP
            + extra_slots * NOOP + CI_PER_DAY)


BASE_DAY = base_day("daily")   # the owner's setting since 2026-11-05 (LONG_DAYS=daily)
EXTRA_SHORT = SHORT_MAKE + POST   # what one extra Short adds (conservative: its slot's no-op is in BASE_DAY too)

API = "https://api.github.com"
TIMEOUT = 15         # seconds per API request
MAX_PAGES = 10       # the runs list returns at most 1,000 runs for a filtered query anyway
WORKERS = 8
DEADLINE = 45        # seconds for the whole sweep: a slow API must never delay a post or cost a minute


class BudgetError(RuntimeError):
    """This month's usage could not be read: the caller must skip the extra Short."""


# ------------------------------------------------------------------------------------------------ maths (pure)
def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def job_minutes(job: dict, now: datetime) -> int:
    """Billed minutes of one job: its run time rounded UP to a whole minute; 0 for a job that never ran (skipped,
    or still queued). A job still running counts up to `now`."""
    if job.get("conclusion") == "skipped":
        return 0
    start = _ts(job.get("started_at"))
    if start is None:
        return 0
    end = _ts(job.get("completed_at")) if job.get("status") == "completed" else now
    if end is None:
        end = now
    secs = (end - start).total_seconds()
    if secs <= 0:
        return 0 if job.get("status") == "completed" else 1
    return math.ceil(secs / 60)


def is_short_run(run: dict) -> bool:
    """A completed single-attempt run that took one minute or less from start to finish: at most one job ran, so it
    bills 1 minute (0 if skipped) and its jobs need not be fetched. (Live episode jobs run one after another and a
    make job that does anything takes minutes, so this never hides a multi-job run.)"""
    if run.get("status") != "completed" or int(run.get("run_attempt") or 1) != 1:
        return False
    start, end = _ts(run.get("run_started_at")), _ts(run.get("updated_at"))
    return start is not None and end is not None and (end - start).total_seconds() <= 60


def month_start(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def next_month(now: datetime) -> datetime:
    first = month_start(now)
    return (first + timedelta(days=32)).replace(day=1)


def usage(runs: Iterable[dict], jobs_of: Callable[[dict], list[dict]], now: datetime,
          skip_run: int | None = None) -> int:
    """Billed minutes of this month's runs. `jobs_of(run)` returns the run's jobs (all attempts); it is called only
    for runs that may have more than one billed job. `skip_run` = the current run (its cost is the caller's estimate)."""
    start, total, need = month_start(now), 0, []
    for run in runs:
        if skip_run is not None and int(run.get("id", -1)) == skip_run:
            continue
        created = _ts(run.get("created_at"))
        if created is not None and created < start:
            continue
        if is_short_run(run):
            total += 0 if run.get("conclusion") == "skipped" else 1
        else:
            need.append(run)
    if need:
        with ThreadPoolExecutor(max_workers=min(WORKERS, len(need))) as pool:
            for jobs in pool.map(jobs_of, need):
                total += sum(job_minutes(j, now) for j in jobs)
    return total


def days_left(now: datetime) -> float:
    """Days from now to the end of the UTC month, counting the rest of today as a fraction."""
    return max(0.0, (next_month(now) - now.astimezone(timezone.utc)).total_seconds() / 86400)


def projected(used: int, now: datetime, extra: int = 0, base_day: float = BASE_DAY) -> float:
    """The month's projected billed minutes: what is used, plus `extra`, plus the base schedule until the month ends."""
    return used + extra + days_left(now) * base_day


def extra_allowed(used: int, now: datetime, limit: int = EXTRA_LIMIT,
                  base: float = BASE_DAY) -> tuple[bool, str]:
    """(post, reason) for one extra Short."""
    p = projected(used, now, EXTRA_SHORT, base)
    verdict = "fits" if p <= limit else "would not fit"
    return p <= limit, (f"budget: {used} min used this month; with this extra Short and the base schedule "
                        f"({base:.0f}/day x {days_left(now):.1f} days) the month projects to {p:.0f} of "
                        f"{MONTH_LIMIT}: {verdict} under {limit}")


def base_warning(used: int, now: datetime, extra: int = 0, base: float = BASE_DAY) -> str | None:
    """A warning when even the base schedule would pass WARN_LIMIT this month (the base still posts)."""
    p = projected(used, now, extra, base)
    if p <= WARN_LIMIT:
        return None
    return (f"runner minutes: {used} used this month, the base schedule projects to {p:.0f} of {MONTH_LIMIT} "
            f"(over {WARN_LIMIT}). Stop dry runs, or set LONG_DAYS to fewer days, or GitHub will stop every "
            f"workflow when the {MONTH_LIMIT} free minutes run out")


# ------------------------------------------------------------------------------------------------ GitHub API
def _get(url: str, token: str, deadline: float | None = None) -> dict:
    if deadline is not None and time.monotonic() > deadline:
        raise BudgetError("GitHub API: the budget sweep ran out of time")
    if not url.startswith(API + "/"):
        raise BudgetError(f"refusing a non-GitHub API url: {url[:80]}")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pixelhelm-channel-bot-budget"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - https api.github.com only (above)
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as e:
        raise BudgetError(f"GitHub API: {type(e).__name__}: {str(e)[:200]}") from e
    if not isinstance(data, dict):
        raise BudgetError("GitHub API: unexpected response")
    return data


def month_used(repo: str, token: str, now: datetime, skip_run: int | None = None) -> int:
    """This month's billed minutes for the whole repo, from the REST API. Raises BudgetError on ANY failure (odd
    payloads included), so the caller can fail safe."""
    if not token or not repo or repo.count("/") != 1:
        raise BudgetError("no token or repository")
    try:
        return _month_used(repo, token, now, skip_run, time.monotonic() + DEADLINE)
    except BudgetError:
        raise
    except Exception as e:  # noqa: BLE001 - whatever the API sent, the answer is "unknown", never a crash
        raise BudgetError(f"unreadable usage: {type(e).__name__}: {str(e)[:200]}") from e


def _month_used(repo: str, token: str, now: datetime, skip_run: int | None, deadline: float) -> int:
    since = month_start(now).strftime("%Y-%m-%d")
    runs: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = _get(f"{API}/repos/{repo}/actions/runs?created=%3E%3D{since}&per_page=100&page={page}", token, deadline)
        batch = data.get("workflow_runs")
        if not isinstance(batch, list):
            raise BudgetError("unexpected runs response")
        runs += batch
        if len(batch) < 100 or len(runs) >= int(data.get("total_count") or 0):
            break

    def jobs_of(run: dict) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 4):
            data = _get(f"{API}/repos/{repo}/actions/runs/{int(run['id'])}/jobs?filter=all&per_page=100&page={page}",
                        token, deadline)
            batch = data.get("jobs")
            if not isinstance(batch, list):
                raise BudgetError("unexpected jobs response")
            out += batch
            if len(batch) < 100:
                break
        return out

    return usage(runs, jobs_of, now, skip_run)
