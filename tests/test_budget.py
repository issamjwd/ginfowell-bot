"""The extra Shorts' runner-minute budget (bot/budget.py). Fixtures shaped like the GitHub REST API; no network."""
from datetime import datetime, timezone

import pytest

from bot import budget

NOW = datetime(2026, 11, 6, 17, 0, tzinfo=timezone.utc)


def run(rid, start, end, conclusion="success", status="completed", attempt=1, created=None):
    return {"id": rid, "status": status, "conclusion": conclusion, "run_attempt": attempt,
            "created_at": created or start, "run_started_at": start, "updated_at": end}


def job(start, end, conclusion="success", status="completed"):
    return {"status": status, "conclusion": conclusion, "started_at": start, "completed_at": end}


# a live Short (measured run 42144416944: make 3:45, claim 0:09, upload 0:15, record 0:10 -> 4 + 1 + 1 + 1)
LIVE_SHORT = [job("2026-11-06T01:31:39Z", "2026-11-06T01:35:24Z"), job("2026-11-06T01:35:27Z", "2026-11-06T01:35:36Z"),
              job("2026-11-06T01:35:40Z", "2026-11-06T01:35:55Z"), job("2026-11-06T01:35:58Z", "2026-11-06T01:36:08Z")]
# a no-op backup whose run sat 1.5 min in the queue (run 42105170953): make 6 s, the rest skipped
NOOP = [job("2026-11-05T20:39:25Z", "2026-11-05T20:39:31Z"),
        *[job("2026-11-05T20:39:32Z", "2026-11-05T20:39:31Z", "skipped") for _ in range(3)]]


@pytest.fixture
def month():
    runs = [
        run(1, "2026-11-06T01:31:35Z", "2026-11-06T01:36:09Z"),                     # live Short: 7
        run(2, "2026-11-05T20:37:53Z", "2026-11-05T20:39:32Z"),                     # queued no-op: jobs fetched, 1
        run(3, "2026-11-05T21:29:21Z", "2026-11-05T21:29:32Z"),                     # 11 s no-op: 1, no jobs call
        run(4, "2026-11-05T21:00:00Z", "2026-11-05T21:00:02Z", conclusion="skipped"),  # LONG_DAYS skip: 0
        run(5, "2026-10-31T23:58:00Z", "2026-11-01T00:20:00Z"),                     # October's: not counted
        run(6, "2026-11-06T16:50:00Z", "2026-11-06T16:50:00Z", conclusion=None, status="in_progress"),  # running
        run(99, "2026-11-06T16:59:00Z", "2026-11-06T16:59:00Z", conclusion=None, status="in_progress"),  # this run
    ]
    jobs = {1: LIVE_SHORT, 2: NOOP, 6: [job("2026-11-06T16:50:10Z", None, None, "in_progress")]}
    return runs, jobs


def test_usage_sums_each_job_rounded_up_and_fetches_only_what_it_must(month):
    runs, jobs = month
    asked = []

    def jobs_of(r):
        asked.append(r["id"])
        return jobs[r["id"]]
    # 7 (live Short) + 1 (queued no-op) + 1 (short no-op) + 0 (skipped) + 10 (a make job running for 9:50)
    assert budget.usage(runs, jobs_of, NOW, skip_run=99) == 7 + 1 + 1 + 0 + 10
    assert sorted(asked) == [1, 2, 6]


def test_job_minutes_edges():
    assert budget.job_minutes(job("2026-11-06T10:00:00Z", "2026-11-06T10:00:01Z"), NOW) == 1
    assert budget.job_minutes(job("2026-11-06T10:00:00Z", "2026-11-06T10:01:00Z"), NOW) == 1
    assert budget.job_minutes(job("2026-11-06T10:00:00Z", "2026-11-06T10:01:01Z"), NOW) == 2
    assert budget.job_minutes(job("2026-11-06T10:00:00Z", "2026-11-06T09:59:59Z", "skipped"), NOW) == 0
    assert budget.job_minutes(job(None, None, None, "queued"), NOW) == 0
    assert budget.job_minutes({"status": "completed", "conclusion": "success", "started_at": "2026-11-06T10:00:00",
                               "completed_at": "2026-11-06T10:00:30"}, NOW) == 1         # naive = UTC


def test_a_rerun_is_never_taken_for_a_one_job_run():
    r = run(7, "2026-11-06T10:00:00Z", "2026-11-06T10:00:40Z", attempt=2)
    assert not budget.is_short_run(r)
    assert budget.usage([r], lambda _r: [job("2026-11-06T09:00:00Z", "2026-11-06T09:04:10Z"),
                                         job("2026-11-06T10:00:00Z", "2026-11-06T10:00:30Z")], NOW) == 5 + 1


def test_days_left_and_projection():
    assert budget.days_left(datetime(2026, 11, 30, 12, tzinfo=timezone.utc)) == pytest.approx(0.5)
    assert budget.days_left(datetime(2026, 12, 31, 0, tzinfo=timezone.utc)) == pytest.approx(1.0)  # December rolls
    assert budget.days_left(datetime(2026, 11, 1, tzinfo=timezone.utc)) == pytest.approx(30.0)
    assert budget.projected(100, datetime(2026, 11, 30, 12, tzinfo=timezone.utc), 8, base_day=40) == 100 + 8 + 20


def test_base_day_follows_long_days():
    # measured costs: long 9 + post 3 + backup 1; 3 x (Short 5 + post 3); 2 Short backups; 2 extra slots; CI 2
    assert budget.base_day("daily") == 43
    assert budget.base_day("1,3,5") == pytest.approx(43 - 4 / 7 * 13)
    assert budget.base_day("") == budget.base_day("1,3,5") == budget.base_day("junk")
    assert budget.base_day("0,1,2,3,4,5,6") == 43


def test_extra_allowed_on_the_measured_numbers():
    # 1 Nov, nothing used: 30 x 43 + 8 = 1,298 fits under 1,850
    ok, why = budget.extra_allowed(0, datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc))
    assert ok and "projects to 1298" in why
    # 6 Nov 17:00 with the 974 minutes really used by then: 974 + 8 + 24.29 x 43 = 2,027, no extra
    ok, why = budget.extra_allowed(974, NOW)
    assert not ok and "projects to 2027" in why and "would not fit" in why
    # 28 Nov noon, 1,600 used: 1,600 + 8 + 2.5 x 43 = 1,715.5 fits; 1,750 used: 1,865.5 does not
    late = datetime(2026, 11, 28, 12, tzinfo=timezone.utc)
    assert budget.extra_allowed(1600, late)[0] is True
    assert budget.extra_allowed(1750, late)[0] is False


def test_base_warning_only_past_the_warn_line():
    late = datetime(2026, 11, 30, 18, tzinfo=timezone.utc)   # a quarter day left: + 10.75
    assert budget.base_warning(1930, late) is None            # 1,940.75
    msg = budget.base_warning(1940, late)                     # 1,950.75
    assert msg and "1950" in msg and "LONG_DAYS" in msg


def test_a_one_minute_run_is_taken_as_one_job():
    # documents the shortcut: a completed first attempt that took <= 60 s bills 1 minute without a jobs call
    r = run(8, "2026-11-06T10:00:00Z", "2026-11-06T10:01:00Z")
    assert budget.is_short_run(r)
    assert budget.usage([r], lambda _r: pytest.fail("no jobs call for a one-minute run"), NOW) == 1


def test_month_used_pages_and_refuses_bad_input(monkeypatch):
    calls = []

    def fake_get(url, token, deadline=None):
        calls.append(url)
        if "/jobs" in url:
            return {"jobs": LIVE_SHORT}
        page = int(url.rsplit("page=", 1)[1])
        n = 100 if page == 1 else 3
        batch = [run(page * 1000 + i, "2026-11-02T10:00:00Z", "2026-11-02T10:06:00Z") for i in range(n)]
        return {"total_count": 103, "workflow_runs": batch}
    monkeypatch.setattr(budget, "_get", fake_get)
    assert budget.month_used("o/r", "t", NOW) == 103 * 7
    assert sum("/actions/runs?" in c for c in calls) == 2 and "created=%3E%3D2026-11-01" in calls[0]
    for repo, token in (("", "t"), ("o/r", ""), ("bad", "t")):
        with pytest.raises(budget.BudgetError):
            budget.month_used(repo, token, NOW)


def test_api_errors_become_budget_errors(monkeypatch):
    def boom(*a, **k):
        raise budget.urllib.error.URLError("down")
    monkeypatch.setattr(budget.urllib.request, "urlopen", boom)
    with pytest.raises(budget.BudgetError):
        budget.month_used("o/r", "t", NOW)
    with pytest.raises(budget.BudgetError):
        budget._get("https://example.com/x", "t")             # never sends the token anywhere but api.github.com
    monkeypatch.setattr(budget, "_get", lambda url, token, deadline=None: {"message": "Bad credentials"})
    with pytest.raises(budget.BudgetError):
        budget.month_used("o/r", "t", NOW)


@pytest.mark.parametrize("payload", [
    {"total_count": 1, "workflow_runs": [{"status": "completed"}]},                        # no id -> KeyError
    {"total_count": 1, "workflow_runs": [{"id": 1, "status": "completed", "run_started_at": "yesterday"}]},
    {"total_count": "many", "workflow_runs": [run(1, "2026-11-02T10:00:00Z", "2026-11-02T10:00:10Z")] * 100},
    {"total_count": 1, "workflow_runs": ["not a run"]},                                     # AttributeError
])
def test_odd_payloads_are_budget_errors_never_crashes(monkeypatch, payload):
    monkeypatch.setattr(budget, "_get", lambda url, token, deadline=None: payload if "/jobs" not in url
                        else {"jobs": [{"status": "completed", "started_at": "garbage"}]})
    with pytest.raises(budget.BudgetError):
        budget.month_used("o/r", "t", NOW)


def test_the_sweep_has_a_deadline(monkeypatch):
    monkeypatch.setattr(budget, "DEADLINE", -1)
    monkeypatch.setattr(budget.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no request after the deadline"))
    with pytest.raises(budget.BudgetError, match="out of time"):
        budget.month_used("o/r", "t", NOW)
