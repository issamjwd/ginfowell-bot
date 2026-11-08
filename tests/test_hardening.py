"""Review round 1: scope fail-closed, token refresh, no infinite 308 loop, never losing a video id, record checks,
live-only guards, AI-video allow-list, footage manifest paths, sprite cache cleanup, stdlib-only publish path."""
import json
import subprocess
import sys

import pytest

from bot import ai_video, config, footage, publish, sprites, youtube
from bot.state import UNCLEAR, UPLOADED, Ledger, StateError
from bot.youtube import Response
from tests.test_youtube import LIVE, META, SESSION, FakeYouTube, _prepared, client


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "video.mp4"
    p.write_bytes(b"\0" * (youtube.CHUNK * 2 + 1234))
    return p


def test_missing_scope_field_fails_closed(video):
    class NoScope(FakeYouTube):
        def request(self, method, url, headers, body, timeout):
            if url == youtube.TOKEN_URL:
                return Response(200, {}, json.dumps({"access_token": "tok"}).encode())
            return super().request(method, url, headers, body, timeout)

    with pytest.raises(youtube.ScopeError):
        client(NoScope(video.stat().st_size)).upload(video, META)


def test_expired_access_token_mid_upload_is_refreshed(video):
    fake = FakeYouTube(video.stat().st_size, chunk_script=["ok", Response(401)])
    assert client(fake).upload(video, META) == "VID123"
    assert sum(1 for c in fake.calls if c[1] == youtube.TOKEN_URL) == 2


def test_308_without_progress_cannot_loop_forever(video):
    class Stuck(FakeYouTube):
        def request(self, method, url, headers, body, timeout):
            self.calls.append((method, url.split("?")[0], headers.get("Content-Range")))
            if url == youtube.TOKEN_URL:
                return Response(200, {}, json.dumps({"access_token": "t", "scope": youtube.SCOPE}).encode())
            if "upload_id" not in url:
                return Response(200, {"location": SESSION})
            return Response(308, {"range": "bytes=0-99"})  # never moves past byte 99

    fake = Stuck(video.stat().st_size)
    with pytest.raises(youtube.UploadUnclear):
        client(fake).upload(video, META)
    assert len(fake.calls) < 60


def test_all_bytes_acknowledged_but_no_video_is_queried_not_resent(video):
    size = video.stat().st_size

    class AckOnly(FakeYouTube):
        def _progress(self):
            if self.received >= self.size:
                return Response(308, {"range": f"bytes=0-{self.size - 1}"})
            return super()._progress()

    fake = AckOnly(size, status_script=[Response(200, {}, json.dumps({"id": "VIDLATE1"}).encode())])
    assert client(fake).upload(video, META) == "VIDLATE1"
    assert not any(c[2] and c[2].startswith(f"bytes {size}-") for c in fake.calls)  # never a 0-byte chunk


def test_success_with_garbage_body_is_unclear(tmp_path, video):
    _prepared(tmp_path, video)
    led = Ledger(tmp_path / "s.jsonl")
    publish.claim(LIVE, led, out_root=tmp_path)

    class Garbage(FakeYouTube):
        def _progress(self):
            if self.received >= self.size:
                return Response(200, {}, b"<html>oops</html>")
            return super()._progress()

    assert publish.upload(LIVE, led, client(Garbage(video.stat().st_size)), out_root=tmp_path)["status"] == UNCLEAR


def test_thumbnail_problem_keeps_the_video_id(tmp_path, video):
    _prepared(tmp_path, video)
    (tmp_path / "ep-20261103-abcd1234" / "thumb.jpg").write_bytes(b"\xff" * (3 * 1024 * 1024))  # > 2 MB: raises
    led = Ledger(tmp_path / "s.jsonl")
    publish.claim(LIVE, led, out_root=tmp_path)
    row = publish.upload(LIVE, led, client(FakeYouTube(video.stat().st_size)), out_root=tmp_path)
    assert row["status"] == UPLOADED and row["video_id"] == "VID123" and row["thumbnail"] is False


def test_unexpected_error_after_claim_is_recorded_unclear(tmp_path, video):
    _prepared(tmp_path, video)
    led = Ledger(tmp_path / "s.jsonl")
    publish.claim(LIVE, led, out_root=tmp_path)

    class Boom(FakeYouTube):
        def request(self, method, url, headers, body, timeout):
            if url == SESSION:
                raise KeyError("surprise")
            return super().request(method, url, headers, body, timeout)

    row = publish.upload(LIVE, led, client(Boom(video.stat().st_size)), out_root=tmp_path)
    assert row["status"] == UNCLEAR and "KeyError" in row["note"]
    assert (tmp_path / "result.json").exists()


def test_record_appends_a_checked_result(tmp_path, video):
    _prepared(tmp_path, video)
    upload_ledger = Ledger(tmp_path / "upload.jsonl")
    publish.claim(LIVE, upload_ledger, out_root=tmp_path)
    publish.upload(LIVE, upload_ledger, client(FakeYouTube(video.stat().st_size)), out_root=tmp_path)
    fresh = Ledger(tmp_path / "fresh.jsonl")  # the record job's checkout has only the claim
    fresh.path.write_text(upload_ledger.path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    row = publish.record(fresh, tmp_path / "result.json")
    assert row["status"] == UPLOADED and row["video_id"] == "VID123"
    with pytest.raises(StateError):
        publish.record(fresh, tmp_path / "result.json")


def test_record_rejects_forged_rows(tmp_path):
    led = Ledger(tmp_path / "s.jsonl")
    led.claim("ep-20261103-zzzz", {})
    bad = tmp_path / "r.json"
    for row in ({"episode_id": "ep-20261103-zzzz", "status": "claimed"},
                {"episode_id": "../../etc", "status": "uploaded"},
                {"episode_id": "ep-20261103-zzzz", "status": "uploaded", "video_id": "x; rm -rf /"}):
        bad.write_text(json.dumps(row), encoding="utf-8")
        with pytest.raises(StateError):
            publish.record(led, bad)


@pytest.mark.parametrize("settings", [
    config.Settings(dry_run=False, enabled=False, in_actions=True),
    config.Settings(dry_run=False, enabled=True, in_actions=False),
    config.Settings(dry_run=True, enabled=True, in_actions=True),
])
def test_live_needs_enabled_actions_and_not_dry(tmp_path, video, settings):
    _prepared(tmp_path, video)
    with pytest.raises(StateError):
        publish.claim(settings, Ledger(tmp_path / "s.jsonl"), out_root=tmp_path)


def test_client_refuses_outside_github_actions(monkeypatch):
    for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        monkeypatch.setenv(k, "x")
    with pytest.raises(config.ConfigError, match="GitHub Actions"):
        publish.client_from_env(config.Settings(in_actions=False))
    assert isinstance(publish.client_from_env(config.Settings(in_actions=True)), youtube.Client)


def test_settings_read_the_live_switches():
    s = config.Settings.from_env({"BOT_ENABLED": "true", "GITHUB_ACTIONS": "true"})
    assert s.enabled and s.in_actions
    s = config.Settings.from_env({"BOT_ENABLED": "yes"})
    assert not s.enabled and not s.in_actions


def test_bad_current_json_is_refused(tmp_path):
    (tmp_path / "current.json").write_text(json.dumps({"episode_id": "../secrets"}), encoding="utf-8")
    with pytest.raises(StateError):
        publish.current(tmp_path)


def test_publish_path_imports_no_third_party_package():
    code = ("import sys\n"
            "for m in ('numpy', 'PIL', 'scipy', 'anthropic', 'imageio_ffmpeg'):\n"
            "    sys.modules[m] = None\n"
            "import bot.publish, bot.state, bot.youtube, bot.fonts, bot.__main__\n"
            "print('ok')\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(config.ROOT), capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr


# ---------------------------------------------------------------- AI video allow-list
def test_ai_video_stays_off_without_allowed_hosts(monkeypatch):
    env = {"AI_VIDEO_API_KEY": "k", "AI_VIDEO_PROVIDER": "higgsfield", "AI_VIDEO_ENDPOINT": "https://api.example.com/v1"}
    assert ai_video.enabled(env) is False


def test_ai_video_endpoint_must_be_allow_listed(monkeypatch, tmp_path):
    hosts = tmp_path / "hosts.json"
    hosts.write_text(json.dumps({"providers": {"generic": {"api": ["api.good.test"], "media": ["cdn.good.test"]}}}))
    monkeypatch.setattr(ai_video, "HOSTS_FILE", hosts)
    base = {"AI_VIDEO_API_KEY": "k", "AI_VIDEO_PROVIDER": "generic"}
    assert ai_video.enabled({**base, "AI_VIDEO_ENDPOINT": "https://api.good.test/v1"}) is True
    assert ai_video.enabled({**base, "AI_VIDEO_ENDPOINT": "https://evil.test/v1"}) is False


@pytest.mark.parametrize("url", ["http://cdn.good.test/a.mp4", "https://evil.test/a.mp4",
                                 "https://user:pw@cdn.good.test/a.mp4", "file:///etc/passwd"])
def test_ai_video_download_hosts_are_checked(url):
    with pytest.raises(ai_video.AIVideoError):
        ai_video.check_url(url, {"cdn.good.test"})


def test_ai_video_rejects_non_mp4(monkeypatch, tmp_path):
    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"#EXTM3U\nhttp://169.254.169.254/\n"

    monkeypatch.setattr(ai_video, "_open", lambda req, timeout: R())
    with pytest.raises(ai_video.AIVideoError, match="not an MP4"):
        ai_video.download("https://cdn.good.test/a.mp4", {"cdn.good.test"}, tmp_path / "x.mp4")


# ---------------------------------------------------------------- footage manifest
def test_manifest_with_unsafe_file_is_ignored(monkeypatch, tmp_path):
    (tmp_path / "halls.mp4").write_bytes(b"x")
    (tmp_path / "manifest.json").write_text(json.dumps({"clips": {
        "halls": {"file": "halls.mp4", "seconds": 9.5},
        "evil": {"file": "../../secret.mp4", "seconds": 9.5},
        "url": {"file": "http://x/a.mp4", "seconds": 9.5}}}))
    monkeypatch.setenv("BOT_FOOTAGE_DIR", str(tmp_path))
    assert footage.available() == {"halls": 9.5}
    with pytest.raises(ValueError):
        footage.clip_path("evil")


# ---------------------------------------------------------------- sprite cache
def test_failed_blender_render_leaves_no_cache_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPRITE_CACHE", tmp_path / "cache")

    def fake_run(cmd, **kw):
        out = tmp_path / "cache"
        folder = next(out.iterdir())
        (folder / "Dreamer_idle_+1.00.png").write_bytes(b"half")  # a partial render
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(sprites.subprocess, "run", fake_run)
    assert sprites.ensure_sprites("blender", res=64, samples=1) is None
    assert not any((tmp_path / "cache").iterdir())


def test_blender_missing_binary_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPRITE_CACHE", tmp_path / "cache")

    def raise_oserror(cmd, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(sprites.subprocess, "run", raise_oserror)
    assert sprites.ensure_sprites("/nope/blender", res=64, samples=1) is None
    assert not any((tmp_path / "cache").iterdir())


def test_claim_records_the_gates_slot_day_and_ignores_junk(tmp_path, video, monkeypatch):
    _prepared(tmp_path, video)
    monkeypatch.setenv("BOT_SLOT_DAY", "2026-11-04")
    row = publish.claim(LIVE, Ledger(tmp_path / "a.jsonl"), out_root=tmp_path)
    assert row["slot_day"] == "2026-11-04"
    monkeypatch.setenv("BOT_SLOT_DAY", "2026-11-04\nstatus=uploaded")
    row = publish.claim(LIVE, Ledger(tmp_path / "b.jsonl"), out_root=tmp_path)
    assert "slot_day" not in row


def test_claim_without_a_slot_day_or_with_an_impossible_one_omits_it(tmp_path, video, monkeypatch):
    _prepared(tmp_path, video)
    monkeypatch.delenv("BOT_SLOT_DAY", raising=False)
    assert "slot_day" not in publish.claim(LIVE, Ledger(tmp_path / "a.jsonl"), out_root=tmp_path)
    monkeypatch.setenv("BOT_SLOT_DAY", "2026-13-45")
    assert "slot_day" not in publish.claim(LIVE, Ledger(tmp_path / "b.jsonl"), out_root=tmp_path)


def test_claim_records_an_extra_short_only_on_the_exact_flag(tmp_path, video, monkeypatch):
    _prepared(tmp_path, video)
    monkeypatch.setenv("BOT_SLOT_EXTRA", "true")
    assert publish.claim(LIVE, Ledger(tmp_path / "a.jsonl"), out_root=tmp_path)["extra"] is True
    for junk in ("", "false", "True", "true\nstatus=uploaded"):
        monkeypatch.setenv("BOT_SLOT_EXTRA", junk)
        assert "extra" not in publish.claim(LIVE, Ledger(tmp_path / f"b{len(junk)}.jsonl"), out_root=tmp_path)
