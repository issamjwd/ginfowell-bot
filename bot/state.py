"""Episode ledger: state/episodes.jsonl (append-only, committed back to the repo by the workflow).

Statuses, in order: "claimed" (written and pushed BEFORE the upload starts) -> "uploaded" (with video_id) or
"unclear" (the upload may or may not have landed) or "failed" (it certainly did not reach YouTube).
Rule: an episode that was ever claimed is never uploaded again by the bot. "unclear" waits for the owner to look in
YouTube Studio; that is how we never double-post on an ambiguous network result.

Stdlib only: the claim/upload/record jobs run it without installing any package.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

CLAIMED, UPLOADED, UNCLEAR, FAILED = "claimed", "uploaded", "unclear", "failed"
STATUSES = (CLAIMED, UPLOADED, UNCLEAR, FAILED)
DAILY_QUOTA = 10_000           # default YouTube Data API project quota, reset at midnight Pacific time
INSERT_COST, THUMB_COST = 1600, 50
NOTE_MAX = 200


class StateError(RuntimeError):
    """The ledger is unreadable or a rule would be broken."""


def fingerprint(template: str, dream_ids: list[str], monster: str | None, lines: dict[str, str]) -> str:
    blob = json.dumps({"t": template, "d": list(dream_ids), "m": monster, "l": sorted(lines.items())},
                      sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def quota_used_in(rows: list[dict], day: date) -> int:
    """Units spent on a Pacific quota day by these ledger rows. Counted from claims: a claim may have spent units."""
    used = 0
    for r in rows:
        if r["status"] != CLAIMED or not r.get("ts"):
            continue
        if pacific_date(datetime.fromisoformat(r["ts"])) == day:
            used += int(r.get("quota_units", INSERT_COST + THUMB_COST))
    return used


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def pacific_date(t: datetime) -> date:
    """US Pacific calendar date of a UTC time (YouTube quota day), without needing tzdata.
    DST: second Sunday of March 02:00 PST (10:00 UTC) to first Sunday of November 02:00 PDT (09:00 UTC)."""
    t = t.astimezone(timezone.utc)
    start = datetime.combine(_nth_sunday(t.year, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=10)
    end = datetime.combine(_nth_sunday(t.year, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=9)
    offset = -7 if start <= t < end else -8
    return (t + timedelta(hours=offset)).date()


def scrub(text: str) -> str:
    """Ledger notes are committed to git: drop query strings (upload session ids, tokens) and cap the length."""
    text = re.sub(r"(https?://[^\s?]+)\?\S*", r"\1?...", str(text))
    text = re.sub(r"(?i)(bearer|token|secret|key)[=: ]+\S+", r"\1=...", text)
    return text[:NOTE_MAX]


@dataclass
class Ledger:
    path: Path
    clock: Callable[[], datetime] = field(default=utcnow)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise StateError(f"{self.path}:{n} is not valid JSON; fix it by hand before the next run") from e
            if row.get("status") not in STATUSES or not row.get("episode_id"):
                raise StateError(f"{self.path}:{n} has no valid status/episode_id")
            out.append(row)
        return out

    def append(self, row: dict) -> dict:
        if row.get("status") not in STATUSES:
            raise StateError(f"bad status {row.get('status')!r}")
        if "note" in row:
            row = {**row, "note": scrub(row["note"])}
        row = {"ts": self.clock().astimezone(timezone.utc).replace(microsecond=0).isoformat(), **row}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return row

    # ---- queries
    def latest(self) -> dict[str, dict]:
        """episode_id -> its newest row."""
        out: dict[str, dict] = {}
        for r in self.rows():
            out[r["episode_id"]] = r
        return out

    def ever_claimed(self, episode_id: str) -> bool:
        return any(r["episode_id"] == episode_id for r in self.rows())

    def used_fingerprints(self) -> set[str]:
        return {r["fingerprint"] for r in self.rows() if r.get("fingerprint")}

    def history(self) -> list[dict]:
        """One row per episode (its claim row data merged with the newest status), oldest first."""
        seen: dict[str, dict] = {}
        for r in self.rows():
            seen.setdefault(r["episode_id"], {}).update(r)
        return list(seen.values())

    def unresolved(self) -> list[dict]:
        return [r for r in self.latest().values() if r["status"] in (CLAIMED, UNCLEAR)]

    def quota_used(self, day: date) -> int:
        """Units spent on a Pacific quota day. Counted from claims: a claim may have spent units."""
        return quota_used_in(self.rows(), day)

    def claim(self, episode_id: str, info: dict, budget: int = DAILY_QUOTA) -> dict:
        """Record intent to upload. Refuses a second claim and refuses when the day's quota can't cover it."""
        if self.ever_claimed(episode_id):
            raise StateError(f"{episode_id} was already claimed; the bot never re-uploads an episode")
        day = pacific_date(self.clock())
        cost = INSERT_COST + THUMB_COST
        used = self.quota_used(day)
        if used + cost > budget:
            raise StateError(f"daily quota would be exceeded ({used} + {cost} > {budget}, Pacific day {day})")
        return self.append({**info, "episode_id": episode_id, "status": CLAIMED, "quota_units": cost})
