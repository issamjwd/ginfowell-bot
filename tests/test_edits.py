"""The Shorts' edit styles (owner feedback 2026-11-04): pace, cold open, reading speed, end tag, no stills, variety."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from bot import bank, captions, edits, footage, look, overlay, planner, storyboard
from tests.conftest import FAKE_CLIPS


@pytest.fixture(scope="module")
def shorts():
    history, used, out = [], set(), []
    t0 = datetime(2026, 11, 5, 13, 7, tzinfo=timezone.utc)
    for i in range(24):
        now = t0 + timedelta(hours=8 * i)
        _, sb = planner.plan(history, used, "auto", FAKE_CLIPS, seed=500 + i, now=now)
        used.add(sb["fingerprint"])
        history.append({**{k: sb[k] for k in ("template", "format", "dream_ids", "lines", "fingerprint")},
                        "music_seed": sb["music"]["seed"], "bpm": sb["music"]["bpm"], "ts": now.isoformat()})
        out.append(sb)
    return out


def test_every_short_is_moving_footage_only(shorts):
    for sb in shorts:
        assert sb["format"] == "footage" and sb["template"] in edits.STYLES
        for s in sb["shots"]:
            assert s["kind"] == "clip"                                        # never a still / drawing as a shot
            z0, z1 = s["zoom"]
            assert abs(z1 - z0) >= 0.05 or abs(s["pan"]) > 0                  # a camera move on every shot


def test_pace_cut_every_one_to_two_and_a_half_seconds_and_no_clip_twice(shorts):
    for sb in shorts:
        secs = [s["seconds"] for s in sb["shots"]]
        assert max(secs) <= edits.MAX_SHOT + 1e-6 and min(secs) >= 0.9
        clips = [s["clip"] for s in sb["shots"]]
        assert len(clips) == len(set(clips))
        assert 8 <= storyboard.total_seconds(sb) <= 16


def test_cold_open_is_the_scare_clip_with_a_hit_and_a_big_hook(shorts):
    scary = set(edits.scary_clips(FAKE_CLIPS))
    for sb in shorts:
        first, last = sb["shots"][0], sb["shots"][-1]
        assert first["clip"] in scary and "impact" in first["fx"]
        assert "impact" in last["fx"]                                          # the turn: hit again
        assert sb["hook"]["title"] and 0 < sb["hook"]["seconds"] <= 3.0
        p = overlay.props_from_storyboard(sb)
        assert p["hook"]["wordFrames"][0] <= 2                               # the hook is up from the first frames
        assert any(q["kind"] == "impact" and q["f"] == 0 for q in p["cues"])  # a hard hit on frame 0


def test_reading_speed_and_fewer_words(shorts):
    for sb in shorts:
        track = sb["captions"]
        assert captions.words_per_second(track) <= captions.WPS + 0.01      # (times are rounded to the millisecond)
        for c in track:
            assert c["end"] - c["start"] >= captions.MIN_HOLD
            assert len(c["text"].split()) <= captions.CHUNK_WORDS + 3   # ("rule 1:" label + an orphan word at most)
        spoken = [w for c in track for w in c["text"].split() if w != "rule" and not w.rstrip(":").isdigit()]
        words = len(sb["hook"]["title"].split()) + len(spoken)            # ("rule 1:" labels are not lines)
        assert words <= 20
        assert all(c["end"] <= storyboard.total_seconds(sb) + 1e-6 for c in track)


def test_no_dead_end_short_end_tag_over_the_last_shot(shorts):
    for sb in shorts:
        assert all(s["kind"] != "card" for s in sb["shots"])                  # no outro card, no black end card
        assert sb["end_tag"]["seconds"] <= 1.5
        p = overlay.props_from_storyboard(sb)
        assert p["endCard"]["style"] == "tag" and p["outro"] is None


def test_styles_rotate_and_differ(shorts):
    for a, b in zip(shorts, shorts[1:]):
        assert a["template"] != b["template"]
    assert {s["template"] for s in shorts} == set(edits.STYLES)
    feel = {(st["look"], st["caption"], st["bed"], st["beats"], st["trans"], st["bpm"]) for st in edits.STYLES.values()}
    assert len(feel) == len(edits.STYLES)                                     # every style is its own edit
    assert {sb["look"] for sb in shorts} == {"clean", "vhs"}


def test_no_speed_lines_anywhere(shorts):
    for sb in shorts:
        assert not any("speed_lines" in s["fx"] for s in sb["shots"])


def test_caption_chunks_and_drop_rather_than_rush():
    assert captions.chunks("something in this dream noticed you.") == ["something in this", "dream noticed you."]
    assert captions.chunks("rule 2: walk. never run.") == ["rule 2: walk.", "never run."]
    assert captions.chunks("it hears running.") == ["it hears running."]
    # a line that cannot be read in the time left is dropped whole, never sped up
    tr = captions.track([("one two three", 0.0, "rise"), ("four five six seven eight nine", 2.0, "rise")],
                        hook_end=0.0, end=3.5)
    assert [c["text"] for c in tr] == ["one two three"]


def test_footage_segments_skip_fades_and_never_overlap():
    prof = tuple([(0.2, 0.01)] * 4 + [(0.02, 0.5)] * 34)                  # a fade from black, then a calm clip
    footage.profile.cache_clear()
    real = footage.profile
    footage.profile = lambda name: prof
    try:
        s0 = footage.best_start("x", 1.5, 9.5)
        assert s0 >= 1.0                                                     # never the fade from black
        s1 = footage.best_start("x", 1.5, 9.5, avoid=[(s0, s0 + 1.5)])
        assert s1 + 1.5 <= s0 or s1 >= s0 + 1.5
    finally:
        footage.profile = real


def test_new_grade_is_darker_and_harder_than_the_old_haze():
    rng = np.random.default_rng(3)
    f = rng.integers(20, 230, (90, 160, 3), dtype=np.uint8)
    g = look.Grade(160, 90, 1)(f, 0).astype(float)
    lo = f.astype(float) < 50
    assert g[lo].mean() < f.astype(float)[lo].mean() + 4                   # shadows are not lifted into haze
    assert g.std() >= f.astype(float).std() * 0.9                          # contrast kept, not flattened
    v = look.vhs(f, 3)
    assert v.shape == f.shape and np.abs(v.astype(int) - f).mean() > 2


def test_found_footage_lines_and_dreams_are_valid(shorts):
    for sb in shorts:
        storyboard.validate(sb, footage=set(FAKE_CLIPS))
        for d in sb["dream_ids"]:
            assert bank.dream(d)


def test_no_particle_burst_over_the_cold_open_subject(shorts):
    for sb in shorts:
        cues = overlay.props_from_storyboard(sb)["cues"]
        cold = [q for q in cues if q["kind"] == "impact" and q["f"] == 0]
        assert cold and all(q.get("burst") is False for q in cold)          # thump and shake, no shards on the face
        assert any(q["kind"] == "impact" and q["f"] > 24 and q.get("burst", True) for q in cues)  # the turn bursts


def test_validator_checks_the_new_music_fields(shorts):
    import copy
    sb = copy.deepcopy(shorts[0])
    sb["music"]["bed"] = "disco"
    with pytest.raises(storyboard.StoryboardError, match="bed"):
        storyboard.validate(sb)
    sb = copy.deepcopy(shorts[0])
    sb["music"]["drop_at"], sb["music"]["resume_at"] = 5.0, 4.0
    with pytest.raises(storyboard.StoryboardError, match="drop_at"):
        storyboard.validate(sb)


def test_burned_fallback_draws_the_hook_on_the_cold_open(shorts, tmp_path, monkeypatch):
    from bot.render import Renderer
    from tests.conftest import tiny_clip
    sb = shorts[0]
    for c in {s["clip"] for s in sb["shots"]}:
        tiny_clip(tmp_path / f"{c}.mp4", seconds=10, w=240, h=270, audio=False)
    monkeypatch.setenv("BOT_FOOTAGE_DIR", str(tmp_path))
    burned, clean = Renderer(sb, 216, 384, 24, text=True), Renderer(sb, 216, 384, 24, text=False)
    a, b = burned.frame(12).astype(int), clean.frame(12).astype(int)
    burned.close()
    clean.close()
    band = slice(int(384 * 0.08), int(384 * 0.26))                           # the hook's upper band
    assert np.abs(a[band] - b[band]).mean() > 3


def test_cold_open_and_turn_start_at_the_scare_peak(shorts):
    tagged = footage.tags()
    for sb in shorts:
        for s in (sb["shots"][0], sb["shots"][-1]):
            peak = tagged.get(s["clip"], {}).get("peak")
            if peak is not None:
                assert s["from"] <= peak <= s["from"] + s["seconds"] + 0.7, (s["clip"], s["from"], peak)


def test_cold_open_is_an_approved_big_monster_starting_just_before_its_peak(shorts):
    ok = set(footage.cold_open_ok())
    tagged = footage.tags()
    assert ok and "halls_firstscare_01" not in ok                     # flicker / hazy openers are out
    for sb in shorts:
        first = sb["shots"][0]
        assert first["clip"] in ok
        peak = tagged[first["clip"]]["peak"]
        assert abs(first["from"] - max(0.0, peak - edits.COLD_LEAD)) < 0.02 or first["from"] < peak


def test_slow_dread_never_goes_two_seconds_without_a_monster():
    tagged = footage.tags()
    history, used, seen = [], set(), 0
    for i in range(60):
        _, sb = planner.plan(history, used, "auto", FAKE_CLIPS, seed=900 + i)
        used.add(sb["fingerprint"])
        history.append({k: sb[k] for k in ("template", "format", "dream_ids", "lines", "fingerprint")})
        if sb["template"] != "slow_dread":
            continue
        seen += 1
        empty_run = 0.0
        for s in sb["shots"]:
            if "monster" in tagged.get(s["clip"], {}).get("tags", []):
                empty_run = 0.0
            else:
                empty_run += s["seconds"]
                assert empty_run <= edits.STYLES["slow_dread"]["monster_gap"] + 1e-6, sb["clips"]
    assert seen >= 3
