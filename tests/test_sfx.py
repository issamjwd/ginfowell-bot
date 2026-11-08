"""Motion v3: the rig (one timing source), the synthesised sound effects (timing, determinism, ducking, level).
The CC0 samples that replace most synth sounds are covered in tests/test_sfx_samples.py; the synth tests here run
with no samples on disk (the fallback)."""
import json

import numpy as np
import pytest

from bot import fx, music, overlay, rig, sfx
from bot.render import Renderer

from .test_overlay import _sb

SR = music.SR


def _props(**over):
    sb = _sb(**over)
    sb["shots"][1]["fx"] = ["impact"]
    sb["shots"][2]["transition"] = "whip"
    return sb, overlay.props_from_storyboard(sb, 1080, 1920, 24)


# ------------------------------------------------------------------------------------------------ the rig
def test_cue_sheet_is_one_sorted_json_timeline():
    _, p = _props()
    cues = p["cues"]
    assert cues == sorted(cues, key=lambda c: (c["f"], c["kind"]))
    assert all(0 <= c["f"] < p["durationInFrames"] for c in cues)
    assert {c["kind"] for c in cues} == {"whoosh", "impact", "tick"}
    assert json.loads(json.dumps(p)) == p


def test_impact_cue_is_the_renderers_impact_frame_and_the_caption_slam():
    sb, p = _props()
    impact = [c for c in p["cues"] if c["kind"] == "impact"]
    assert [c["f"] for c in impact] == [rig.first_frame(4.0, 24)] == [96]
    c1 = p["captions"][1]
    assert round(c1["start"] * 24) + c1["hitAt"] == impact[0]["f"]          # the slam lands on the same frame
    assert not any(c["kind"] == "whoosh" and c["f"] == 96 for c in p["cues"])  # an impact caption gets no whoosh
    # the burst rides the renderer's own impact shake from that frame
    dx, dy = fx.shake_offset(0.0, 1, 34.0, 4.0)
    assert impact[0]["shake"][0] == [-dx, -dy]


def test_impact_without_a_caption_still_gets_a_cue():
    sb = _sb()
    sb["shots"][1]["fx"] = []
    sb["shots"][3]["fx"] = ["impact"]                                          # the captionless clip at 9.0 s
    p = overlay.props_from_storyboard(sb, 1080, 1920, 24)
    assert [c["f"] for c in p["cues"] if c["kind"] == "impact"] == [216]


def test_impact_on_a_fractional_cut_follows_the_renderer():
    sb = _sb()
    sb["shots"][0]["seconds"] = 4.01
    sb["shots"][1]["fx"] = ["impact"]
    p = overlay.props_from_storyboard(sb, 1080, 1920, 24)
    assert [c["f"] for c in p["cues"] if c["kind"] == "impact"] == [97]        # ceil(4.01 * 24), as bot/render.py


def test_whoosh_on_each_text_entrance():
    _, p = _props()
    w = [c for c in p["cues"] if c["kind"] == "whoosh"]
    hook_first = p["hook"]["wordFrames"][0]
    c0, _, c2 = p["captions"]
    assert w[0]["f"] == hook_first and w[0]["dir"] == "up"
    assert any(c["f"] == round(c0["start"] * 24) for c in w)                   # caption after the hook
    whip = [c for c in w if c["dir"] == "left"]
    assert [c["f"] for c in whip] == [round(c2["start"] * 24)]                 # the whip gets the long whoosh
    assert any(c["f"] == round(p["outro"]["start"] * 24) + 6 for c in w)       # the outro's first word


def test_hook_word_frames_match_the_overlay_formula():
    # Short.jsx: gap = max(1, min(4, round(beatFrames / 4), floor(room * 0.45 / words))), word i at 1 + i * gap
    assert rig.hook_word_frames("did you see it?", 2.0, 60 / 92, 24) == [1, 5, 9, 13]
    assert rig.hook_word_frames("one two three four five six seven eight", 1.0, 0.65, 24) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert rig.hook_word_frames("", 2.0, 0.65, 24) == []
    assert rig.js_round(2.5) == 3 and rig.js_round(3.5) == 4                  # JavaScript rounding, not banker's


def test_end_card_ticks_land_on_each_letter():
    _, p = _props()
    end_from = p["durationInFrames"] - rig.js_round(p["endCard"]["seconds"] * 24)
    ticks = [c for c in p["cues"] if c["kind"] == "tick"]
    assert len(ticks) == len(p["endCard"]["title"]) == len(p["endCard"]["letterFrames"])
    assert [c["f"] - end_from for c in ticks] == p["endCard"]["letterFrames"] == list(range(3, 3 + len(ticks)))


def test_burned_fallback_cues_skip_overlay_only_sounds():
    sb = _sb()
    sb["shots"][1]["fx"] = ["impact"]
    cues = rig.burned_cues(sb, 24)
    assert {c["kind"] for c in cues} == {"whoosh", "impact"}                   # no hook, no lettered end card
    assert [c["f"] for c in cues if c["kind"] == "impact"] == [96]
    assert [c["f"] for c in cues if c["kind"] == "whoosh"] == [6, 150]         # clip + scene captions, settled


# ------------------------------------------------------------------------------------------------ sound
@pytest.fixture
def synth(monkeypatch, tmp_path):
    """No sample files: every cue uses its synthesised fallback."""
    monkeypatch.setattr(sfx, "SAMPLE_DIR", tmp_path / "no_samples")


def _onset(x: np.ndarray, thresh: float) -> int:
    return int(np.argmax(np.abs(x) > thresh))


@pytest.mark.parametrize("frame", [24, 37, 100])
def test_thump_starts_on_its_frame(frame, synth):
    n = SR * 6
    bus = sfx.effects([{"f": frame, "kind": "impact", "gain": 0.9}], n, 24, seed=5)
    at = int(round(frame / 24 * SR))
    assert not bus[:at].any()                                                  # nothing before the frame
    assert at <= _onset(bus[:, 0], 0.01) <= at + int(0.004 * SR)               # lands within 4 ms of it


def test_whoosh_peaks_into_its_landing_frame(synth):
    n, frame = SR * 4, 48
    bus = sfx.effects([{"f": frame, "kind": "whoosh", "gain": 0.5, "dir": "up"}], n, 24, seed=1)
    at = int(round(frame / 24 * SR))
    env = np.abs(bus[:, 0] + bus[:, 1])
    peak = int(np.argmax(env))
    assert at - int(0.08 * SR) <= peak <= at + int(0.03 * SR)                  # the swell crests on the landing
    assert not bus[at + int(0.2 * SR):].any()                                  # and is gone soon after
    assert bus[at - int(sfx.WHOOSH_PRE * SR) + 10: at].any()                   # it builds up before it


def test_ticks_are_short_and_on_their_frames(synth):
    cues = [{"f": 100 + i, "kind": "tick", "gain": 0.16, "i": i} for i in range(4)]
    bus = sfx.effects(cues, SR * 6, 24, seed=2)
    for c in cues:
        at = int(round(c["f"] / 24 * SR))
        assert np.abs(bus[at:at + int(0.01 * SR)]).max() > 0.02
    assert not bus[int(round(104 / 24 * SR)) + int(0.07 * SR):].any()


def test_sound_design_is_deterministic():
    _, p = _props()
    score = music.score(16.0, 92, 7, "tense", [4.0])
    a = sfx.mix(score, p["cues"], 24, 7)
    b = sfx.mix(score, p["cues"], 24, 7)
    assert np.array_equal(a, b)
    c = sfx.mix(score, p["cues"], 24, 8)                                       # another episode sounds different
    assert not np.array_equal(a, c)


def test_score_ducks_under_effects_and_mix_stays_in_range():
    _, p = _props()
    n = SR * 16
    duck = sfx.duck_curve(p["cues"], n, 24)
    impact = next(c for c in p["cues"] if c["kind"] == "impact")
    at = int(round(impact["f"] / 24 * SR))
    assert duck[at + int(0.01 * SR)] < 0.6                                     # about -5 dB or more under a thump
    assert duck.min() >= 1 - max(d for d, _, _ in sfx.DUCK.values()) - 1e-9   # never stacks deeper than one duck
    assert duck[int(9.5 * SR)] == pytest.approx(1.0)                           # untouched between cues
    out = sfx.mix(music.score(16.0, 92, 7, "tense", [4.0]), p["cues"], 24, 7)
    assert out.shape == (int(16.0 * SR), 2) and out.dtype == np.float32
    assert np.abs(out).max() <= sfx.PEAK + 1e-6
    assert np.abs(out).max() > 0.3                                             # still a real level before loudnorm


def test_no_cues_leaves_the_score_alone():
    score = music.score(3.0, 92, 1)
    assert sfx.mix(score, [], 24, 1) is score


def test_renderer_audio_uses_the_rig(monkeypatch, tmp_path):
    sb = _sb(music={"bpm": 92, "seed": 3, "mood": "tense"})
    sb["shots"][1]["fx"] = ["impact"]
    seen = {}
    real = sfx.mix

    def spy(score, cues, fps, seed, room=0.0):
        seen["cues"], seen["seed"], seen["room"] = cues, seed, room
        return real(score, cues, fps, seed, room)

    monkeypatch.setattr(sfx, "mix", spy)
    clean = Renderer(sb, 108, 192, 24, text=False)
    clean.audio(tmp_path / "a.wav")
    assert seen["cues"] == overlay.props_from_storyboard(sb, 108, 192, 24)["cues"] and seen["seed"] == 3
    assert seen["room"] == 0.0                                    # a storyboard without a grade: no room tone
    burned = Renderer(sb, 108, 192, 24, text=True)
    burned.audio(tmp_path / "b.wav")
    assert seen["cues"] == rig.burned_cues(sb, 24)
    monkeypatch.setenv("BOT_SFX", "off")
    seen.clear()
    burned.audio(tmp_path / "c.wav")
    assert not seen and (tmp_path / "c.wav").stat().st_size > 1000


def test_clean_audio_survives_a_storyboard_the_overlay_cannot_read(tmp_path):
    sb = _sb(music={"bpm": 92, "seed": 3}, dream_ids=[])                    # props_from_storyboard would raise
    sb["shots"][1]["fx"] = ["impact"]
    r = Renderer(sb, 108, 192, 24, text=False)
    assert r.cues() == rig.burned_cues(sb, 24)
    assert r.audio(tmp_path / "a.wav").stat().st_size > 1000


def test_no_whoosh_or_impact_cue_under_the_end_card():
    sb = _sb()
    sb["shots"][5]["fx"] = ["impact"]                                         # an impact on the end card itself
    p = overlay.props_from_storyboard(sb, 1080, 1920, 24)
    end_from = p["durationInFrames"] - rig.js_round(p["endCard"]["seconds"] * 24)
    assert all(c["f"] < end_from for c in p["cues"] if c["kind"] != "tick")
