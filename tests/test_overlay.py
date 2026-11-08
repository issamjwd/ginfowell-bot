"""Remotion text overlay: props building, the clean render, the shared style file, and the burned-caption fallback."""
import json
import os
import re
import shutil

import numpy as np
import pytest

from tests.conftest import plan_legacy
from bot import config, fonts, overlay, pipeline, planner, qa, style
from bot.render import Renderer

ROOT = config.ROOT


def _sb(**over):
    sb = {
        "dream_ids": ["hills"],
        "lines": {"hook": "nobody else is here. probably."},
        "thumbnail": {"dream": "hills", "text": "did you see it?"},
        "shots": [
            {"kind": "clip", "seconds": 4.0, "caption": "nobody else is here. probably.", "fx": []},
            {"kind": "clip", "seconds": 2.0, "caption": "did you see it?", "fx": ["impact"]},
            {"kind": "scene", "seconds": 3.0, "caption": "it hears running.", "fx": []},
            {"kind": "clip", "seconds": 2.0, "caption": None, "fx": ["shake"]},
            {"kind": "card", "seconds": 2.5, "big": "liminality", "caption": "fall asleep. remember. wake up.", "fx": []},
            {"kind": "card", "seconds": 2.0, "end": True, "caption": None, "fx": []},
        ],
    }
    sb.update(over)
    return sb


def test_props_follow_the_storyboard_timeline():
    p = overlay.props_from_storyboard(_sb(), 1080, 1920, 24)
    assert p["durationInFrames"] == round(15.5 * 24)
    assert [c["text"] for c in p["captions"]] == ["nobody else is here. probably.", "did you see it?", "it hears running."]
    assert [c["style"] for c in p["captions"]] == ["drift", "glitch", "drift"]       # scenes get captions too
    assert p["captions"][1]["start"] == 4.0 and p["captions"][1]["end"] == 6.0
    assert p["outro"] == {"text": "fall asleep. remember. wake up.", "start": 11.0}
    assert p["endCard"]["seconds"] == 2.0                                             # matches the end shot
    assert p["freezeAt"] == pytest.approx(9.0 - 0.4)                                  # end of the last calm shot


def test_hook_title_never_repeats_a_caption():
    p = overlay.props_from_storyboard(_sb())
    said = {c["text"] for c in p["captions"]}
    assert p["hook"]["title"] not in said
    assert p["hook"]["title"] == "the hills"          # thumbnail AND hook line are both captions: the dream's name
    assert p["hook"]["label"] == ""                   # and the label never doubles the title


def test_hook_uses_the_thumbnail_line_when_it_is_new():
    p = overlay.props_from_storyboard(_sb(thumbnail={"dream": "hills", "text": "Don't Stare."}))
    assert p["hook"]["title"] == "Don't Stare."
    assert p["hook"]["label"] == "the hills"


def test_dedupe_ignores_case_and_punctuation():
    p = overlay.props_from_storyboard(_sb(thumbnail={"dream": "hills", "text": "DID you see it"}))
    assert p["hook"]["title"] == "the hills"


def test_hook_fills_or_skips_template_slots():
    sb = _sb(thumbnail={"dream": "hills", "text": "did you see it?"}, lines={"hook": "this is {dream}."})
    p = overlay.props_from_storyboard(sb)
    assert p["hook"]["title"] == "this is the hills."
    sb["shots"][0]["caption"] = "this is the hills."              # the filled line is also a caption: dream name
    p = overlay.props_from_storyboard(sb)
    assert p["hook"]["title"] == "the hills"
    p = overlay.props_from_storyboard(_sb(thumbnail={"dream": "hills", "text": "a {monster}"}, lines={}))
    assert "{" not in p["hook"]["title"]


def test_first_caption_waits_for_the_hook():
    p = overlay.props_from_storyboard(_sb())
    assert p["captions"][0]["start"] == overlay.HOOK_SECONDS
    assert p["captions"][0]["end"] == 4.0


@pytest.mark.parametrize("first", [1.5, 2.526, 3.0, 4.0])
def test_hook_and_first_caption_never_overlap(first):
    sb = _sb(thumbnail={"dream": "hills", "text": "you've been here"})
    sb["shots"][0]["seconds"] = first
    p = overlay.props_from_storyboard(sb)
    c0 = p["captions"][0]
    assert c0["end"] - c0["start"] >= overlay.CAPTION_MIN - 1e-9
    if p["hook"]:
        assert p["hook"]["until"] <= c0["start"] + 1e-9 and p["hook"]["until"] >= overlay.HOOK_MIN
    else:
        assert first - overlay.CAPTION_MIN < overlay.HOOK_MIN


def test_props_are_json_and_low_res_keeps_the_timeline():
    p = overlay.props_from_storyboard(_sb(), 540, 960, 24)
    assert (p["width"], p["height"]) == (540, 960)
    assert json.loads(json.dumps(p)) == p


def test_style_file_is_the_one_source():
    s = style.load()
    assert style.rgb("accent") == (0xF2, 0xA7, 0xC3)
    assert set(s["colours"]) == {"ink", "paper", "sub", "accent"}           # one accent
    assert len(s["fonts"]) == 2                                              # two fonts
    assert fonts.sans(40).path.endswith(s["fonts"]["body"]["files"]["700"])
    assert style.head_font().name in s["fonts"]["head"]["files"]
    js = (ROOT / "render" / "render.mjs").read_text(encoding="utf-8")
    assert "style.json" in js
    jsx = (ROOT / "render" / "src" / "Short.jsx").read_text(encoding="utf-8")
    # the JSX only has a fallback copy of the palette; it must match style.json exactly
    for name, hexv in s["colours"].items():
        assert re.search(rf"{name}: '{hexv}'", jsx), name
    assert "remotion.media" not in js and "ensureBrowser" not in js          # never downloads a browser
    assert not re.search(r"https?://|@import|googleapis", jsx)              # nothing loads from the network


def test_overlay_off_or_missing_tools_is_unavailable(monkeypatch):
    assert overlay.available("off") == (False, "BOT_OVERLAY=off")
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    ok, why = overlay.available("auto")
    assert not ok and "node" in why


def test_browser_env_must_point_at_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("REMOTION_BROWSER_EXECUTABLE", str(tmp_path / "nope"))
    assert overlay.find_browser() is None
    exe = tmp_path / "chrome"
    exe.write_bytes(b"x")
    monkeypatch.setenv("REMOTION_BROWSER_EXECUTABLE", str(exe))
    assert overlay.find_browser() == str(exe)


def test_bot_overlay_setting_is_checked():
    assert config.Settings.from_env({}).overlay == "auto"
    assert config.Settings.from_env({"BOT_OVERLAY": "off"}).overlay == "off"
    with pytest.raises(config.ConfigError):
        config.Settings.from_env({"BOT_OVERLAY": "maybe"})


def test_clean_render_has_no_burned_text():
    _, sb = plan_legacy("animated", {}, seed=11)
    k = next(i for i, s in enumerate(sb["shots"]) if s["kind"] != "card" and s.get("caption"))
    fi = int(round((sum(float(s["seconds"]) for s in sb["shots"][:k]) + 0.9) * 24))
    burned = Renderer(sb, 216, 384, 24, sprite_dir=None)
    clean = Renderer(sb, 216, 384, 24, sprite_dir=None, text=False)
    for i in range(fi):
        burned.frame(i)
        clean.frame(i)
    a, b = burned.frame(fi).astype(int), clean.frame(fi).astype(int)
    burned.close()
    clean.close()
    band = slice(int(384 * 0.52), int(384 * 0.68))                         # the caption band (style.json y 0.6)
    assert np.abs(a[band] - b[band]).mean() > 3                             # the caption is only in the burned one
    assert np.abs(a[:150] - b[:150]).mean() < 1                             # the rest of the picture is identical


def _small_episode():
    _, sb = plan_legacy("animated", {}, seed=11)
    return sb


def test_overlay_failure_falls_back_to_burned_captions(monkeypatch, tmp_path):
    monkeypatch.setattr(overlay, "available", lambda mode="auto": (True, "ok"))

    def boom(*_a, **_k):
        raise overlay.OverlayError("chrome crashed")

    monkeypatch.setattr(overlay, "render", boom)
    video, rep = pipeline.render_video(_small_episode(), tmp_path, 216, 384, None, {})
    assert rep.ok, rep.as_text()
    assert video == tmp_path / "video.mp4" and video.exists()
    assert rep.info["text"].startswith("burned captions") and "chrome crashed" in rep.info["text"]
    assert not (tmp_path / "clean.mp4").exists()
    assert (tmp_path / "video_sheet.jpg").exists()


def test_overlay_output_failing_qa_also_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(overlay, "available", lambda mode="auto": (True, "ok"))
    monkeypatch.setattr(overlay, "render", lambda clean, sb, out, *a, **k: out.write_bytes(b"not a video" * 10))
    video, rep = pipeline.render_video(_small_episode(), tmp_path, 216, 384, None, {})
    assert rep.ok and rep.info["text"].startswith("burned captions (overlay: overlay video failed QA")


def test_overlay_success_keeps_the_overlay_video(monkeypatch, tmp_path):
    seen = {}

    def fake_render(clean, sb, out, *a, **k):
        seen["clean"] = clean
        shutil.copyfile(clean, out)  # stand-in for node render.mjs + the audio mux
        return out

    monkeypatch.setattr(overlay, "available", lambda mode="auto": (True, "ok"))
    monkeypatch.setattr(overlay, "render", fake_render)
    video, rep = pipeline.render_video(_small_episode(), tmp_path, 216, 384, None, {})
    assert rep.ok and rep.info["text"] == "remotion overlay"
    assert seen["clean"].name == "clean.mp4" and not seen["clean"].exists()     # temp clean render removed
    assert (tmp_path / "video_sheet.jpg").exists() and not (tmp_path / "clean_sheet.jpg").exists()


def test_overlay_off_goes_straight_to_burned(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(overlay, "render", lambda *a, **k: called.append(1))
    _, rep = pipeline.render_video(_small_episode(), tmp_path, 216, 384, None, {}, overlay_mode="off")
    assert rep.ok and not called and "BOT_OVERLAY=off" in rep.info["text"]


def test_child_env_hides_keys_and_scrub_redacts(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-0123456789")
    monkeypatch.setenv("AI_VIDEO_API_KEY", "vid-test-0123456789")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    env = overlay._child_env()
    assert "ANTHROPIC_API_KEY" not in env and "AI_VIDEO_API_KEY" not in env
    assert "PATH" in env and "REMOTION_BROWSER_EXECUTABLE" in env
    assert "sk-test" not in overlay._scrub("boom sk-test-0123456789 vid-test-0123456789")


def test_caption_emphasis_picks_the_word_by_meaning():
    def word(t):
        i = overlay.emphasis(t)
        return t.split()[i].strip(".,?!") if i >= 0 else None
    assert word("don't run. it hears running.") == "running"          # the threat, where the line lands
    assert word("somewhere, a clock is ticking.") == "clock"          # a concrete thing beats a filler verb
    assert word("it's behind you.") == "behind"
    assert word("rule 1: keep the lamps on.") == "lamps"
    assert word("pov: the halls never end") == "halls"
    # owner: never filler, pronouns or common verbs ("see", "remember", "here"); none when no word earns it
    for line in ("did you see it?", "you remember this place.", "it was here the whole time.", "walk slowly.",
                 "find what you forgot.", "wake up.", "it is in the"):
        assert word(line) is None, line
    for w in ("see", "remember", "here", "you", "it", "was", "look", "go"):
        assert w in overlay.STOP and w not in overlay.CHARGED


def test_captions_sync_to_the_picture_fx():
    sb = _sb()
    sb["shots"][1]["fx"] = ["impact", "zoom_punch"]
    sb["shots"][2]["transition"] = "whip"
    p = overlay.props_from_storyboard(sb, 1080, 1920, 24)
    c0, c1, c2 = p["captions"]
    assert c0["hits"] == [] and c0["enter"] == "cut" and c0["shake"] == []   # starts after the hook, not on a cut
    assert c1["hits"] == ["impact", "zoom_punch"] and c1["enter"] == "cut"
    assert c2["enter"] == "whip" and c2["hits"] == []
    # the impact shake track is the renderer's own (content moves by -dx, -dy) and decays to nothing
    from bot import fx
    dx, dy = fx.shake_offset(0.0, 1, 34.0, 4.0)
    assert c1["shake"][0] == [-dx, -dy]
    assert 0 < len(c1["shake"]) <= 48 and c1["shake"][-1] != [0, 0]
    assert all(abs(x) <= 34 and abs(y) <= 34 for x, y in c1["shake"])
    assert json.loads(json.dumps(p)) == p


def test_late_caption_shake_track_starts_at_its_own_frame():
    sb = _sb()
    sb["shots"][0]["fx"] = ["shake"]                                         # first caption waits for the hook
    p = overlay.props_from_storyboard(sb, 1080, 1920, 24)
    from bot import fx
    skip = int(round(p["captions"][0]["start"] * 24))
    dx, dy = fx.shake_offset(skip / 24, 0, 12.0, 0.8)
    assert p["captions"][0]["shake"][0] == [-dx, -dy]
    assert p["captions"][0]["hits"] == []


def test_caption_frames_follow_the_renderer_on_a_fractional_cut():
    sb = _sb()
    sb["shots"][0]["seconds"] = 4.01                                         # shot 1 starts at 4.01 s = frame 96.24
    sb["shots"][1]["fx"] = ["impact"]
    p = overlay.props_from_storyboard(sb, 1080, 1920, 24)
    c1 = p["captions"][1]
    assert round(c1["start"] * 24) == 97                                     # bot/render.py starts it on frame 97
    assert c1["tl0"] == pytest.approx(97 / 24 - 4.01, abs=1e-5)
    from bot import fx
    dx, dy = fx.shake_offset(97 / 24 - 4.01, 1, 34.0, 4.0)
    assert c1["shake"][0] == [-dx, -dy]


def test_overlay_gets_the_score_beat_and_one_grid():
    sb = _sb(music={"bpm": 92, "seed": 1})
    p = overlay.props_from_storyboard(sb)
    assert p["beat"] == pytest.approx(60 / 92, abs=1e-5)
    assert overlay.props_from_storyboard(_sb())["beat"] > 0                  # no music block: a sane default
    assert style.caption()["left"] + style.caption()["max_width"] * 1080 <= 1080 - 120   # clear of the button column


def test_whip_and_punch_captions_carry_the_renderer_offset():
    sb = _sb()
    sb["shots"][1]["seconds"] = 2.01
    sb["shots"][2]["transition"] = "whip"                                    # shot 2 starts at 6.01 s = frame 144.24
    sb["shots"][2]["fx"] = ["zoom_punch"]
    p = overlay.props_from_storyboard(sb, 1080, 1920, 24)
    c2 = p["captions"][2]
    assert c2["enter"] == "whip" and c2["hits"] == ["zoom_punch"] and c2["shake"] == []
    assert round(c2["start"] * 24) == 145 and c2["tl0"] == pytest.approx(145 / 24 - 6.01, abs=1e-5)
    hook_end = round(p["hook"]["until"] * 24)
    assert hook_end <= round(p["captions"][0]["start"] * 24)                 # hook and first caption never share a frame



def test_qa_grabs_the_sheet_frames_in_its_own_decode_with_or_without_audio(tmp_path):
    from tests.conftest import tiny_clip
    for audio in (True, False):
        v = tiny_clip(tmp_path / f"v{audio}.mp4", seconds=10, w=192, h=108, audio=audio)
        folder = tmp_path / f"g{audio}"
        folder.mkdir()
        rep = qa.check(v, 192, 108, grab=([30, 10, 30, 200], 64, folder))
        assert sorted(f.name for f in folder.glob("*.png")) == ["1.png", "2.png", "3.png"]
        assert rep.checks["size"] == "ok" and rep.checks["black runs"] == "ok"
        assert (rep.checks["audio level"] == "ok") is audio
        overlay.sheet_from_folder([30, 10, 30, 200], folder, tmp_path / f"s{audio}.jpg")
        assert (tmp_path / f"s{audio}.jpg").exists()


def test_a_failed_single_pass_falls_back_to_burned_captions(monkeypatch, tmp_path):
    monkeypatch.setattr(overlay, "available", lambda mode="auto": (True, "ok"))
    monkeypatch.setattr(overlay, "single_pass_ok", lambda *a, **k: True)

    def no_chrome(*a, **k):
        raise overlay.OverlayError("chrome crashed")
    monkeypatch.setattr(overlay, "render_text_frames", no_chrome)
    video, rep = pipeline.render_video(_small_episode(), tmp_path, 216, 384, None, {})
    assert rep.ok and rep.info["text"].startswith("burned captions") and "chrome crashed" in rep.info["text"]
    assert video.exists()
