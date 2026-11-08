"""Render smoke tests at tiny size: animated (seed sprites) and the drawn look / music building blocks."""
import numpy as np
from PIL import Image

from tests.conftest import plan_legacy
from bot import look, music, planner, qa, sprites
from bot.render import Renderer


def test_animated_short_renders_and_passes_qa(tmp_path):
    _, sb = plan_legacy("animated", {}, seed=11)
    r = Renderer(sb, 216, 384, 24, sprite_dir=None)  # seed sprites: no Blender needed
    out = r.render(tmp_path / "v.mp4", preset="ultrafast", crf=30)
    rep = qa.check(out, 216, 384)
    assert rep.ok, rep.as_text()
    assert (tmp_path / "v_sheet.jpg").exists()


def test_impact_frame_is_three_tone():
    f = (np.random.default_rng(1).random((64, 36, 3)) * 255).astype(np.uint8)
    out = look.impact_frame(f, invert=False)
    assert len({tuple(p) for p in out.reshape(-1, 3)}) <= 3


def test_every_action_has_drawings_from_seed():
    for ch in sprites.CHARACTERS:
        for act in sprites.ACTIONS:
            ims = sprites.drawings(ch, act, None)
            assert ims and all(isinstance(i, Image.Image) and i.mode == "RGBA" for i in ims)


def test_shadow_tint_keeps_body_and_softens_black():
    im = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    im.paste((0, 0, 0, 200), (0, 30, 40, 40))      # shadow-catcher pixels
    im.paste((250, 180, 200, 255), (10, 0, 30, 30))  # body
    out = np.asarray(look.tint_shadow(im))
    assert tuple(out[5, 20, :3]) == (250, 180, 200)
    assert out[35, 5, 3] < 200 and out[35, 5, :3].max() > 20


def test_music_drops_to_silence_then_hits():
    sr = music.SR
    dropped = music.score(6.0, 90, 3, "tense", hits=[], drop_at=2.0)
    assert dropped.shape[1] == 2
    assert np.abs(dropped[int(2.2 * sr):int(4.0 * sr), 0]).max() < 0.01      # dead silence after the drop
    assert np.abs(dropped[int(0.5 * sr):int(1.9 * sr), 0]).max() > 0.1       # music before it
    hit = music.score(6.0, 90, 3, "tense", hits=[3.0], drop_at=2.0)
    assert np.abs(hit[int(3.0 * sr):int(3.4 * sr), 0]).max() > 0.2          # the impact lands


def test_transitions_see_the_previous_shot():
    """Regression: fades/whips need the last frame of the shot before (was silently dropped once)."""
    _, sb = plan_legacy("animated", {}, seed=11)
    r = Renderer(sb, 108, 192, 24, sprite_dir=None)
    first_cut = int(round(float(sb["shots"][0]["seconds"]) * 24)) + 1
    for fi in range(first_cut + 1):
        r.frame(fi)
    assert r.prev_last is not None and r.last_k == 1
    r.close()
