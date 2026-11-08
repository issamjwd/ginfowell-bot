"""Edit v4: per-style grade, motion (camera, ramps, transitions), sound cues and synthesis, kinetic hooks."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from bot import edits, grade, longform, motion, overlay, planner, rig, sfx, storyboard
from bot.music import SR
from tests.conftest import FAKE_CLIPS, ffmpeg

NOW = datetime(2026, 11, 5, 13, 7, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def shorts():
    history, used, out = [], set(), []
    for i in range(18):
        now = NOW + timedelta(hours=8 * i)
        _, sb = planner.plan(history, used, "auto", FAKE_CLIPS, seed=900 + i, now=now)
        used.add(sb["fingerprint"])
        history.append({**{k: sb[k] for k in ("template", "format", "dream_ids", "lines", "fingerprint")},
                        "music_seed": sb["music"]["seed"], "bpm": sb["music"]["bpm"], "ts": now.isoformat()})
        out.append(sb)
    return out


# ------------------------------------------------------------------------------------------------ grade
@pytest.mark.parametrize("name", sorted(grade.LOOKS))
def test_every_look_is_one_filmic_chain(name):
    vf = grade.chain(name, 1080, 1920)
    assert vf.startswith("curves=r=") and "colorbalance" not in vf and vf.count("format=yuv420p") == 1
    lk = grade.LOOKS[name]
    if lk.halation:
        # the glow is screen-blended in RGB (on YUV it turns the frame magenta) at quarter size
        assert "format=gbrp,split" in vf and "blend=all_mode=screen" in vf and "scale=270:480" in vf
    if lk.vignette:
        assert "vignette=angle=" in vf
    assert ("noise=c0s=" in vf) == (lk.grain > 0)
    assert "out_color_matrix=bt709" in grade.chain(name, 1920, 1080, bt709=True)


def test_each_short_style_has_its_own_look():
    looks = [grade.LOOKS[s] for s in edits.STYLES]
    assert len({(lk.curve, lk.shadows, lk.highlights, lk.saturation) for lk in looks}) == len(looks)
    assert set(edits.STYLES) <= set(grade.LOOKS) and "long" in grade.LOOKS


def test_grade_chain_runs_in_ffmpeg(tmp_path):
    src = tmp_path / "in.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=108x192:rate=1:duration=1", "-frames:v", "1", str(src))
    for name in ("chase_cut", "found_footage"):
        ffmpeg("-i", str(src), "-vf", "format=rgb24," + grade.chain(name, 108, 192), "-frames:v", "1",
               str(tmp_path / f"{name}.png"))
        assert (tmp_path / f"{name}.png").stat().st_size > 1000


# ------------------------------------------------------------------------------------------------ motion maths
def test_speed_ramp_maps_shot_time_to_clip_time():
    ramp = [[1.0, 0.5], [1.4, 0.0], [1.7, 1.0]]
    assert motion.source_time(0.8, ramp) == pytest.approx(0.8)
    assert motion.source_time(1.2, ramp) == pytest.approx(1.1)              # slow motion: half speed
    assert motion.source_time(1.5, ramp) == motion.source_time(1.65, ramp)  # the freeze frame holds
    assert motion.source_time(2.0, ramp) == pytest.approx(1.2 + 0.3)        # snapped back to 1x
    assert motion.speed_at(1.5, ramp) == 0 and motion.speed_at(0.2, ramp) == 1.0
    assert motion.source_time(2.0, None, 0.5) == pytest.approx(1.0)


def test_punch_snaps_in_then_relaxes():
    assert motion.punch_env(0.49, [0.5]) == 0
    assert motion.punch_env(0.5 + motion.PUNCH_SNAP, [0.5]) > 0.99          # arrives in ~2 frames
    assert 0.2 < motion.punch_env(1.0, [0.5]) < 0.5                         # then lets go
    assert motion.punch_env(1.0, [0.5, 0.95]) == motion.punch_env(0.05, [0.0])  # strongest wins, never adds up


def test_handheld_is_smooth_and_bounded():
    xs = [motion.handheld(i / 24, 7, 10.0)[0] for i in range(240)]
    assert max(abs(x) for x in xs) <= 10.0 + 1e-9
    assert max(abs(a - b) for a, b in zip(xs, xs[1:])) < 2.5                 # drifts, never jumps frame to frame
    assert motion.handheld(1.0, 7, 0.0) == (0.0, 0.0, 0.0)


def test_camera_and_blurs_keep_the_frame_shape():
    f = (np.random.default_rng(1).random((96, 54, 3)) * 255).astype(np.uint8)
    for out in (motion.camera(f, 1.2, 3, -2), motion.directional_blur(f, 20), motion.radial_blur(f, 0.2),
                motion.light_leak(f, 0.9, 0.3), motion.glitch(f, f, 1.0, 3), motion.freeze_look(f, 0.8)):
        assert out.shape == f.shape and out.dtype == np.uint8
    assert motion.camera(f, 1.0) is f
    assert motion.directional_blur(f, 20).std() < f.std()                   # the blur really smears


class _Frames:
    def __init__(self):
        self.i = -1

    def next(self):
        self.i += 1
        return np.full((4, 4, 3), self.i * 10, np.uint8)

    def close(self):
        pass


def test_ramp_reader_blends_between_source_frames():
    r = motion.RampReader(_Frames(), 24)
    assert r.at(0.0)[0, 0, 0] == 0
    assert r.at(0.5 / 24)[0, 0, 0] == 5                                       # half way between frame 0 and 1
    assert r.at(3 / 24)[0, 0, 0] == 30


@pytest.mark.parametrize("kind", motion.TRANSITIONS)
def test_every_transition_changes_only_its_own_frames(kind):
    f = np.repeat(np.linspace(40, 140, 36, dtype=np.uint8)[None, :, None], 64, 0).repeat(3, 2)  # a gradient
    prev = np.full((64, 36, 3), 200, np.uint8)
    n_in, n_out = motion.IN_FRAMES[kind], motion.OUT_FRAMES[kind]
    assert motion.transition_in(kind, f, prev, n_in, 1) is f
    assert motion.transition_out(kind, f, n_out + 1, 1) is f
    if kind not in ("cut",) and n_in:
        assert not np.array_equal(motion.transition_in(kind, f, prev, 0, 1), f)
    if kind == "flash":
        assert motion.transition_in(kind, f, prev, 0, 1).mean() > 200       # a real flash frame
    if kind == "dip":
        assert motion.transition_out(kind, f, 1, 1).mean() < 5               # through black
    if kind == "whip":
        assert motion.transition_out(kind, f, 1, 1).std() < f.std()          # the pan smears the old shot


# ------------------------------------------------------------------------------------------------ the styles
def test_styles_have_their_own_motion_vocabulary():
    assert set(edits.MOTION) == set(edits.STYLES)
    vocab = {tuple(sorted(set(m["trans"]))) for m in edits.MOTION.values()}
    assert len(vocab) >= 5                                                    # cut vocabularies differ
    assert {m["hook"] for m in edits.MOTION.values()} == set(storyboard.HOOK_KINDS)
    assert set(edits.MOTION["found_footage"]["trans"]) <= {"glitch", "cut"}  # glitch only for found footage
    assert all("glitch" not in m["trans"] for k, m in edits.MOTION.items() if k != "found_footage")


def test_shorts_carry_grade_motion_and_safe_flashes(shorts):
    for sb in shorts:
        st = sb["template"]
        assert sb["grade"] == st and sb["hook"]["kind"] == edits.MOTION[st]["hook"]
        assert all(s["hand"] == edits.MOTION[st]["hand"] for s in sb["shots"])
        middle = [s["transition"] for s in sb["shots"][1:-1]]
        assert set(middle) <= set(edits.MOTION[st]["trans"]) | {"whip"}
        t, flashes = 0.0, []
        for s in sb["shots"]:
            if s["transition"] == "flash" or "impact" in s["fx"]:
                flashes.append(t)
            t += s["seconds"]
        assert all(b - a >= storyboard.MIN_IMPACT_GAP for a, b in zip(flashes, flashes[1:]))
        if edits.MOTION[st]["ramp"]:
            assert sb["shots"][-2]["ramp"][0][1] == edits.MOTION[st]["ramp"]   # slow motion into the reveal
        last = sb["shots"][-1]
        if "ramp" in last:                                                     # the freeze frame on the monster
            (t0, s0), (t1, s1) = last["ramp"]
            assert s0 == 0 and s1 == 1 and t1 - t0 == pytest.approx(edits.FREEZE_HOLD) and last["punches"] == [t0]


def test_consecutive_shorts_never_share_a_style(shorts):
    styles = [sb["template"] for sb in shorts]
    assert all(a != b for a, b in zip(styles, styles[1:]))
    assert len(set(styles)) == len(edits.STYLES)


def test_validator_rejects_bad_motion(shorts):
    sb = shorts[0]
    for field, bad in (("hand", 0.5), ("punch", 1.0), ("ramp", [[0.1, 5.0]]), ("ramp", "fast"), ("punches", [99.0])):
        broken = {**sb, "shots": [dict(s) for s in sb["shots"]]}
        broken["shots"][1][field] = bad
        with pytest.raises(storyboard.StoryboardError):
            storyboard.validate(broken)
    broken = {**sb, "shots": [dict(s) for s in sb["shots"]], "grade": "neon"}
    with pytest.raises(storyboard.StoryboardError):
        storyboard.validate(broken)
    broken = {**sb, "shots": [dict(s) for s in sb["shots"]]}
    broken["shots"][1]["transition"] = "flash"                                # right after the cold open's impact
    if broken["shots"][0]["seconds"] < storyboard.MIN_IMPACT_GAP:
        with pytest.raises(storyboard.StoryboardError):
            storyboard.validate(broken)


# ------------------------------------------------------------------------------------------------ sound cues
def _sb_with(shots):
    return {"grade": "chase_cut", "shots": shots}


def _clip(secs, tr="cut", **kw):
    return {"kind": "clip", "seconds": secs, "transition": tr, "fx": kw.pop("fx", []), **kw}


def test_edit_cues_land_on_the_cut_frames():
    sb = _sb_with([_clip(1.0, fx=["impact"]), _clip(1.0, "whip"), _clip(1.0, "flash", punches=[0.5]),
                   _clip(1.0, "leak", ramp=[[0.4, 0.4]]), _clip(1.5, "dip", fx=["impact"], ramp=[[0.5, 0.0], [0.8, 1.0]]),
                   _clip(1.0, "glitch")])
    cues = rig.edit_cues(sb, 24)
    by = {(c["kind"], c["f"]) for c in cues}
    assert ("whoosh", 24) in by and next(c for c in cues if c["f"] == 24)["dir"] == "left"
    assert ("hit", 48) in by and ("hit", 60) in by                            # flash frame + its punch
    assert ("swell", 72) in by
    assert ("riser", 96) in by                                                # slow motion runs into the next cut
    assert ("subdrop", 96) in by and ("boom", 108) in by                      # freeze frame at 0.5 s in
    assert ("glitch", 132) in by
    assert rig.edit_cues({"shots": sb["shots"]}, 24) == []                    # older storyboards: unchanged sound


def test_shorts_props_carry_the_edit_cues(shorts):
    for sb in shorts[:6]:
        p = overlay.props_from_storyboard(sb)
        kinds = {c["kind"] for c in p["cues"]}
        assert p["graded"] is True and p["hook"]["kind"] == sb["hook"]["kind"]
        assert p["cues"] == sorted(p["cues"], key=lambda c: (c["f"], c["kind"]))
        assert kinds & {"whoosh", "hit", "swell", "subdrop", "glitch", "riser", "boom"}


# ------------------------------------------------------------------------------------------------ sound synthesis
@pytest.fixture
def synth(monkeypatch, tmp_path):
    """No sample files on disk: the synthesised fallback (the samples: tests/test_sfx_samples.py)."""
    monkeypatch.setattr(sfx, "SAMPLE_DIR", tmp_path / "no_samples")


def _onset(x, thr):
    return int(np.argmax(np.abs(x) > thr))


@pytest.mark.parametrize("kind", ["hit", "boom", "subdrop", "glitch"])
def test_new_hits_start_on_their_frame(kind, synth):
    n, frame = SR * 4, 30
    bus = sfx.effects([{"f": frame, "kind": kind, "gain": 0.8}], n, 24, seed=4)
    at = int(round(frame / 24 * SR))
    assert not bus[:at].any()
    assert at <= _onset(bus[:, 0], 0.01) <= at + int(0.012 * SR)


@pytest.mark.parametrize("kind", ["riser", "swell"])
def test_builds_end_exactly_on_their_frame(kind, synth):
    n, frame = SR * 4, 60
    bus = sfx.effects([{"f": frame, "kind": kind, "gain": 0.8, "len": 1.0}], n, 24, seed=4)
    at = int(round(frame / 24 * SR))
    assert not bus[at:].any()                                                 # silence from the landing on
    assert np.abs(bus[at - int(0.1 * SR):at]).max() > 0.2                    # loudest right before it
    assert np.abs(bus[at - int(0.95 * SR):at - int(0.8 * SR)]).max() < np.abs(bus[at - int(0.1 * SR):at]).max()


def test_impact_has_weight_on_a_phone_speaker():
    x = sfx.thump(np.random.default_rng(1), 1.0)
    spec = np.abs(np.fft.rfft(x[: SR // 2]))
    freqs = np.fft.rfftfreq(SR // 2, 1 / SR)
    assert spec[(freqs > 150) & (freqs < 400)].sum() > 0.15 * spec[freqs < 150].sum()


def test_room_tone_is_quiet_and_never_silent(synth):
    room = sfx.room_tone(SR * 3, 5, 0.05)
    assert np.abs(room).max() == pytest.approx(0.05, rel=0.05)
    assert np.abs(room[SR:2 * SR]).min() >= 0 and np.abs(room[SR:2 * SR]).mean() > 0.005
    score = np.zeros((SR * 3, 2), np.float32)
    out = sfx.mix(score, [], 24, 5, room=0.05)
    assert out.shape == score.shape and np.abs(out).max() <= sfx.PEAK + 1e-6 and out.any()


def test_loudnorm_measures_then_goes_linear(tmp_path):
    from bot import music
    from bot.render import loudnorm_filter
    wav = music.write_wav(tmp_path / "a.wav", music.score(4.0, 90, 3, "tense", [1.0]))
    f = loudnorm_filter(wav)
    assert f.startswith("loudnorm=I=-14:TP=-2.0") and "linear=true" in f and "measured_I=" in f


# ------------------------------------------------------------------------------------------------ long video
def test_long_video_gets_the_grade_and_edited_chapter_cuts():
    sb = longform.plan_long([], set(), FAKE_CLIPS, seed=77, now=NOW)
    assert sb["grade"] == "long"
    clips = [s for s in sb["shots"] if s["kind"] == "clip"]
    assert all(s["hand"] == longform.HAND for s in clips)
    chapters = [s["transition"] for s in sb["shots"] if s.get("chapter")]
    assert chapters and chapters[::2] == ["dip"] * len(chapters[::2]) and set(chapters[1::2]) <= {"leak"}
    ramps = [s for s in sb["shots"] if s.get("ramp")]
    assert ramps and all(r["ramp"][0][1] == longform.RAMP_SPEED for r in ramps)
    kinds = {c["kind"] for c in rig.edit_cues(sb, 24)}
    assert {"subdrop", "swell", "riser"} <= kinds


def test_a_flash_cut_right_before_the_impact_becomes_a_whip():
    mv = {**edits.MOTION["chase_cut"], "punch": 0, "every": 0, "ramp": None}
    shots = [{"seconds": 1.0, "transition": "cut", "fx": ["impact"]}, {"seconds": 2.0, "transition": "whip", "fx": []},
             {"seconds": 1.2, "transition": "flash", "fx": []}, {"seconds": 2.3, "transition": "cut", "fx": ["impact"]}]
    edits._beat_motion(shots, mv, 100, None)
    assert shots[2]["transition"] == "whip"                                  # 1.2 s before the turn's impact
    shots[2].update(seconds=1.8, transition="flash")
    edits._beat_motion(shots, mv, 100, None)
    assert shots[2]["transition"] == "flash"                                 # far enough from both impacts


def test_validator_survives_garbage_types():
    sb = planner.plan([], set(), "auto", FAKE_CLIPS, seed=31, now=NOW)[1]
    for bad in ({"grade": ["x"]}, {"grade": {"a": 1}}):
        with pytest.raises(storyboard.StoryboardError):
            storyboard.validate({**sb, **bad})
    broken = {**sb, "shots": [dict(s) for s in sb["shots"]]}
    broken["shots"][1].update(seconds="long", punches=[0.1], ramp=[[0.1, 0.5]], transition=["whip"])
    with pytest.raises(storyboard.StoryboardError):
        storyboard.validate(broken)


def test_mix_never_starts_on_a_full_scale_sample():
    score = np.full((SR, 2), 0.5, np.float32)
    out = sfx.mix(score, [{"f": 0, "kind": "impact", "gain": 0.9}], 24, 3)
    assert abs(out[0]).max() < 1e-6 and abs(out[int(sfx.START_RAMP * SR) // 2]).max() < 0.5
