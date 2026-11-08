from bot import qa
from tests.conftest import tiny_clip

W, H = 216, 384


def test_good_clip_passes(tmp_path):
    r = qa.check(tiny_clip(tmp_path / "ok.mp4"), W, H)
    assert r.ok, r.as_text()


def test_wrong_size_fails(tmp_path):
    r = qa.check(tiny_clip(tmp_path / "wide.mp4", w=384, h=216), W, H)
    assert not r.ok and r.checks["size"] != "ok"


def test_no_audio_fails(tmp_path):
    r = qa.check(tiny_clip(tmp_path / "mute.mp4", audio=False), W, H)
    assert not r.ok and r.checks["audio stream"] != "ok"


def test_black_run_fails(tmp_path):
    r = qa.check(tiny_clip(tmp_path / "black.mp4", black=(3.0, 2.0)), W, H)
    assert not r.ok and r.checks["black runs"] != "ok"


def test_frozen_run_fails(tmp_path):
    r = qa.check(tiny_clip(tmp_path / "frozen.mp4", freeze=(2.0, 4.0)), W, H)
    assert not r.ok and r.checks["frozen runs"] != "ok"


def test_too_long_fails(tmp_path):
    r = qa.check(tiny_clip(tmp_path / "long.mp4", seconds=62), W, H)
    assert not r.ok and r.checks["length"] != "ok"


def test_too_short_fails(tmp_path):
    r = qa.check(tiny_clip(tmp_path / "short.mp4", seconds=4), W, H)
    assert not r.ok and r.checks["length"] != "ok"


def test_missing_file_fails(tmp_path):
    assert not qa.check(tmp_path / "nope.mp4", W, H).ok
