"""Narrator (bot/voice.py): line extraction, text cleanup, and that it never breaks an edit. Offline, no model."""
import numpy as np

from bot import voice


def test_lines_follow_the_hook_and_merge_chunks_of_one_line():
    sb = {"hook": {"title": "pov: you fell asleep", "seconds": 2.0},
          "captions": [{"text": "the halls", "start": 2.4, "end": 3.2}, {"text": "go on.", "start": 3.2, "end": 4.0},
                       {"text": "something waits", "start": 5.0, "end": 6.5}]}
    assert voice.lines(sb) == [(0.15, 2.0, "p o v, you fell asleep"), (2.4, 4.0, "the halls go on."),
                               (5.0, 6.5, "something waits")]


def test_spoken_text_has_no_tags():
    assert voice.spoken("it was there #shorts ...") == "it was there"


def test_off_or_missing_model_leaves_the_mix_unchanged(monkeypatch, tmp_path):
    st = np.random.default_rng(1).uniform(-0.5, 0.5, (44100, 2)).astype(np.float32)
    monkeypatch.setattr(voice, "DIR", tmp_path)                      # no model files
    assert voice.mix(st, {"hook": {"title": "hi", "seconds": 1}}, 44100) is st
    monkeypatch.setenv("BOT_VOICE", "off")
    assert voice.enabled() is False


def test_a_tts_error_never_costs_the_episode(monkeypatch):
    st = np.zeros((44100, 2), np.float32)
    monkeypatch.setattr(voice, "enabled", lambda: True)
    monkeypatch.setattr(voice, "_say", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert voice.mix(st, {"hook": {"title": "hi", "seconds": 1}}, 44100) is st
