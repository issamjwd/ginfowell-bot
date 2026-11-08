"""The 16:9 long video: planning, metadata, overlay props / frame ranges, renderer layout, QA limits."""
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from bot import bank, config, footage, longform, metadata, overlay, qa, storyboard
from bot.render import Renderer
from tests.conftest import FAKE_CLIPS, tiny_clip

NOW = datetime(2026, 11, 4, 15, 23, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def series():
    history, used, out = [], set(), []
    for i in range(10):
        sb = longform.plan_long(history, used, FAKE_CLIPS, seed=100 + i, now=NOW + timedelta(days=2 * i))
        used.add(sb["fingerprint"])
        history.append({"kind": "long", "dream_ids": sb["dream_ids"], "lines": sb["lines"], "title": sb["title"],
                        "fingerprint": sb["fingerprint"], "music_seed": sb["music"]["seed"],
                        "ts": (NOW + timedelta(days=2 * i)).isoformat()})
        out.append(sb)
    return out


def test_long_storyboards_are_valid_and_two_to_four_minutes(series):
    for sb in series:
        assert sb["kind"] == "long" and sb["episode_id"].startswith("ep-") and "-long-" in sb["episode_id"]
        total = storyboard.total_seconds(sb)
        assert 105 <= total <= 250
        assert config.LONG_MIN_SECONDS < total < config.LONG_MAX_SECONDS
        storyboard.validate(sb, footage=set(FAKE_CLIPS))


def test_long_rotation(series):
    fps = [s["fingerprint"] for s in series]
    assert len(set(fps)) == len(fps)
    for a, b in zip(series, series[1:]):
        assert a["dream_ids"][0] != b["dream_ids"][0]          # a new lead dream every time
        assert a["lines"]["hook"] != b["lines"]["hook"]
        assert a["title"] != b["title"]
    tagged = footage.tags()
    current = {d.id for d in bank.dreams() if len(longform.current_clips(d, FAKE_CLIPS, tagged)) >= longform.MIN_CLIPS}
    for sb in series:                                             # only dreams of the upgraded game get chapters
        assert set(sb["dream_ids"]) <= current and len(sb["dream_ids"]) == min(len(current), 6)
    n = -(-len(current) // 6)                                     # every current dream comes round within n+1 videos
    for i in range(len(series) - n):
        assert set().union(*(s["dream_ids"] for s in series[i:i + n + 1])) == current


def test_hook_lands_in_the_first_five_seconds_and_chapters_are_youtube_valid(series):
    for sb in series:
        assert sb["hook"]["title"] and "{" not in sb["hook"]["title"]
        p = overlay.props_from_storyboard(sb, 1920, 1080, 24)
        assert p["hook"]["until"] <= 5.0 and p["hook"]["wordFrames"][-1] < 5 * 24
        stamps = re.findall(r"^(\d+):(\d\d) (.+)$", sb["description"], re.M)
        secs = [int(m) * 60 + int(s) for m, s, _ in stamps]
        assert secs[0] == 0 and len(secs) >= 4                  # YouTube chapters: start at 0:00, 3+ chapters
        ends = secs[1:] + [int(storyboard.total_seconds(sb))]
        assert all(b - a >= 10 for a, b in zip(secs, ends))     # each chapter at least 10 s
        assert [n for _, _, n in stamps[1:]] == [bank.dream(d).name for d in sb["dream_ids"]]


def test_long_metadata_is_not_a_short(series):
    for sb in series:
        assert "#shorts" not in sb["title"].lower() and "#shorts" not in sb["description"].lower()
        assert "shorts" not in [t.lower() for t in sb["tags"]]
        assert sum(len(t) for t in sb["tags"]) <= 450
        assert metadata.STORE_URL in sb["description"]


def test_long_overlay_props_ranges_and_cues(series):
    sb = series[0]
    p = overlay.props_from_storyboard(sb, 1920, 1080, 24)
    n = p["durationInFrames"]
    assert p["layout"] == "landscape" and p["dust"] is False
    assert len(p["chapters"]) == len(sb["dream_ids"])
    r = p["ranges"]
    assert r[0][0] == 0 and r[-1][1] == n - 1                  # hook first, outro + end card last
    assert all(a <= b for a, b in r) and all(b1 + overlay.RANGE_GAP < a2 for (_, b1), (a2, _) in zip(r, r[1:]))
    covered = set()
    for a, b in r:
        covered.update(range(a, b + 1))
    for c in p["captions"]:                                     # every caption frame is rendered
        assert set(range(round(c["start"] * 24), round(c["end"] * 24))) <= covered
    for ch in p["chapters"]:
        assert ch["wordFrames"] and round(ch["start"] * 24) in covered
    kinds = {q["kind"] for q in p["cues"]}
    assert {"whoosh", "tick"} <= kinds
    assert sum(b - a + 1 for a, b in r) < n                     # the plain footage between texts is not re-rendered
    assert overlay.TIMEOUT <= overlay.timeout_for(p) <= overlay.TIMEOUT_MAX


def test_long_alpha_ranges_are_text_only_and_the_film_tail_is_opaque_only_when_ungraded(series):
    sb = dict(series[0])
    assert "grade" in sb
    # graded (edit v4): the tail's light film texture is plain alpha too, so EVERY range is text only and the video
    # is made in one pass (the overlay frames first, then one encode)
    p = overlay.props_from_storyboard(sb, 1920, 1080, 24)
    assert p["graded"] is True and len(p["alpha"]) == len(p["ranges"]) and all(p["alpha"])
    assert overlay.single_pass_ok(sb, 1920, 1080, 24)
    # an older storyboard without the ffmpeg grade: the full film grain blends with the picture, the tail is opaque
    sb.pop("grade")
    p = overlay.props_from_storyboard(sb, 1920, 1080, 24)
    al = p["alpha"]
    assert len(al) == len(p["ranges"]) and al[-1] is False and all(al[:-1])
    assert all(b < p["filmFrom"] for (_, b), x in zip(p["ranges"], al) if x)   # nothing film-textured is alpha
    assert not overlay.single_pass_ok(sb, 1920, 1080, 24)
    assert "alpha" not in overlay.props_from_storyboard(__import__("tests.test_overlay", fromlist=["_sb"])._sb(),
                                                        1080, 1920, 24)


def test_plain_names_renames_remotions_padded_frames(tmp_path):
    for n in (7, 8, 9, 10):
        (tmp_path / f"element-{n:02d}.png").write_bytes(b"x")
    overlay._plain_names(tmp_path, 7, 10)
    assert sorted(f.name for f in tmp_path.iterdir()) == ["element-10.png", "element-7.png", "element-8.png",
                                                          "element-9.png"]
    (tmp_path / "element-8.png").unlink()
    with pytest.raises(overlay.OverlayError):
        overlay._plain_names(tmp_path, 7, 10)


def test_short_props_are_unchanged_by_the_long_video():
    from tests.test_overlay import _sb
    p = overlay.props_from_storyboard(_sb(), 1080, 1920, 24)
    assert p["layout"] == "portrait" and p["chapters"] == []
    assert "ranges" not in p and "dust" not in p and "filmFrom" not in p


def test_panel_layout_renders_footage_at_its_own_shape(tmp_path, monkeypatch):
    clip = tiny_clip(tmp_path / "halls.mp4", seconds=4, w=240, h=270, audio=False)
    monkeypatch.setenv("BOT_FOOTAGE_DIR", str(tmp_path))
    sb = {"kind": "long", "dream_ids": ["halls"], "music": {"bpm": 80, "seed": 3, "mood": "soft"},
          "shots": [{"kind": "clip", "layout": "panel", "backdrop": "halls", "clip": "halls", "seconds": 1.0,
                     "from": 0.0, "speed": 1.0, "pan": 0.0, "zoom": [1.0, 1.0], "caption": None, "fx": []}]}
    r = Renderer(sb, 384, 216, 24, text=False)
    assert r.panel_width() == 192                                # 8:9 of the height
    f = r.frame(3)
    r.close()
    assert f.shape == (216, 384, 3)
    assert clip.exists() and np.asarray(f).std() > 5


def test_qa_long_limits(tmp_path):
    v = tiny_clip(tmp_path / "v.mp4", seconds=12, w=192, h=108)
    rep = qa.check(v, 192, 108, max_seconds=config.LONG_MAX_SECONDS, min_seconds=config.LONG_MIN_SECONDS)
    assert rep.checks["size"] == "ok" and rep.checks["length"] != "ok"   # 12 s is far too short for the long video
    assert qa.check(v, 192, 108).checks["length"] == "ok"               # but fine under the Shorts limits


def test_bot_kind_setting():
    assert config.Settings.from_env({}).kind == "short"
    assert config.Settings.from_env({"BOT_KIND": "long"}).kind == "long"
    with pytest.raises(config.ConfigError):
        config.Settings.from_env({"BOT_KIND": "movie"})


def test_long_meets_the_shorts_bar(series):
    tagged = footage.tags()
    for sb in series:
        shots = sb["shots"]
        assert all(s["kind"] == "clip" for s in shots)                       # no static title / end cards
        assert all(longform.MIN_SHOT - 1e-6 <= s["seconds"] <= longform.MAX_SHOT + 1e-6 for s in shots)
        assert all(abs(s["zoom"][1] - s["zoom"][0]) >= 0.05 - 1e-6 for s in shots)  # a camera move on every shot
        assert all(tagged.get(s["clip"], {}).get("tags") for s in shots)     # current-game footage only
        first = shots[0]
        assert "scare" in tagged[first["clip"]]["tags"] and "impact" in first["fx"]   # a scare in the first second
        titled = [s for s in shots if s.get("chapter")]
        assert len(titled) == len(sb["dream_ids"])                           # titles ride footage shots
        moments = {}
        for s in shots:                                                      # no moment of a clip shown twice
            for a, b in moments.get(s["clip"], []):
                assert s["from"] + s["seconds"] <= a + 1e-6 or s["from"] >= b - 1e-6
            moments.setdefault(s["clip"], []).append((s["from"], s["from"] + s["seconds"]))
        assert sb["end_tag"]["seconds"] >= 5.0                               # end screen elements fit, over footage
        p = overlay.props_from_storyboard(sb, 1920, 1080, 24)
        assert p["endCard"]["style"] == "tag" and all(c["seconds"] <= 1.5 for c in p["chapters"])
        assert all(c["end"] - c["start"] >= 1.2 for c in sb["captions"])


def test_long_chapters_vary_clip_and_framing(series):
    for sb in series:
        shots = sb["shots"]
        # the same clip never plays twice in a row inside a chapter (the ~15 s of near-identical corridor fix)
        for a, b in zip(shots, shots[1:]):
            if not b.get("chapter"):
                assert a["clip"] != b["clip"]
        tight = [s for s in shots if s["zoom"][0] >= longform.TIGHT[0] - 1e-6 and s["pan"]]
        assert len(tight) >= len(sb["dream_ids"])                           # a closer angle in every chapter
        assert all(max(s["zoom"]) <= 1.24 for s in shots)                    # never a soft, over-zoomed crop


def test_long_cold_open_prefers_the_by_eye_list(series):
    ok = set(footage.cold_open_ok())
    tagged = footage.tags()
    for sb in series:
        own = {c for d in bank.dreams() if d.id in sb["dream_ids"] for c in longform.current_clips(d, FAKE_CLIPS, tagged)}
        candidates = {c for c in own & ok if "scare" in tagged[c]["tags"] and tagged[c].get("peak") is not None}
        if candidates:                                                    # never a dark tunnel / night opener
            assert sb["shots"][0]["clip"] in candidates


def test_clip_profiles_are_kept_between_runs_and_junk_is_ignored(tmp_path, monkeypatch):
    clips = tmp_path / "footage"
    clips.mkdir()
    (clips / "halls_walk_01.mp4").write_bytes(b"x" * 100)
    cache = tmp_path / "pc" / "profiles.json"
    monkeypatch.setenv("BOT_FOOTAGE_DIR", str(clips))
    monkeypatch.setenv("BOT_PROFILE_CACHE", str(cache))
    measured = []

    def fake_measure(path):
        measured.append(path.name)
        return ((0.1, 0.5), (0.2, 0.25))

    monkeypatch.setattr(footage, "_measure", fake_measure)
    for first_run in (True, False):
        footage.profile.cache_clear()
        monkeypatch.setattr(footage, "_DISK", None)
        monkeypatch.setattr(footage, "_DISK_NEW", False)
        assert footage.profile("halls_walk_01") == ((0.1, 0.5), (0.2, 0.25))
        footage.save_profiles()
        assert (cache.parent / "dirty").exists() is first_run      # saved (and marked) only when something is new
    assert measured == ["halls_walk_01.mp4"]                          # the second run read the cache
    # a different file under the same name (another release) is measured again; malformed entries are dropped
    cache.write_text('{"halls_walk_01:100": [[2, 0.1]], "x": "y"}', encoding="utf-8")
    footage.profile.cache_clear()
    monkeypatch.setattr(footage, "_DISK", None)
    assert footage.profile("halls_walk_01") == ((0.1, 0.5), (0.2, 0.25)) and len(measured) == 2
    footage.profile.cache_clear()


def test_single_pass_lays_the_text_frames_over_the_graded_picture_in_one_encode(tmp_path, monkeypatch):
    import subprocess
    import tempfile
    from pathlib import Path

    from PIL import Image

    tiny_clip(tmp_path / "halls.mp4", seconds=4, w=192, h=108, audio=False)
    monkeypatch.setenv("BOT_FOOTAGE_DIR", str(tmp_path))
    shot = {"kind": "clip", "clip": "halls", "from": 0.0, "speed": 1.0, "pan": 0.0, "zoom": [1.0, 1.0],
            "caption": None, "fx": []}
    sb = {"kind": "long", "grade": "long", "dream_ids": ["halls"], "music": {"bpm": 80, "seed": 3, "mood": "soft"},
          "shots": [{**shot, "seconds": 1.0}, {**shot, "seconds": 1.0}]}
    tmp = tempfile.TemporaryDirectory()
    part = Path(tmp.name) / "overlay.part0"
    part.mkdir()
    for n in range(5, 11):                       # frames 5-10: an opaque red box top-left, transparent elsewhere
        im = Image.new("RGBA", (192, 108), (0, 0, 0, 0))
        im.paste((255, 0, 0, 255), (0, 0, 60, 40))
        im.save(part / f"element-{n}.png")
    frames = overlay.TextFrames([part], [[5, 10]], 24, tmp)
    out = tmp_path / "video.mp4"
    Renderer(sb, 192, 108, 24, text=False).render(out, text_frames=frames, x264=overlay.FINAL_X264)
    frames.close()
    raw = subprocess.run([config.ffmpeg_exe(), "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "rgb24",
                          "-"], capture_output=True, check=True).stdout
    video = np.frombuffer(raw, np.uint8).reshape(-1, 108, 192, 3).astype(int)
    assert len(video) == 48                                              # every frame, once
    red = lambda f: f[5:35, 5:55, 0].mean() > 180 and f[5:35, 5:55, 1].mean() < 90   # noqa: E731
    assert [i for i in range(48) if red(video[i])] == list(range(5, 11))  # the text sits exactly on its frames
    probe = qa._probe(out)
    assert probe["audio"] and probe["pix_fmt"] == "yuv420p"
