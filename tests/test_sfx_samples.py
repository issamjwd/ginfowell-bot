"""The CC0 sound effects (assets/sfx/): on disk with credits, each one's anchor (onset / crest / end of build) on its
cue frame, varied per episode, the synth fallback when a file is missing; and frame 0 of a video is never black."""
import numpy as np
import pytest

from bot import config, footage, sfx
from bot.music import SR
from bot.render import Renderer
from tests.conftest import tiny_clip

from .test_overlay import _sb

FPS = 24


def _at(frame: int) -> int:
    return int(round(frame / FPS * SR))


def _bus(cue: dict, seed: int = 3, seconds: float = 8.0) -> np.ndarray:
    return sfx.effects([cue], int(seconds * SR), FPS, seed)


def _anchor_of(cue: dict, seed: int = 3) -> int:
    """Where effects() put the sound's anchor: re-derive the same sound with the same seeding."""
    snd, anchor = sfx.sampled(cue, np.random.default_rng([seed & 0xFFFFFFFF, 0, int(cue["f"]), 7]),
                              sfx.video_pitch(seed))
    return anchor


# ------------------------------------------------------------------------------------------------ the files
def test_every_sample_is_in_the_repo_small_and_credited():
    names = {n for alts in sfx.SAMPLES.values() for n in alts}
    assert len(names) == 13
    credits = (config.ASSETS / "sfx" / "CREDITS.txt").read_text(encoding="utf-8")
    total = 0
    for n in sorted(names):
        f = sfx.SAMPLE_DIR / f"{n}.ogg"
        assert f.is_file(), n
        total += f.stat().st_size
        assert f"{n}.mp3 |" in credits and "CC0" in credits.split(f"{n}.mp3 |")[1].splitlines()[0]
        x = sfx._sample(n)
        assert x is not None and x.shape[1] == 2 and np.abs(x).max() == pytest.approx(1.0)
        if n != "room_tone":
            assert len(x) <= 4.6 * SR, n                                     # long tails trimmed
    assert total < 1_200_000


# ------------------------------------------------------------------------------------------------ anchors
def test_onset_finds_a_transient_after_quiet_lead_in():
    rng = np.random.default_rng(0)
    x = np.zeros((SR, 2))
    x[:4000] = rng.standard_normal((4000, 2)) * 0.01                    # a faint rumble before it
    x[5000:9000] = rng.standard_normal((4000, 2)) * np.exp(-np.arange(4000) / 900)[:, None]
    assert abs(sfx.onset(x) - 5000) <= int(0.003 * SR)
    y = np.zeros((SR, 2))
    y[:3000] = rng.standard_normal((3000, 2))                            # loud from sample 0
    assert sfx.onset(y) <= int(0.003 * SR)


@pytest.mark.parametrize("cue", [
    {"f": 40, "kind": "impact", "gain": 0.9},
    {"f": 40, "kind": "hit", "gain": 0.5},
    {"f": 40, "kind": "boom", "gain": 0.95},
    {"f": 40, "kind": "subdrop", "gain": 0.8},
    {"f": 40, "kind": "glitch", "gain": 0.5},
])
def test_transient_lands_on_its_frame(cue):
    bus = _bus(cue)
    at = _at(cue["f"])
    assert abs(sfx.onset(bus) - at) <= int(0.005 * SR)                   # the hit is ON the frame (a frame is 42 ms)
    before = np.abs(bus[: at - int(0.01 * SR)]).max(initial=0.0)
    assert before < 0.2 * np.abs(bus).max()                              # only a quiet lead-in comes before it


@pytest.mark.parametrize("direction", ["left", "up"])
def test_whoosh_crests_on_its_landing_frame(direction):
    cue = {"f": 48, "kind": "whoosh", "gain": 0.6, "dir": direction}
    bus = _bus(cue)
    peak = sfx.crest(bus)
    assert abs(peak - _at(48)) <= int(0.02 * SR)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_riser_ends_exactly_on_the_reveal_frame(seed):
    cue = {"f": 96, "kind": "riser", "gain": 0.6, "len": 1.1}
    bus = _bus(cue, seed)
    at = _at(96)
    assert not bus[at:].any()                                            # silence from the reveal frame on
    tail = np.abs(bus[at - int(0.15 * SR): at - int(0.006 * SR)]).max()
    assert tail > 0.6 * np.abs(bus).max()                                # loudest right into it, not faded away
    start = np.nonzero(np.abs(bus).max(axis=1) > 0)[0][0]
    assert abs((at - start) - 1.1 * SR) <= 2                             # as long as the ramp asked for


def test_swell_peaks_on_the_cut():
    bus = _bus({"f": 60, "kind": "swell", "gain": 0.55, "len": 0.8})
    at = _at(60)
    assert not bus[at:].any()
    env = sfx._env(bus, 30.0)
    assert env[at - int(0.05 * SR): at - int(0.015 * SR)].max() >= 0.7 * env.max()   # at full swell into the cut
    assert env[at - int(0.4 * SR)] < 0.6 * env.max()                                  # and it swelled up to it


def test_impact_is_layered_with_the_synth_sub():
    cue = {"f": 24, "kind": "impact", "gain": 0.9}
    bus = _bus(cue)
    at = _at(24)
    seg = bus[at: at + int(0.3 * SR)].mean(axis=1)
    spec = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(len(seg), 1 / SR)
    low = spec[(freqs > 30) & (freqs < 70)].sum() / spec[(freqs > 30) & (freqs < 2000)].sum()
    assert low > 0.12                                                    # the 38-62 Hz sub is there for weight


# ------------------------------------------------------------------------------------------------ variety
def test_videos_differ_but_each_is_repeatable():
    cues = [{"f": 30, "kind": "impact", "gain": 0.9}, {"f": 70, "kind": "whoosh", "gain": 0.6, "dir": "left"},
            {"f": 110, "kind": "glitch", "gain": 0.5}, {"f": 150, "kind": "subdrop", "gain": 0.8}]
    a = sfx.effects(cues, 8 * SR, FPS, 11)
    assert np.array_equal(a, sfx.effects(cues, 8 * SR, FPS, 11))
    assert not np.allclose(a, sfx.effects(cues, 8 * SR, FPS, 12))
    pitches = {round(sfx.video_pitch(s), 3) for s in range(40)}
    assert len(pitches) > 30 and all(abs(p) <= 1.0 for p in pitches)
    picks = set()
    for s in range(30):                                                  # both drops / glitches get used
        rng = np.random.default_rng([s, 0, 150, 7])
        snd, _ = sfx.sampled({"f": 150, "kind": "subdrop", "gain": 0.8}, rng, 0.0)
        picks.add(len(snd) > 3.5 * SR)                                   # impact_downer is the longer one
    assert picks == {True, False}


def test_pitch_shift_stays_within_two_semitones():
    x = sfx._sample("impact_punch")
    for semis in (-2.0, 2.0):
        y = sfx.varispeed(x, semis)
        assert len(y) == pytest.approx(len(x) / 2 ** (semis / 12), abs=2)


# ------------------------------------------------------------------------------------------------ fallback + room
def test_missing_files_fall_back_to_the_synth(monkeypatch, tmp_path):
    cue = {"f": 24, "kind": "impact", "gain": 0.9}
    with_samples = _bus(cue)
    monkeypatch.setattr(sfx, "SAMPLE_DIR", tmp_path)
    assert sfx.sampled(cue, np.random.default_rng(0), 0.0) is None
    synth = _bus(cue)
    at = _at(24)
    assert not synth[:at].any() and np.abs(synth[at: at + int(0.01 * SR)]).max() > 0.05
    assert not np.allclose(with_samples, synth)
    (tmp_path / "impact_cinematic.ogg").write_bytes(b"not audio")      # a broken file: synth too, no crash
    sfx._load.cache_clear()
    assert sfx.sampled(cue, np.random.default_rng(0), 0.0) is None


def test_room_tone_loops_quietly_from_a_random_point():
    n = 70 * SR                                                          # longer than the 40 s recording
    a = sfx.room_tone(n, 5, 0.05)
    b = sfx.room_tone(n, 6, 0.05)
    assert a.shape == (n, 2) and np.abs(a).max() <= 0.05 + 1e-9
    assert np.abs(a[n // 2]).max() > 0                                   # never digital silence
    assert not np.allclose(a[SR: 2 * SR], b[SR: 2 * SR])                # another episode starts elsewhere
    assert not a[0].any() and not a[-1].any()                            # faded in and out
    jump = np.abs(np.diff(a[SR:-SR], axis=0)).max()
    assert jump < 0.05                                                   # no click at the loop seam


# ------------------------------------------------------------------------------------------------ frame 0
@pytest.mark.parametrize("tr", ["dip", "flash", "leak", "zoom", "glitch", "fade", "whip"])
def test_first_frame_never_opens_through_a_transition(tr, tmp_path, monkeypatch):
    tiny_clip(tmp_path / "halls.mp4", seconds=4, w=240, h=270, audio=False)
    monkeypatch.setenv("BOT_FOOTAGE_DIR", str(tmp_path))
    sb = _sb(music={"bpm": 90, "seed": 3}, grade="clean")
    sb["shots"][0].update(clip="halls", transition=tr, fx=["impact"], **{"from": 0.0})
    plain = _sb(music={"bpm": 90, "seed": 3}, grade="clean")
    plain["shots"][0].update(clip="halls", transition="cut", fx=["impact"], **{"from": 0.0})
    r, p = Renderer(sb, 216, 384, FPS, text=False), Renderer(plain, 216, 384, FPS, text=False)
    try:
        f0 = r.frame(0)
        assert np.array_equal(f0, p.frame(0))                            # exactly the plain cut's first frame
        assert not footage.is_black(f0) and f0.mean() > 40
    finally:
        r.close()
        p.close()


def test_first_frame_skips_a_black_lead_in(tmp_path, monkeypatch):
    tiny_clip(tmp_path / "halls.mp4", seconds=4, w=240, h=270, audio=False, black=(0.0, 0.3))
    monkeypatch.setenv("BOT_FOOTAGE_DIR", str(tmp_path))
    sb = _sb(music={"bpm": 90, "seed": 3}, grade="clean")
    sb["shots"][0].update(clip="halls", fx=["impact"], **{"from": 0.0})
    r = Renderer(sb, 216, 384, FPS, text=False)
    try:
        frames = [r.frame(i) for i in range(4)]
    finally:
        r.close()
    assert not any(footage.is_black(f) for f in frames)


# ------------------------------------------------------------------------------------------------ the master
def test_encoded_audio_stays_under_minus_one_dbtp(tmp_path, monkeypatch):
    from bot import music, render
    score = music.score(6.0, 90, 3, "tense", [1.0])
    cues = [{"f": 24, "kind": "impact", "gain": 0.9}, {"f": 60, "kind": "subdrop", "gain": 0.8},
            {"f": 96, "kind": "boom", "gain": 0.95}]
    wav = music.write_wav(tmp_path / "a.wav", sfx.mix(score, cues, FPS, 3, room=0.05))
    out = render.encode_audio(wav, tmp_path / "a.m4a")
    assert render.true_peak(out) <= render.MAX_TRUE_PEAK + 1e-6
    seen = []
    peaks = iter([0.4, -1.5])                                            # an overshoot, then a good encode
    monkeypatch.setattr(render, "true_peak", lambda p: next(peaks))
    real = render.loudnorm_filter
    monkeypatch.setattr(render, "loudnorm_filter", lambda w, tp=render.TRUE_PEAK: seen.append(tp) or real(w, tp))
    render.encode_audio(wav, tmp_path / "b.m4a")
    assert seen[0] == render.TRUE_PEAK and seen[1] == pytest.approx(render.TRUE_PEAK - (0.4 - render.MAX_TRUE_PEAK) - 0.2)
