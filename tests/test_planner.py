import pytest

from bot import bank, planner
from bot.state import fingerprint
from tests.conftest import FAKE_CLIPS, plan_legacy


def _run(n, fmt="auto", clips=FAKE_CLIPS, seed0=0):
    history, used, out = [], set(), []
    for i in range(n):
        _, sb = planner.plan(history, used, fmt, clips, seed=seed0 + i)
        out.append(sb)
        used.add(sb["fingerprint"])
        history.append({k: sb[k] for k in ("template", "format", "dream_ids", "lines", "fingerprint")})
    return out


def test_never_repeats_a_combination():
    eps = _run(60)
    fps = [e["fingerprint"] for e in eps]
    assert len(fps) == len(set(fps))
    for e in eps:
        assert e["fingerprint"] == fingerprint(e["template"], e["dream_ids"], e["monster"], e["lines"])


def test_never_same_template_twice_in_a_row_and_footage_only_while_animated_is_retired():
    eps = _run(30)
    for a, b in zip(eps, eps[1:]):
        assert a["template"] != b["template"]
    assert {e["format"] for e in eps} == {"footage"}


def test_recent_dreams_are_avoided():
    eps = _run(20, fmt="footage")
    singles = [e["dream_ids"][0] for e in eps if len(e["dream_ids"]) == 1]
    repeats = sum(1 for a, b in zip(singles, singles[1:]) if a == b)
    assert repeats == 0


def test_without_footage_nothing_is_made():
    # the animated (still-drawing) templates are retired: no footage means no Short, never a slideshow
    for fmt in ("auto", "footage", "animated"):
        with pytest.raises(planner.PlanError):
            planner.plan([], set(), fmt, {}, seed=1)


def test_used_fingerprints_are_skipped():
    _, first = planner.plan([], set(), "auto", FAKE_CLIPS, seed=7)
    _, second = planner.plan([], {first["fingerprint"]}, "auto", FAKE_CLIPS, seed=7)
    assert second["fingerprint"] != first["fingerprint"]


def test_dream_count_comes_from_data():
    assert bank.dream_count() == len(bank.dreams()) == 13
    assert bank.fill("{count} dreams. one way home.") == f"{len(bank.dreams())} dreams. one way home."


def test_monsters_only_where_the_data_allows():
    eps, history, used = [], [], set()
    for i in range(40):
        _, sb = plan_legacy("animated", FAKE_CLIPS, seed=i, history=history, used=used)
        used.add(sb["fingerprint"])
        history.append({k: sb[k] for k in ("template", "format", "dream_ids", "lines", "fingerprint")})
        eps.append(sb)
    for e in eps:
        allowed = {d.monster for d in bank.dreams() if d.id in e["dream_ids"]}
        if e["monster"]:
            assert e["monster"] in allowed
        for s in e["shots"]:
            for a in s.get("actors", []):
                assert a["character"] in ("Dreamer", e["monster"])


def test_gentle_dream_has_no_scares():
    for e in _run(80, fmt="footage"):
        if e["dream_ids"] == ["train"]:
            for s in e["shots"]:
                assert not {"impact", "shake", "flash"} & set(s["fx"])
            assert "turn" not in e["lines"] or e["lines"]["turn"] in bank.lines()["memory"]


def test_a_dry_run_can_force_one_style():
    from bot import config
    _, sb = planner.plan([], set(), "auto", FAKE_CLIPS, seed=5, template="monster_reveal")
    assert sb["template"] == "monster_reveal"
    with pytest.raises(planner.PlanError):
        planner.plan([], set(), "auto", FAKE_CLIPS, seed=5, template="no_such_style")
    assert config.Settings.from_env({"BOT_TEMPLATE": "Monster_Reveal"}).template == "monster_reveal"
    assert config.Settings.from_env({}).template == ""
    with pytest.raises(config.ConfigError):
        config.Settings.from_env({"BOT_TEMPLATE": "x; rm -rf /"})
