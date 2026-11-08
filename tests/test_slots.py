"""Catch-up gate (bot/slots.py): a dropped cron is caught up by a backup; a fired one is never doubled."""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot import slots

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "episode.yml"
DAY = "2026-11-04"


def at(hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime(2026, 11, 4, h, m, tzinfo=timezone.utc)


def short(ep, status="uploaded", ts=f"{DAY}T13:10:00+00:00", kind="short"):
    rows = [{"episode_id": ep, "status": "claimed", "ts": ts, **({"kind": kind} if kind else {})}]
    if status != "claimed":
        rows.append({"episode_id": ep, "status": status, "ts": ts})
    return rows


def test_manual_runs_always_run():
    assert slots.due(short("a") + short("b") + short("c"), at("13:49"), "workflow_dispatch")[0] is True


@pytest.mark.parametrize("cron,posted,run", [
    ("7 13 * * *", 0, True), ("49 13 * * *", 0, True), ("49 13 * * *", 1, False),   # slot 1 + its backup
    ("37 17 * * *", 1, True), ("37 17 * * *", 2, False), ("19 18 * * *", 1, True),  # slot 2
    ("37 17 * * *", 0, True),                                                      # slot 1 dropped: slot 2 catches up
    ("13 21 * * *", 2, True), ("13 21 * * *", 3, False), ("13 21 * * *", 9, False),  # never more than 3 a day
    ("29 23 * * *", 2, True),                                     # the 23:29 extra slot backs up slot 3 (base first)
])
def test_short_slots_count_todays_shorts(cron, posted, run):
    rows = [r for i in range(posted) for r in short(f"s{i}")]
    assert slots.due(rows, at("22:00"), "schedule", cron)[0] is run


def test_a_dropped_day_is_caught_up_one_short_per_cron():
    rows, made = [], []
    for cron, now in (("37 17 * * *", "17:40"), ("19 18 * * *", "18:20"), ("13 21 * * *", "21:15"),
                      ("29 23 * * *", "23:30")):
        d = slots.decide(rows, at(now), "schedule", cron)
        if d.run and not d.extra:       # (an extra Short needs the minute budget too: not part of this test)
            ep = f"ep{len(made)}"
            made.append(ep)
            rows += short(ep, ts=f"{DAY}T{now}:30+00:00")
    assert made == ["ep0", "ep1", "ep2"]   # slot 1's two crons were lost; three Shorts still go up that day


def test_failed_counts_as_not_posted_and_unclear_or_claimed_as_posted():
    assert slots.due(short("a", "failed"), at("13:50"), "schedule", "49 13 * * *")[0] is True
    for st in ("claimed", "unclear", "uploaded"):
        assert slots.due(short("a", st), at("13:50"), "schedule", "49 13 * * *")[0] is False


def test_long_slot_ignores_shorts_and_shorts_ignore_the_long_video():
    long_rows = short("L", kind="long", ts=f"{DAY}T15:30:00+00:00")
    three = [r for i in range(3) for r in short(f"s{i}")]
    if slots.LONG_SLOTS:   # ginfowell has no long video
        assert slots.due(three, at("16:41"), "schedule", "41 16 * * 3")[0] is True
    if slots.LONG_SLOTS:   # ginfowell has no long video
        assert slots.due(long_rows, at("16:41"), "schedule", "41 16 * * 3")[0] is False
    assert slots.due(long_rows, at("13:07"), "schedule", "7 13 * * *")[0] is True


def test_old_rows_without_kind_are_shorts_and_days_are_utc_claim_days():
    rows = short("old", kind=None)
    assert slots.due(rows, at("13:50"), "schedule", "49 13 * * *")[0] is False
    late = short("y", ts="2026-11-03T23:50:00+00:00")          # yesterday's claim: today is still open
    assert slots.due(late, at("13:07"), "schedule", "7 13 * * *")[0] is True


def test_unknown_schedule_never_posts():
    assert slots.due([], at("13:07"), "schedule", "1 2 * * *")[0] is False


def test_cli_reads_the_given_ledger_and_writes_the_output(tmp_path, monkeypatch):
    led = tmp_path / "ledger.jsonl"
    led.write_text("", encoding="utf-8")
    out = tmp_path / "out.txt"
    for k, v in {"GITHUB_OUTPUT": str(out), "BOT_LEDGER": str(led), "GITHUB_EVENT_NAME": "schedule",
                 "SCHEDULE": "7 13 * * *"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert slots.main(["gate"]) == 0
    assert out.read_text(encoding="utf-8").splitlines()[0] == "run=true"
    led.write_text("not json\n", encoding="utf-8")
    assert slots.main(["gate"]) == 3            # an unreadable ledger stops the run
    assert slots.main([]) == 2


def test_workflow_crons_match_the_slots_and_avoid_busy_minutes():
    wf = WF.read_text(encoding="utf-8")
    crons = re.findall(r'cron: "([^"]+)"', wf)
    heads = {" ".join(c.split()[:2]) for c in crons}
    assert heads == set(slots.SHORT_SLOTS) | set(slots.EXTRA_SLOTS) | set(slots.LONG_SLOTS)
    for c in crons:
        assert c.split()[0] not in ("0", "30")
        kind, _ = slots.slot_of(c)
        if kind == "long":
            assert c.split()[4] in "0123456"           # one line per weekday (the LONG_DAYS gate needs it)
    for head in slots.LONG_SLOTS:
        assert sum(1 for c in crons if c.startswith(head + " ")) == 7
    # the gate is the first step after checkout and every later make step waits for it
    make = wf[wf.index("\n  make:"):wf.index("\n  claim:")]
    assert "python3 -m bot.slots gate" in make
    after = make[make.index("id: gate"):]
    steps = after.split("\n      - ")[1:]
    assert steps and all("steps.gate.outputs.run == 'true'" in st for st in steps)


# ---- late crons (GitHub started the 21:13 Short at 01:27 the next day, run 41948615252)
NOV5 = "2026-11-05"


def at5(hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime(2026, 11, 5, h, m, tzinfo=timezone.utc)


def test_on_time_run_counts_today():
    assert slots.slot_day(at5("21:14"), "schedule", "13 21 * * *").isoformat() == NOV5
    rows = [r for i in range(2) for r in short(f"s{i}", ts=f"{NOV5}T13:10:00+00:00")]
    run, why = slots.due(rows, at5("21:14"), "schedule", "13 21 * * *")
    assert run is True and "today" in why


def test_late_run_after_midnight_fills_yesterdays_empty_slot():
    # Nov 4 had two Shorts; slot 3's cron starts at 01:27 on Nov 5: it is Nov 4's slot 3, still open
    rows = [r for i in range(2) for r in short(f"s{i}", ts=f"{DAY}T13:10:00+00:00")]
    assert slots.slot_day(at5("01:27"), "schedule", "13 21 * * *").isoformat() == DAY
    run, why = slots.due(rows, at5("01:27"), "schedule", "13 21 * * *")
    assert run is True and DAY in why
    # and Nov 5's own slot 1 is not used up by it once its claim records slot_day
    late = short("late", ts=f"{NOV5}T01:27:30+00:00")
    late[0]["slot_day"] = DAY
    assert slots.due(rows + late, at5("13:07"), "schedule", "7 13 * * *")[0] is True


def test_late_run_after_midnight_skips_when_yesterdays_slot_is_filled():
    rows = [r for i in range(3) for r in short(f"s{i}", ts=f"{DAY}T13:10:00+00:00")]
    run, why = slots.due(rows, at5("01:27"), "schedule", "13 21 * * *")
    assert run is False and DAY in why
    # a late long-video backup after midnight checks yesterday's long video too
    long_rows = short("L", kind="long", ts=f"{DAY}T15:30:00+00:00")
    if slots.LONG_SLOTS:   # ginfowell has no long video
        assert slots.due(long_rows, at5("00:20"), "schedule", "41 16 * * 3")[0] is False


def test_manual_dispatch_is_always_today_and_always_runs():
    assert slots.slot_day(at5("01:27"), "workflow_dispatch", "13 21 * * *").isoformat() == NOV5
    full = [r for i in range(3) for r in short(f"s{i}", ts=f"{NOV5}T00:10:00+00:00")]
    assert slots.due(full, at5("01:27"), "workflow_dispatch")[0] is True


def test_gate_writes_the_slot_day_and_the_claim_records_it(tmp_path, monkeypatch):
    led = tmp_path / "ledger.jsonl"
    led.write_text("", encoding="utf-8")
    out = tmp_path / "out.txt"
    for k, v in {"GITHUB_OUTPUT": str(out), "BOT_LEDGER": str(led), "GITHUB_EVENT_NAME": "schedule",
                 "SCHEDULE": "13 21 * * *"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(slots, "utcnow", lambda: at5("01:27"))
    assert slots.main(["gate"]) == 0
    assert out.read_text(encoding="utf-8").split() == ["run=true", f"slot_day={DAY}", "extra=false"]
    # old rows without slot_day still count on their claim's UTC day; a garbage slot_day falls back too
    bad = short("b", ts=f"{NOV5}T02:00:00+00:00")
    bad[0]["slot_day"] = "yesterday"
    assert slots.posted_on(bad, datetime(2026, 11, 5).date()) == ["b"]


def test_slot_day_boundaries_and_malformed_schedules():
    assert slots.slot_day(at5("23:59"), "schedule", "13 21 * * *").isoformat() == NOV5     # late but same day
    assert slots.slot_day(at5("00:00"), "schedule", "13 21 * * *").isoformat() == DAY      # just past midnight
    assert slots.slot_day(at5("21:13"), "schedule", "13 21 * * *").isoformat() == NOV5     # on time
    for bad in ("", "x", "13 xx * * *"):
        assert slots.slot_day(at5("01:00"), "schedule", bad).isoformat() == NOV5
    assert slots.slot_day(datetime(2026, 11, 5, 1, 27), "schedule", "13 21 * * *").isoformat() == DAY  # naive = UTC
    # a Wednesday long backup that starts after midnight is Wednesday's (the job-level LONG_DAYS gate reads the
    # cron's weekday, the slot day reads the cron's hour: both say Wednesday)
    if slots.LONG_SLOTS:   # ginfowell has no long video
        assert slots.slot_day(at5("00:30"), "schedule", "41 16 * * 3").isoformat() == DAY


def test_a_late_claim_counts_only_on_its_slot_day():
    late = short("late", ts=f"{NOV5}T01:27:30+00:00")
    late[0]["slot_day"] = DAY
    assert slots.posted_on(late, datetime(2026, 11, 4).date()) == ["late"]
    assert slots.posted_on(late, datetime(2026, 11, 5).date()) == []


# ---- extra Shorts (owner 2026-11-06: 3-5 a day, only while the month's runner minutes fit)
def extra(ep, ts=f"{DAY}T10:20:00+00:00"):
    rows = short(ep, ts=ts)
    rows[0]["extra"] = True
    return rows


def base(n, ts=f"{DAY}T13:10:00+00:00"):
    return [r for i in range(n) for r in short(f"b{i}", ts=ts)]


def test_extra_slots_need_the_budget_and_cap_at_two_a_day():
    d = slots.decide([], at("10:19"), "schedule", "19 10 * * *")
    assert d.run and d.extra                                   # extra 1: allowed by the ledger, budget decides
    assert slots.decide(extra("e1"), at("10:50"), "schedule", "19 10 * * *").run is False
    d = slots.decide(base(3) + extra("e1"), at("23:29"), "schedule", "29 23 * * *")
    assert d.run and d.extra                                   # extra 2 on a full base day
    two = extra("e1") + extra("e2", ts=f"{DAY}T23:30:00+00:00")
    assert slots.decide(base(3) + two, at("23:40"), "schedule", "29 23 * * *").run is False


def test_extras_never_use_up_a_base_slot():
    # the 10:19 extra went up first: slot 1 still posts its base Short, and so do slots 2 and 3
    rows = extra("e1")
    for cron, now in (("7 13 * * *", "13:07"), ("37 17 * * *", "17:37"), ("13 21 * * *", "21:13")):
        d = slots.decide(rows, at(now), "schedule", cron)
        assert d.run and not d.extra
        rows += short(f"base{now}", ts=f"{DAY}T{now}:30+00:00")
    assert slots.decide(rows, at("21:20"), "schedule", "13 21 * * *").run is False
    assert len(slots.posted_on(rows, at("22:00").date(), "short")) == 4


def test_the_late_extra_slot_catches_up_a_missing_base_short_without_the_budget():
    d = slots.decide(base(2), at("23:29"), "schedule", "29 23 * * *")
    assert d.run and not d.extra and "catching up" in d.why
    # the morning extra slot never stands in for a base Short
    assert slots.decide([], at("10:19"), "schedule", "19 10 * * *").extra is True


def test_a_late_extra_cron_after_midnight_counts_on_its_own_day():
    rows = base(3) + extra("e1") + extra("e2", ts=f"{DAY}T22:00:00+00:00")
    d = slots.decide(rows, at5("02:10"), "schedule", "29 23 * * *")
    assert d.run is False and DAY in d.why                    # Nov 4 is full: no 6th Short, Nov 5 untouched
    d = slots.decide(base(3) + extra("e1"), at5("02:10"), "schedule", "29 23 * * *")
    assert d.run and d.extra and DAY in d.why
    assert slots.slot_day(at5("02:10"), "schedule", "29 23 * * *").isoformat() == DAY


def test_quota_room_keeps_the_base_videos_of_the_day():
    # 1,650 units a video, 10,000 a Pacific day: 3 base Shorts + the long video + 2 extras = 9,900 fits
    now = at("10:19")
    ok, why = slots.quota_room([], now, now.date())
    assert ok, why
    # a failed upload earlier in the Pacific day already spent 1,650 x 3: the extra would starve a base video
    spent = [r for i in range(3) for r in short(f"f{i}", "failed", ts=f"{DAY}T09:00:00+00:00")]
    ok, why = slots.quota_room(spent, now, now.date())
    assert not ok and "base video" in why


def _gate(monkeypatch, tmp_path, cron, now, rows=()):
    import json
    led = tmp_path / "ledger.jsonl"
    led.write_text("".join(json.dumps(r) + chr(10) for r in rows), encoding="utf-8")
    out = tmp_path / "out.txt"
    out.write_text("", encoding="utf-8")
    for k, v in {"GITHUB_OUTPUT": str(out), "BOT_LEDGER": str(led), "GITHUB_EVENT_NAME": "schedule",
                 "SCHEDULE": cron, "REPO": "o/r", "GH_TOKEN": "t", "GITHUB_RUN_ID": "7"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(slots, "utcnow", lambda: now)
    assert slots.main(["gate"]) == 0
    return dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").split())


def test_gate_posts_an_extra_only_when_the_budget_fits(monkeypatch, tmp_path):
    seen = {}

    def used(repo, token, now, skip_run=None):
        seen["args"] = (repo, token, skip_run)
        return 300
    monkeypatch.setattr(slots.budget, "month_used", used)
    assert _gate(monkeypatch, tmp_path, "19 10 * * *", at("10:19")) == {"run": "true", "slot_day": DAY,
                                                                          "extra": "true"}
    assert seen["args"] == ("o/r", "t", 7)                      # this run's own minutes are not counted twice
    monkeypatch.setattr(slots.budget, "month_used", lambda *a, **k: 1700)
    assert _gate(monkeypatch, tmp_path, "19 10 * * *", at("10:19"))["run"] == "false"


def test_gate_skips_the_extra_when_the_budget_cannot_be_read(monkeypatch, tmp_path, capsys):
    def broken(*a, **k):
        raise slots.budget.BudgetError("GitHub API: HTTPError: 403")
    monkeypatch.setattr(slots.budget, "month_used", broken)
    assert _gate(monkeypatch, tmp_path, "19 10 * * *", at("10:19")) == {"run": "false", "slot_day": DAY,
                                                                          "extra": "false"}
    assert "fail safe" in capsys.readouterr().out
    # ... but never a base Short: it posts, and only says the budget is unknown
    assert _gate(monkeypatch, tmp_path, "7 13 * * *", at("13:07"))["run"] == "true"


def test_base_shorts_post_over_the_warning_line_and_say_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(slots.budget, "month_used", lambda *a, **k: 1990)
    assert _gate(monkeypatch, tmp_path, "7 13 * * *", at("13:07")) == {"run": "true", "slot_day": DAY,
                                                                         "extra": "false"}
    assert "::warning::runner minutes" in capsys.readouterr().out


@pytest.mark.parametrize("exc", [KeyError("id"), ValueError("bad date"), AttributeError("list"), RuntimeError("x")])
def test_any_budget_crash_skips_the_extra_and_never_the_base(monkeypatch, tmp_path, exc):
    def broken(*a, **k):
        raise exc
    monkeypatch.setattr(slots.budget, "month_used", broken)
    assert _gate(monkeypatch, tmp_path, "19 10 * * *", at("10:19"))["run"] == "false"
    assert _gate(monkeypatch, tmp_path, "7 13 * * *", at("13:07"))["run"] == "true"
    if slots.LONG_SLOTS:   # ginfowell has no long video
        assert _gate(monkeypatch, tmp_path, "23 15 * * 3", at("15:23"))["run"] == "true"


def test_the_budget_uses_the_long_days_variable(monkeypatch, tmp_path):
    seen = {}
    real = slots.budget.extra_allowed

    def spy(used, now, limit=slots.budget.EXTRA_LIMIT, base=slots.budget.BASE_DAY):
        seen["base"] = base
        return real(used, now, limit, base)
    monkeypatch.setattr(slots.budget, "month_used", lambda *a, **k: 0)
    monkeypatch.setattr(slots.budget, "extra_allowed", spy)
    monkeypatch.setenv("LONG_DAYS", "daily")
    _gate(monkeypatch, tmp_path, "19 10 * * *", at("10:19"))
    assert seen["base"] == slots.budget.base_day("daily")
    monkeypatch.setenv("LONG_DAYS", "")
    _gate(monkeypatch, tmp_path, "19 10 * * *", at("10:19"))
    assert seen["base"] == slots.budget.base_day("1,3,5")


def test_long_skip_days_parse_dates_and_ignore_junk():
    assert slots.long_skip_days("2026-11-08, 2026-11-15,,bad,2026-13-01") == {"2026-11-08", "2026-11-15"}
    assert slots.long_skip_days("") == set()


def test_a_skipped_day_has_no_long_video_but_keeps_its_shorts(monkeypatch):
    monkeypatch.setenv("LONG_SKIP", f"{DAY},2026-11-15")
    run, why = slots.due([], at("15:23"), "schedule", "23 15 * * 3")
    assert run is False   # ginfowell: no long slot at all, so any reason is right
    if slots.LONG_SLOTS:   # ginfowell has no long video
        assert "LONG_SKIP" in why
        assert slots.due([], at("16:41"), "schedule", "41 16 * * 3")[0] is False      # the backup too
    assert slots.due([], at("13:07"), "schedule", "7 13 * * *")[0] is True        # Shorts still post
    monkeypatch.setenv("LONG_SKIP", "2026-11-15")
    if slots.LONG_SLOTS:   # ginfowell has no long video
        assert slots.due([], at("15:23"), "schedule", "23 15 * * 3")[0] is True       # other days post
    assert slots.due([], at("15:23"), "workflow_dispatch")[0] is True              # manual runs always run


# ---- from FOUR_FROM (2026-12-01): 4 base Shorts a day (10:19, 13:07, 17:37, 21:13) + one budget-checked extra
DEC = "2026-12-02"


def at_dec(hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime(2026, 12, 2, h, m, tzinfo=timezone.utc)


def dec_base(n):
    return [r for i in range(n) for r in short(f"d{i}", ts=f"{DEC}T10:20:00+00:00")]


def test_four_base_shorts_a_day_from_december():
    d = slots.decide([], at_dec("10:19"), "schedule", "19 10 * * *")
    assert d.run and not d.extra                                      # 10:19 is base slot 1, no budget check
    assert slots.decide(dec_base(1), at_dec("10:40"), "schedule", "19 10 * * *").run is False
    for cron, now, n in (("7 13 * * *", "13:07", 1), ("37 17 * * *", "17:37", 2), ("13 21 * * *", "21:13", 3)):
        d = slots.decide(dec_base(n), at_dec(now), "schedule", cron)
        assert d.run and not d.extra
        assert slots.decide(dec_base(n + 1), at_dec(now), "schedule", cron).run is False
    d = slots.decide(dec_base(3), at_dec("23:29"), "schedule", "29 23 * * *")
    assert d.run and not d.extra and "catching up" in d.why           # 23:29 backs up base slot 4
    d = slots.decide(dec_base(4), at_dec("23:29"), "schedule", "29 23 * * *")
    assert d.run and d.extra                                          # the one extra, budget permitting
    one = dec_base(4) + extra("x1", ts=f"{DEC}T23:30:00+00:00")
    assert slots.decide(one, at_dec("23:40"), "schedule", "29 23 * * *").run is False


def test_november_keeps_three_base_shorts_and_two_extras():
    assert slots.shorts_per_day(at("10:00").date()) == 3 and slots.extras_per_day(at("10:00").date()) == 2
    assert slots.decide([], at("10:19"), "schedule", "19 10 * * *").extra is True


def test_budget_base_day_counts_four_shorts_from_december():
    assert slots.budget.base_day("daily", base_shorts=4, extra_slots=1) == slots.budget.base_day("daily") + 8 - 1
