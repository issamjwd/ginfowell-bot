"""World Shorts (bot/world.py) and the topic switch (bot/topic.py). Offline: no test touches the network."""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import pytest
from PIL import Image

from bot import config, pipeline, publish, qa, topic, world, youtube


# ------------------------------------------------------------------------------------------------- topic switch
def _row(template, kind="short", status="uploaded"):
    return {"episode_id": "ep-x", "template": template, "kind": kind, "status": status}


def test_most_is_three_world_shorts_then_one_liminality():
    env = {"BOT_KIND": "short"}
    w, lim = _row("world_space"), _row("monster_reveal")
    assert topic.choose([], env) is True
    assert topic.choose([lim, w, w], env) is True
    assert topic.choose([lim, w, w, w], env) is False
    assert topic.choose([w, w, w, lim], env) is True
    assert topic.choose([w, w, w, _row("world_history", status="failed")], env) is False


def test_mix_alternates_liminality_and_world():
    env = {"BOT_KIND": "short", "BOT_WORLD": "mix"}
    assert topic.choose([], env) is True
    assert topic.choose([_row("monster_reveal")], env) is True
    assert topic.choose([_row("world_space")], env) is False
    # the long video and failed uploads don't count
    assert topic.choose([_row("world_space"), _row("long_tour", kind="long")], env) is False
    assert topic.choose([_row("monster_reveal"), _row("world_space", status="failed")], env) is True


def test_modes_and_overrides():
    h = [_row("world_space")]
    assert topic.choose(h, {"BOT_WORLD": "only"}) is True
    assert topic.choose([], {"BOT_WORLD": "off"}) is False
    assert topic.choose([], {"BOT_WORLD": "nonsense"}) is True          # unknown value = most
    assert topic.choose([], {"BOT_KIND": "long", "BOT_WORLD": "only"}) is False
    assert topic.choose(h, {"BOT_TOPIC": "world"}) is True
    assert topic.choose([], {"INPUT_TOPIC": "liminality"}) is False
    assert topic.choose([], {"BOT_TEMPLATE": "monster_reveal"}) is False
    assert topic.choose(h, {"BOT_TEMPLATE": "world_history"}) is True


def test_topic_cli_writes_output_and_env(tmp_path, monkeypatch):
    out, env = tmp_path / "out", tmp_path / "env"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_ENV", str(env))
    monkeypatch.setenv("BOT_WORLD", "off")
    monkeypatch.delenv("BOT_TOPIC", raising=False)
    monkeypatch.delenv("BOT_TEMPLATE", raising=False)
    assert topic.main() == 0
    assert out.read_text() == "world=false\n" and env.read_text() == "BOT_TOPIC=liminality\n"


# ----------------------------------------------------------------------------------------------------- network
@pytest.mark.parametrize("url,want", [
    ("https://images-api.nasa.gov/search?q=x", "https://images-api.nasa.gov/search?q=x"),
    ("http://images-assets.nasa.gov/image/A/A~large.jpg", "https://images-assets.nasa.gov/image/A/A~large.jpg"),
])
def test_allowed_urls(url, want):
    assert world.check_url(url) == want


@pytest.mark.parametrize("url", ["http://en.wikipedia.org/x", "https://evil.example.com/a.jpg",
                                 "https://u:p@upload.wikimedia.org/a.jpg", "file:///etc/passwd",
                                 "https://upload.wikimedia.org:8443/a.jpg", "https://169.254.169.254/"])
def test_other_urls_refused(url):
    with pytest.raises(world.WorldError):
        world.check_url(url)


# ------------------------------------------------------------------------------------------------------ space
GOOD = "Sunrise over a dune field on Mars, seen by the Curiosity rover on its way up the mountain. More text."


def test_nasa_item_filters():
    need = ("mars",)
    assert world.nasa_ok({"description": GOOD}, need) == GOOD.split(" More")[0]
    assert world.nasa_ok({"description": GOOD}, ("saturn",)) is None                       # off-subject
    assert world.nasa_ok({"description": GOOD + " Credit: ESA/Hubble"}, need) is None       # someone else's
    assert world.nasa_ok({"description": GOOD + " Image credit: NASA/JPL-Caltech"}, need)  # NASA's own
    assert world.nasa_ok({"description": GOOD, "photographer": "Jane Doe / Reuters"}, need) is None
    assert world.nasa_ok({"description": GOOD, "secondary_creator": "NASA/Bill Ingalls"}, need)
    assert world.nasa_ok({"description": "© 2020 " + GOOD}, need) is None
    assert world.nasa_ok({"description": "This graph shows the pressure at the surface of Mars over a day."},
                         need) is None
    assert world.nasa_ok({"description": "Mars."}, need) is None                            # too short
    s = world.nasa_ok({"description": "KENNEDY SPACE CENTER, FLA. - Workers move the Mars rover into the "
                                      "clean room before launch day."}, need)
    assert s.startswith("Workers move")


# ---------------------------------------------------------------------------------------------------- history
def test_commons_files_only():
    assert world.commons_file("https://upload.wikimedia.org/wikipedia/commons/c/c7/Saturn_x.jpg?utm=1") == \
        "Saturn_x.jpg"
    assert world.commons_file("https://upload.wikimedia.org/wikipedia/commons/thumb/c/c7/A_b.png/320px-A_b.png") == \
        "A_b.png"
    assert world.commons_file("https://upload.wikimedia.org/wikipedia/en/c/c7/Poster.jpg") is None  # may be non-free
    assert world.commons_file("https://evil.example.com/wikipedia/commons/c/c7/A.jpg") is None
    assert world.commons_file("https://upload.wikimedia.org/wikipedia/commons/c/c7/A.svg") is None


def test_free_licences():
    for ok in ("Public domain", "CC0", "CC BY 2.0", "CC BY-SA 4.0", "PD-US"):
        assert world.FREE_LICENCE.match(ok), ok
    for bad in ("Fair use", "CC BY-NC 2.0", "CC BY-ND 3.0", "All rights reserved", ""):
        assert not world.FREE_LICENCE.match(bad), bad


def _page(src):
    return {"title": "X", "originalimage": {"source": src}}


def test_history_events_skip_grim_long_and_non_free():
    commons = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Rail_line.jpg"
    feed = {"selected": [
        {"year": 1987, "text": "Singapore's first rail line (pictured) opened, with trains running between two towns",
         "pages": [_page(commons)]},
        {"year": 1990, "text": "A bombing in the city centre killed many people on a busy market day morning",
         "pages": [_page(commons)]},
        {"year": 1991, "text": "A famous film poster was revealed to a crowd of fans in the city of Los Angeles",
         "pages": [_page("https://upload.wikimedia.org/wikipedia/en/a/ab/Poster.jpg")]},
        {"year": 1992, "text": " ".join(["word"] * 40), "pages": [_page(commons)]},
    ]}
    ev = world.history_events(feed)
    assert [e["year"] for e in ev] == [1987]
    assert "(pictured)" not in ev[0]["text"] and ev[0]["text"].endswith(".")


# ------------------------------------------------------------------------------------------------------ plan
def test_plan_alternates_kinds_and_never_repeats(monkeypatch):
    def fake(kind):
        def f(*a, **k):
            sc = [world.Scene(None, f"{kind} caption {i}", media_id=f"{kind}{i}") for i in range(3)]
            return {"template": kind, "topic": "T", "hook": "Hook", "scenes": sc}
        return f
    monkeypatch.setattr(world, "plan_space", fake("world_space"))
    monkeypatch.setattr(world, "plan_history", fake("world_history"))
    assert world.plan([], set(), 1)["template"] == "world_space"
    assert world.plan([_row("world_space")], set(), 1)["template"] == "world_history"
    p = world.plan([], set(), 1)
    assert world.plan([], {p["fingerprint"]}, 1)["template"] == "world_history"  # posted already: the other kind
    with pytest.raises(world.WorldError):
        world.plan([], {p["fingerprint"]}, 1, forced="world_space")


# ---------------------------------------------------------------------------------------------------- render
def _plan():
    rng = random.Random(3)
    scenes = []
    for i, size in enumerate(((1200, 800), (700, 1000), (900, 900))):
        im = Image.effect_noise(size, 60)
        im = Image.merge("RGB", [im.point(lambda v, k=k: (v + k * 70 + rng.randint(0, 40)) % 256) for k in range(3)])
        scenes.append(world.Scene(im, f"Caption number {i} for a test scene, long enough to wrap onto two lines.",
                                  big="1987" if i == 1 else "", credit="NASA  ·  TEST", seconds=3.9,
                                  media_id=f"T{i}"))
    return {"template": "world_space", "topic": "Saturn", "hook": "Real NASA photos of Saturn",
            "label": "FROM NASA'S ARCHIVE", "scenes": scenes, "title": "Real NASA photos of Saturn #shorts",
            "category_id": "28", "tags": ["space"], "credits": "Images: NASA.",
            "lines": {"hook": "Real NASA photos of Saturn", "topic": "Saturn"}, "media": ["T0", "T1", "T2"],
            "fingerprint": "abcdef0123456789"}


def test_render_passes_qa_and_hands_off_like_any_short(tmp_path):
    p = _plan()
    folder = tmp_path / "ep-20261107-abcdef01"
    folder.mkdir()
    video = world.render(p, folder / "video.mp4", 540, 960, seed=7)
    report = qa.check(video, 540, 960)
    assert report.ok, report.as_text()
    world.thumbnail(p, folder / "thumb.jpg")
    assert Image.open(folder / "thumb.jpg").size == (1280, 720)
    sb = world.storyboard(p, 7, datetime(2026, 11, 7, tzinfo=timezone.utc), 540, 960)
    assert sb["episode_id"] == "ep-20261107-abcdef01" and publish.EPISODE_ID.match(sb["episode_id"])
    (folder / "storyboard.json").write_text(json.dumps(sb), encoding="utf-8")
    (tmp_path / "current.json").write_text(json.dumps({"episode_id": sb["episode_id"]}), encoding="utf-8")
    got, _ = publish.current(tmp_path)
    meta = youtube.video_meta(got, "private")
    assert meta["snippet"]["categoryId"] == "28" and meta["status"]["containsSyntheticMedia"] is False
    assert "Liminality" in got["description"] and got["title"].endswith("#shorts")


def test_unknown_category_falls_back_to_gaming():
    assert youtube.video_meta({"title": "a", "description": "b", "category_id": "10"}, "private")[
        "snippet"]["categoryId"] == "20"


def test_pipeline_falls_back_when_no_world_short(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise world.WorldError("offline")
    monkeypatch.setattr(world, "make", boom)
    assert pipeline._world(config.Settings(), [], set(), 1, tmp_path) is None


# ------------------------------------------------------------------------------------------------- clips + lists
def test_topic_lists_are_well_formed():
    lists = {k: v for k, v in json.loads(world.NATURE_FILE.read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    assert len(lists) >= 8
    for kind, spec in lists.items():
        assert spec["hook"] and len(spec["titles"]) >= 20, kind
        assert len(set(spec["titles"])) == len(spec["titles"]), kind
        assert str(spec.get("category_id", "27")) in youtube.CATEGORIES, kind


def test_non_footage_videos_are_skipped():
    assert world.NOT_CLIP.search("Expedition 70 crew news conference")
    assert world.NOT_CLIP.search("Artemis II animated trailer")
    assert not world.NOT_CLIP.search("A Falcon 9 rocket lifts off from Launch Complex 39A at Kennedy.")


def test_fetch_file_refuses_a_non_mp4(monkeypatch, tmp_path):
    class Resp:
        def __init__(self):
            self.chunks = [b"<html>not a video</html>", b""]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.chunks.pop(0)

    class Opener:
        def open(self, req, timeout):
            return Resp()

    monkeypatch.setattr(world.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(world.WorldError):
        world.fetch_file("https://images-assets.nasa.gov/video/x/x~medium.mp4", tmp_path / "x.mp4")
    assert not (tmp_path / "x.mp4").exists()


def test_a_clip_scene_plays_the_busiest_part(tmp_path):
    # a still first half, a moving second half: the window lands in the moving part, and the scene shows video
    src = tmp_path / "src.mp4"
    world._ff(["-f", "lavfi", "-i", "color=c=gray:s=320x180:d=6", "-f", "lavfi", "-i",
               "testsrc2=s=320x180:d=6", "-filter_complex", "[0][1]concat=n=2:v=1[v]", "-map", "[v]",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(src)])
    # _ff whitelists only "file", lavfi is an input format, not a protocol: fall back if this ffmpeg refuses
    if not src.exists():
        pytest.skip("ffmpeg could not make the test clip")
    dur = world.clip_seconds(src)
    start = world.busiest_start(src, dur, 3.0)
    assert start >= 5.0
    p = _plan()
    p["scenes"][0] = world.Scene(world.clip_frame(src, start), "A test clip caption that is long enough.",
                                 credit="NASA  ·  T", seconds=3.9, media_id="clip", clip=src, clip_start=start)
    video = world.render(p, tmp_path / "v.mp4", 540, 960, seed=3)
    assert qa.check(video, 540, 960).ok


def test_game_spotlights_use_published_games_and_reuse_few_screenshots(monkeypatch):
    p = world.plan_game(random.Random(1), set(), set())
    assert p["template"] == "world_game" and len(p["scenes"]) >= world.MIN_SCENES
    assert "roblox.com/games/" in p["credits"] and p["scenes"][0].big
    games = json.loads((world.GAMES_DIR / "games.json").read_text(encoding="utf-8"))
    assert p["topic"] in {g["name"] for k, g in games.items() if not k.startswith("_") and g.get("published")}
    # a game recently shown is skipped while others are left
    others = {world.plan_game(random.Random(s), set(), {p["topic"]})["topic"] for s in range(5)}
    assert p["topic"] not in others


def test_games_mode_alternates_with_liminality():
    env = {"BOT_KIND": "short", "BOT_WORLD": "games"}
    assert topic.choose([_row("monster_reveal")], env) is True
    assert topic.choose([_row("world_game")], env) is False


def test_accent_never_repeats_the_last_three():
    from bot import palette
    hist = [{"accent": a} for a in palette.ACCENTS[:3]]
    assert all(palette.pick(hist, s)["accent"] not in palette.ACCENTS[:3] for s in range(20))
