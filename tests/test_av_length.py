"""Audio must end with the picture (APEX review 2026-11-10: a Short's audio ran 115 ms past its video, 12.948 s vs
12.833 s; the A/V-duration gate is <= 50 ms). render.fit_audio_to_video re-muxes (stream copy) to the picture."""
from __future__ import annotations

import json
import subprocess

from bot import config, render
from tests.conftest import ffmpeg


def _durations(path):
    out = subprocess.run([config.ffprobe_exe(), "-v", "error", "-show_entries", "stream=codec_type,duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: float(s["duration"]) for s in json.loads(out)["streams"]}


def _clip(path, video_s, audio_s):
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=216x384:rate=24:duration={video_s}", "-f", "lavfi", "-i",
           f"sine=frequency=330:duration={audio_s}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", "48000", str(path))
    return path


def test_long_audio_is_cut_to_the_picture(tmp_path):
    v = _clip(tmp_path / "video.mp4", 12.833333, 12.948)
    assert _durations(v)["audio"] - _durations(v)["video"] > 0.1          # the defect, as measured
    pic = v.read_bytes()
    render.fit_audio_to_video(v)
    d = _durations(v)
    assert abs(d["audio"] - d["video"]) <= 0.05, d
    assert v.read_bytes() != pic and not list(tmp_path.glob("*.tmp*"))


def test_matching_file_is_left_alone(tmp_path):
    v = _clip(tmp_path / "video.mp4", 5, 5)
    before = v.read_bytes()
    render.fit_audio_to_video(v)
    assert v.read_bytes() == before


def test_unreadable_file_is_left_for_qa(tmp_path):
    bad = tmp_path / "video.mp4"
    bad.write_bytes(b"not a video")
    assert render.fit_audio_to_video(bad) == bad and bad.read_bytes() == b"not a video"
