"""Fonts (shipped TTFs, fail-closed pinning), QA probe error handling + ffprobe parsing, ffmpeg errors surfaced."""
import hashlib
import json
import subprocess

import pytest

from bot import config, fonts, planner, qa
from bot.render import Renderer
from tests.conftest import tiny_clip, plan_legacy


# ---------------------------------------------------------------- fonts
def test_shipped_fonts_match_their_hashes():
    for name, prefix in fonts.SHIPPED.items():
        assert hashlib.sha256((config.FONTS / name).read_bytes()).hexdigest().startswith(prefix), name


def test_caption_font_is_montserrat_not_pillows_default():
    f = fonts.sans(40)
    assert f.getname()[0].startswith("Montserrat")
    assert fonts.sans(40, "medium").getname()[0].startswith("Montserrat")


def test_serif_falls_back_to_shipped_merriweather(monkeypatch):
    monkeypatch.delenv("LOCAL_SERIF_FONT", raising=False)
    monkeypatch.delenv("LOCAL_SERIF_FONT_ITALIC", raising=False)
    fonts.serif.cache_clear()
    if (config.FONTS / "EBGaramond.ttf").exists():
        pytest.skip("EB Garamond is present here")
    assert fonts.serif(40).getname()[0] == "Merriweather"
    assert fonts.serif(40, italic=True).getname()[1] == "Italic"
    fonts.serif.cache_clear()


def test_unpinned_lock_ensure_is_a_no_op():
    lock = json.loads(fonts.LOCK.read_text())
    if fonts.is_pinned(lock):
        pytest.skip("lock already pinned")
    assert fonts.ensure() == []


def test_pin_fails_without_writing_if_any_font_fails(monkeypatch, tmp_path):
    lock = tmp_path / "fonts.lock.json"
    lock.write_text(fonts.LOCK.read_text())
    monkeypatch.setattr(fonts, "LOCK", lock)
    good_font = bytes([0, 1, 0, 0]) + b"\0" * 20_000
    calls = []

    def fake_get(url, timeout=60):
        calls.append(url)
        if "api.github.com" in url:
            return json.dumps({"sha": "a" * 40}).encode()
        if "Italic" in url:
            raise OSError("404")
        return good_font

    monkeypatch.setattr(fonts, "_get", fake_get)
    before = lock.read_text()
    with pytest.raises(fonts.FontError):
        fonts.pin()
    assert lock.read_text() == before


def test_pin_rejects_non_font_bytes(monkeypatch, tmp_path):
    lock = tmp_path / "fonts.lock.json"
    lock.write_text(fonts.LOCK.read_text())
    monkeypatch.setattr(fonts, "LOCK", lock)
    monkeypatch.setattr(fonts, "_get", lambda url, timeout=60: json.dumps({"sha": "b" * 40}).encode()
                        if "api.github.com" in url else b"<html>" + b"x" * 20_000)
    with pytest.raises(fonts.FontError, match="not a font"):
        fonts.pin()


def test_font_fetch_refuses_other_hosts():
    with pytest.raises(fonts.FontError):
        fonts._get("https://evil.example.com/x.ttf")


# ---------------------------------------------------------------- QA
FFPROBE_JSON = {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 216,
                             "height": 384}, {"codec_type": "audio", "codec_name": "aac"}],
                "format": {"duration": "10.000000"}}


def test_ffprobe_branch_parses_json(monkeypatch, tmp_path):
    clip = tiny_clip(tmp_path / "ok.mp4")
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[0] == "ffprobe-fake":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(FFPROBE_JSON), "")
        return real_run(cmd, **kw)

    monkeypatch.setattr(config, "ffprobe_exe", lambda: "ffprobe-fake")
    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    r = qa.check(clip, 216, 384)
    assert r.ok, r.as_text()
    assert r.info["seconds"] == 10.0 and r.info["audio"] is True


def test_ffprobe_failure_is_a_failed_report_not_a_crash(monkeypatch, tmp_path):
    clip = tiny_clip(tmp_path / "ok.mp4")
    monkeypatch.setattr(config, "ffprobe_exe", lambda: "ffprobe-fake")
    monkeypatch.setattr(qa.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "bad file"))
    r = qa.check(clip, 216, 384)
    assert not r.ok and "probe" in r.checks


def test_analysis_failure_is_a_failed_report(monkeypatch, tmp_path):
    clip = tiny_clip(tmp_path / "ok.mp4")
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if "-af" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "decoder exploded")
        return real_run(cmd, **kw)

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    r = qa.check(clip, 216, 384)
    assert not r.ok and "analysis" in r.checks


def test_corrupt_file_fails_qa(tmp_path):
    p = tmp_path / "bad.mp4"
    p.write_bytes(b"\0" * 20_000)
    assert not qa.check(p, 216, 384).ok


@pytest.mark.skipif(config.ffprobe_exe() is None, reason="no ffprobe on this machine (CI has it)")
def test_real_ffprobe_on_a_real_clip(tmp_path):
    r = qa.check(tiny_clip(tmp_path / "ok.mp4"), 216, 384)
    assert r.ok, r.as_text()


# ---------------------------------------------------------------- render
def test_ffmpeg_failure_reports_ffmpegs_own_error(tmp_path):
    _, sb = plan_legacy("animated", {}, seed=11)
    with pytest.raises(RuntimeError) as e:
        Renderer(sb, 108, 192, 24).render(tmp_path / "v.mp4", preset="no_such_preset")
    msg = str(e.value).lower()
    assert "rc=" in msg and "preset" in msg  # ffmpeg's own complaint, not a bare BrokenPipeError
