"""Variety guard: three Shorts a day never look like a repetitive, mass-produced feed (bot/variety.py)."""
from datetime import datetime, timedelta, timezone

import pytest

from bot import planner, variety
from tests.conftest import FAKE_CLIPS

T0 = datetime(2026, 11, 2, 13, 7, tzinfo=timezone.utc)
SLOTS = (timedelta(hours=0), timedelta(hours=4, minutes=30), timedelta(hours=8, minutes=6))  # 13:07, 17:37, 21:13


def _row(sb: dict, when: datetime) -> dict:
    return {**{k: sb[k] for k in ("episode_id", "template", "format", "dream_ids", "lines", "fingerprint", "title")},
            "kind": "short", "music_seed": sb["music"]["seed"], "bpm": sb["music"]["bpm"], "status": "uploaded",
            "ts": when.isoformat()}


def _weeks(days: int, seed0: int = 0) -> list[tuple[datetime, dict]]:
    history, used, out = [], set(), []
    for day in range(days):
        for k, slot in enumerate(SLOTS):
            now = T0 + timedelta(days=day) + slot
            _, sb = planner.plan(history, used, "auto", FAKE_CLIPS, seed=seed0 + day * 3 + k, now=now)
            used.add(sb["fingerprint"])
            history.append(_row(sb, now))
            out.append((now, sb))
    return out


@pytest.fixture(scope="module")
def three_weeks():
    return _weeks(21)


def test_no_fingerprint_repeats_in_three_weeks(three_weeks):
    fps = [sb["fingerprint"] for _, sb in three_weeks]
    assert len(fps) == len(set(fps)) == 63


def test_same_day_shorts_differ_in_template_lead_dream_hook_and_music(three_weeks):
    for d in range(21):
        day = [sb for _, sb in three_weeks[d * 3:d * 3 + 3]]
        for key in (lambda s: s["template"], lambda s: s["dream_ids"][0],
                    lambda s: variety.first_line(s["lines"]), lambda s: s["music"]["seed"]):
            vals = [key(s) for s in day]
            assert len(set(vals)) == 3, (d, vals)


def test_templates_and_hooks_rotate(three_weeks):
    sbs = [sb for _, sb in three_weeks]
    for a, b in zip(sbs, sbs[1:]):
        assert a["template"] != b["template"]
    # every enabled template gets used in three weeks; no hook line dominates
    from bot import edits
    assert {s["template"] for s in sbs} == set(edits.STYLES)   # every live edit style gets used
    hooks = [variety.first_line(s["lines"]) for s in sbs]
    assert max(hooks.count(h) for h in set(hooks)) <= 63 // 4


def test_music_seed_never_repeats_within_14_days(three_weeks):
    for i, (t, sb) in enumerate(three_weeks):
        for t2, sb2 in three_weeks[i + 1:]:
            if t2 - t <= timedelta(days=14):
                assert sb["music"]["seed"] != sb2["music"]["seed"]


def test_hard_rules_flag_a_same_day_clone():
    now = T0
    row = {"template": "one_dream", "dream_ids": ["halls"], "lines": {"hook": "x"}, "fingerprint": "f1",
           "music_seed": 7, "bpm": 80, "ts": (now - timedelta(hours=3)).isoformat()}
    c = variety.Candidate("one_dream", ("halls",), {"hook": "x"}, "f2", 8, 81)
    errs = variety.hard_problems(c, [row], set(), now)
    assert "template already used today" in errs and "lead dream already used today" in errs
    assert "hook line already used today" in errs
    # the next day the same template + dream is only a soft problem (rotation), not a hard one
    later = now + timedelta(days=1)
    assert variety.hard_problems(c, [row], set(), later) == []
    assert "same template and lead dream in the last 14 days" in variety.soft_problems(c, [row], later)


def test_long_videos_do_not_count_against_shorts():
    long_row = {"kind": "long", "template": "long_tour", "dream_ids": ["halls"], "ts": T0.isoformat()}
    assert variety.of_kind([long_row], "short") == []
    old_row = {"template": "one_dream"}  # rows from before the long video existed are Shorts
    assert variety.of_kind([old_row], "short") == [old_row]
