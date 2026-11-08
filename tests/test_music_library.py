"""Real-track music (bot/music.py library / assign / library_bed). Offline: tiny tracks made with ffmpeg."""
import json

import numpy as np
import pytest

from bot import config, music, storyboard


@pytest.fixture
def pack(tmp_path, monkeypatch):
    import subprocess
    tracks = []
    for i, mood in enumerate(["tense", "tense", "tense", "soft"]):
        f = tmp_path / f"t{i}.opus"
        subprocess.run([config.ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={200 + 100 * i}:duration=40", "-c:a", "libopus", "-b:a", "48k", str(f)],
                       check=True)
        tracks.append({"file": f.name, "title": f"Track {i}", "mood": mood})
    lib = tmp_path / "music.json"
    lib.write_text(json.dumps({"credit": "\"{title}\" Kevin MacLeod (incompetech.com). CC BY 4.0.", "tracks": tracks}))
    monkeypatch.setattr(music, "LIBRARY", lib)
    monkeypatch.setattr(music, "MUSIC_DIR", tmp_path)
    return tmp_path


def test_no_track_repeats_until_every_other_one_played(pack):
    hist, got = [], []
    for seed in range(6):
        sb = {"music": {"mood": "tense", "seed": seed}, "description": "d"}
        music.assign(sb, hist, seed)
        got.append(sb["music"]["track"])
        hist.append({"music_track": sb["music"]["track"]})
    assert len(set(got[:3])) == 3 and len(set(got[3:6])) == 3
    assert "Kevin MacLeod" in sb["description"] and not storyboard._text_problems("d", sb["description"], 4500, False)


def test_no_pack_leaves_the_storyboard_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(music, "MUSIC_DIR", tmp_path)
    sb = {"music": {"mood": "tense", "seed": 1}, "description": "d"}
    assert music.assign(sb, [], 1) == {"music": {"mood": "tense", "seed": 1}, "description": "d"}


def test_bed_has_the_length_fades_and_a_fresh_cut(pack):
    a = music.library_bed(12, ["t0.opus"], 1)
    b = music.library_bed(12, ["t0.opus"], 2, drop_at=5.0, resume_at=6.0)
    assert a.shape == (12 * music.SR, 2) and np.abs(a).max() <= 0.86
    assert np.abs(a[:100]).max() < 0.05                                   # fades in
    assert np.abs(b[int(5.5 * music.SR)]).max() < 1e-3                    # the drop is silent
    long_ = music.library_bed(70, ["t0.opus", "t1.opus"], 3)            # longer than one track: crossfaded in
    assert long_.shape[0] == 70 * music.SR
