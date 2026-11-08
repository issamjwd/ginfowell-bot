import copy

import pytest

from bot import planner, storyboard
from bot.storyboard import StoryboardError, validate
from tests.conftest import FAKE_CLIPS, plan_legacy


def _sb(fmt="animated", seed=3):
    if fmt == "animated":  # the retired per-shot-caption templates (still validated: the long video uses the rules)
        return plan_legacy(fmt, FAKE_CLIPS, seed=seed)[1]
    _, sb = planner.plan([], set(), fmt, FAKE_CLIPS, seed=seed)
    return sb


def test_planned_storyboards_are_valid_for_every_template():
    seen = set()
    for seed in range(60):
        _, sb = planner.plan([], set(), "auto", FAKE_CLIPS, seed=seed)
        validate(sb, footage=set(FAKE_CLIPS))
        seen.add(sb["template"])
    assert len(seen) >= 6


def test_total_length_stays_under_the_shorts_limit():
    for seed in range(30):
        sb = _sb("auto", seed)
        assert 8 <= storyboard.total_seconds(sb) <= storyboard.MAX_SECONDS


def _first_caption_shot(sb):
    return next(s for s in sb["shots"] if s.get("caption"))


@pytest.mark.parametrize("caption, reason", [
    ("Don't Stare.", "lowercase"),
    ("thirteen rooms to escape", "dreams"),
    ("a room with no doors", "dreams"),
    ("blood on the carpet", "gory"),
    ("you are here ✦", "unsafe characters"),
    ("x" * 61, "longer than"),
])
def test_caption_rules(caption, reason):
    sb = _sb()
    _first_caption_shot(sb)["caption"] = caption
    with pytest.raises(StoryboardError) as e:
        validate(sb)
    assert any(reason in err for err in e.value.errors)


def test_proper_names_with_room_are_allowed():
    sb = _sb()
    _first_caption_shot(sb)["caption"] = "you fell asleep in the waiting room."
    validate(sb)
    _first_caption_shot(sb)["caption"] = "this is the room of windows."
    validate(sb)


def test_too_long_is_rejected():
    sb = _sb()
    sb["shots"][0]["seconds"] = 15.0
    sb["shots"][1]["seconds"] = 15.0
    sb["shots"][2]["seconds"] = 15.0
    sb["shots"].append(copy.deepcopy(sb["shots"][0]))
    with pytest.raises(StoryboardError, match="total length"):
        validate(sb)


def test_flashes_too_close_are_rejected():
    sb = _sb()
    sb["shots"][0]["fx"] = ["impact"]
    sb["shots"][0]["seconds"] = 0.6
    sb["shots"][1]["fx"] = ["flash"]
    with pytest.raises(StoryboardError, match="photosensitivity"):
        validate(sb)


def test_unknown_dream_character_and_clip_are_rejected():
    sb = _sb("animated")
    scene = next(s for s in sb["shots"] if s["kind"] == "scene")
    scene["dream"] = "basement"
    scene["actors"][0]["character"] = "Clown"
    with pytest.raises(StoryboardError) as e:
        validate(sb)
    assert any("unknown dream" in x for x in e.value.errors)
    assert any("character" in x for x in e.value.errors)

    fsb = _sb("footage")
    clip = next(s for s in fsb["shots"] if s["kind"] == "clip")
    clip["clip"] = "nope"
    with pytest.raises(StoryboardError, match="unknown footage"):
        validate(fsb, footage=set(FAKE_CLIPS))


def test_clip_shots_are_not_allowed_in_animated_episodes():
    sb = _sb("animated")
    sb["shots"][0] = {"kind": "clip", "seconds": 2.0, "clip": "halls", "fx": [], "caption": None}
    with pytest.raises(StoryboardError, match="footage episodes"):
        validate(sb)


def test_non_object_and_bad_types():
    with pytest.raises(StoryboardError):
        validate([])
    sb = _sb()
    sb["music"]["bpm"] = "fast"
    sb["shots"][0]["fx"] = ["explode"]
    with pytest.raises(StoryboardError) as e:
        validate(sb)
    assert len(e.value.errors) >= 2
