import json
from datetime import date, datetime, timezone

import pytest

from bot.state import CLAIMED, UPLOADED, Ledger, StateError, fingerprint, pacific_date, scrub


def at(iso: str):
    """A fixed clock, so quota tests do not depend on today's date."""
    t = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    return lambda: t


def test_claim_once_only(tmp_path):
    led = Ledger(tmp_path / "s.jsonl")
    led.claim("ep-20261103-aaaa", {"fingerprint": "f1", "template": "dont_stare"})
    with pytest.raises(StateError, match="already claimed"):
        led.claim("ep-20261103-aaaa", {"fingerprint": "f1"})


def test_rows_round_trip_and_latest(tmp_path):
    led = Ledger(tmp_path / "s.jsonl")
    led.claim("ep-1aaa", {"fingerprint": "f1", "dream_ids": ["halls"], "template": "t"})
    led.append({"episode_id": "ep-1aaa", "status": UPLOADED, "video_id": "abc"})
    assert led.latest()["ep-1aaa"]["status"] == UPLOADED
    h = led.history()
    assert h[0]["dream_ids"] == ["halls"] and h[0]["video_id"] == "abc"
    assert led.used_fingerprints() == {"f1"}
    assert led.unresolved() == []


def test_unclear_and_claimed_are_unresolved(tmp_path):
    led = Ledger(tmp_path / "s.jsonl")
    led.claim("ep-2aaa", {})
    assert [r["episode_id"] for r in led.unresolved()] == ["ep-2aaa"]


def test_quota_budget_refuses_within_one_pacific_day(tmp_path):
    p = tmp_path / "s.jsonl"
    Ledger(p, clock=at("2030-06-10T08:00:00")).claim("ep-3aaa", {}, budget=3500)   # 01:00 PDT, June 10
    Ledger(p, clock=at("2030-06-11T06:30:00")).claim("ep-3bbb", {}, budget=3500)   # 23:30 PDT, still June 10
    with pytest.raises(StateError, match="quota"):
        Ledger(p, clock=at("2030-06-11T06:50:00")).claim("ep-3ccc", {}, budget=3500)
    Ledger(p, clock=at("2030-06-11T07:10:00")).claim("ep-3ddd", {}, budget=3500)   # 00:10 PDT, June 11: new day


def test_pacific_date_handles_both_offsets():
    assert pacific_date(datetime(2026, 1, 15, 7, 59, tzinfo=timezone.utc)) == date(2026, 1, 14)   # PST -8
    assert pacific_date(datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)) == date(2026, 1, 15)
    assert pacific_date(datetime(2026, 7, 15, 6, 59, tzinfo=timezone.utc)) == date(2026, 7, 14)   # PDT -7
    assert pacific_date(datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc)) == date(2026, 7, 15)
    assert pacific_date(datetime(2026, 3, 8, 9, 59, tzinfo=timezone.utc)) == date(2026, 3, 8)     # DST from 10:00Z
    assert pacific_date(datetime(2026, 11, 1, 8, 59, tzinfo=timezone.utc)) == date(2026, 11, 1)   # PDT until 09:00Z


def test_notes_are_scrubbed_and_short(tmp_path):
    led = Ledger(tmp_path / "s.jsonl")
    note = "PUT https://www.googleapis.com/upload/youtube/v3/videos?upload_id=SECRET1 token=abc " + "x" * 500
    row = led.append({"episode_id": "ep-5aaa", "status": "failed", "note": note})
    assert "SECRET1" not in row["note"] and "token=abc" not in row["note"] and len(row["note"]) <= 200
    assert "upload_id" not in scrub("see https://h/x?upload_id=1&a=2")


def test_corrupt_ledger_stops_the_run(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"episode_id": "ep-x", "status": "claimed"}\nnot json\n', encoding="utf-8")
    with pytest.raises(StateError, match="not valid JSON"):
        Ledger(p).rows()
    p.write_text(json.dumps({"episode_id": "ep-x", "status": "maybe"}) + "\n", encoding="utf-8")
    with pytest.raises(StateError):
        Ledger(p).rows()


def test_bad_status_is_never_written(tmp_path):
    with pytest.raises(StateError):
        Ledger(tmp_path / "s.jsonl").append({"episode_id": "ep-x", "status": "done"})


def test_fingerprint_is_order_stable():
    a = fingerprint("t", ["halls"], None, {"hook": "a", "turn": "b"})
    b = fingerprint("t", ["halls"], None, {"turn": "b", "hook": "a"})
    assert a == b and a != fingerprint("t", ["hotel"], None, {"hook": "a", "turn": "b"})


def test_rows_are_lf_jsonl(tmp_path):
    led = Ledger(tmp_path / "s.jsonl")
    led.append({"episode_id": "ep-4aaa", "status": CLAIMED})
    raw = (tmp_path / "s.jsonl").read_bytes()
    assert raw.endswith(b"\n") and b"\r\n" not in raw
