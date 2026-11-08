"""Upload client against a fake HTTP layer: happy path, resume, quota, scope, hijack, ambiguity."""
import json

import pytest

from bot import config, publish, youtube
from bot.state import CLAIMED, FAILED, UNCLEAR, UPLOADED, Ledger, StateError
from bot.youtube import Response, TransportError

SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=SESSION1"


class FakeYouTube:
    """Scripted server. `script` maps a step name to a list of responses/exceptions consumed in order."""

    def __init__(self, size, scope=youtube.SCOPE, init=None, chunk_script=None, status_script=None):
        self.size, self.scope = size, scope
        self.calls = []
        self.received = 0
        self.init = init or [Response(200, {"location": SESSION})]
        self.chunk_script = chunk_script or []
        self.status_script = status_script or []

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url.split("?")[0], headers.get("Content-Range")))
        if url == youtube.TOKEN_URL:
            return Response(200, {}, json.dumps({"access_token": "tok", "scope": self.scope}).encode())
        if url.startswith(youtube.UPLOAD_URL) and "upload_id" not in url:
            r = self.init.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        if url == SESSION and headers.get("Content-Range", "").startswith("bytes */"):
            r = self.status_script.pop(0) if self.status_script else self._progress()
            if isinstance(r, Exception):
                raise r
            return r
        if url == SESSION:
            if self.chunk_script:
                r = self.chunk_script.pop(0)
                if isinstance(r, Exception):
                    raise r
                if r != "ok":
                    return r
            self.received += len(body)
            return self._progress()
        if url.startswith(youtube.THUMB_URL):
            return Response(200, {}, b"{}")
        raise AssertionError(f"unexpected URL {url}")

    def _progress(self):
        if self.received >= self.size:
            return Response(200, {}, json.dumps({"id": "VID123"}).encode())
        return Response(308, {"range": f"bytes=0-{self.received - 1}"} if self.received else {})


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "video.mp4"
    p.write_bytes(b"\0" * (youtube.CHUNK * 2 + 1234))
    return p


def client(fake):
    return youtube.Client("id", "secret", "refresh", transport=fake, sleep=lambda s: None)


META = {"snippet": {"title": "t"}, "status": {"privacyStatus": "private"}}


def test_happy_path_uploads_in_chunks(video):
    fake = FakeYouTube(video.stat().st_size)
    assert client(fake).upload(video, META) == "VID123"
    puts = [c for c in fake.calls if c[0] == "PUT"]
    assert len(puts) == 3 and puts[0][2].startswith("bytes 0-")


def test_extra_scope_is_refused(video):
    fake = FakeYouTube(video.stat().st_size, scope=youtube.SCOPE + " https://www.googleapis.com/auth/youtube")
    with pytest.raises(youtube.ScopeError):
        client(fake).upload(video, META)
    assert all(c[1] == youtube.TOKEN_URL for c in fake.calls)


def test_quota_exceeded_is_a_clean_failure(video):
    fake = FakeYouTube(video.stat().st_size, init=[Response(403, {}, b'{"error":{"message":"quotaExceeded"}}')])
    with pytest.raises(youtube.QuotaError):
        client(fake).upload(video, META)


def test_server_errors_resume_from_the_reported_offset(video):
    fake = FakeYouTube(video.stat().st_size, chunk_script=["ok", Response(503), TransportError("reset")])
    assert client(fake).upload(video, META) == "VID123"
    assert fake.received == video.stat().st_size  # no byte sent twice past the server's offset


def test_lost_final_response_is_checked_not_resent(video):
    size = video.stat().st_size

    class LoseLast(FakeYouTube):
        def request(self, method, url, headers, body, timeout):
            r = super().request(method, url, headers, body, timeout)
            if url == SESSION and body and self.received >= self.size:
                raise TransportError("connection dropped after the last chunk")
            return r

    fake = LoseLast(size)
    assert client(fake).upload(video, META) == "VID123"
    assert fake.received == size


def test_repeated_network_loss_is_unclear(video):
    fake = FakeYouTube(video.stat().st_size, chunk_script=[TransportError("x")] * 3,
                       status_script=[TransportError("y")] * 20)
    with pytest.raises(youtube.UploadUnclear):
        client(fake).upload(video, META)


def test_hijacked_session_url_is_refused(video):
    fake = FakeYouTube(video.stat().st_size,
                       init=[Response(200, {"location": "https://evil.example.com/upload?upload_id=1"})])
    with pytest.raises(youtube.EndpointError):
        client(fake).upload(video, META)
    assert not any("evil" in c[1] for c in fake.calls)


def test_expired_session_is_a_certain_failure(video):
    fake = FakeYouTube(video.stat().st_size, chunk_script=[Response(404)])
    with pytest.raises(youtube.UploadFailed):
        client(fake).upload(video, META)


def test_video_meta_privacy_and_synthetic_flag():
    m = youtube.video_meta({"title": "a", "description": "b", "tags": ["x"], "synthetic": True}, "unlisted")
    assert m["status"]["privacyStatus"] == "unlisted"
    assert m["status"]["containsSyntheticMedia"] is True
    assert m["status"]["selfDeclaredMadeForKids"] is False
    assert youtube.video_meta({"title": "a", "description": "b"}, "private")["status"]["containsSyntheticMedia"] is False


# ---------------------------------------------------------------- pipeline: claim -> upload -> ledger
def _prepared(tmp_path, video):
    folder = tmp_path / "ep-20261103-abcd1234"
    folder.mkdir()
    (folder / "video.mp4").write_bytes(video.read_bytes())
    (folder / "thumb.jpg").write_bytes(b"\xff\xd8fakejpeg")
    sb = {"episode_id": "ep-20261103-abcd1234", "template": "dont_stare", "format": "animated",
          "dream_ids": ["halls"], "monster": "Mannequin", "lines": {"hook": "x"}, "fingerprint": "fp",
          "title": "t #shorts", "description": "d", "tags": []}
    (folder / "storyboard.json").write_text(json.dumps(sb), encoding="utf-8")
    (tmp_path / "current.json").write_text(json.dumps({"episode_id": sb["episode_id"]}), encoding="utf-8")
    return sb


LIVE = config.Settings(privacy="private", dry_run=False, enabled=True, in_actions=True)


def test_pipeline_uploads_once_and_records(tmp_path, video):
    sb = _prepared(tmp_path, video)
    led = Ledger(tmp_path / "s.jsonl")
    publish.claim(LIVE, led, out_root=tmp_path)
    row = publish.upload(LIVE, led, client(FakeYouTube(video.stat().st_size)), out_root=tmp_path)
    assert row["status"] == UPLOADED and row["video_id"] == "VID123"
    with pytest.raises(StateError):  # second run: never again
        publish.upload(LIVE, led, client(FakeYouTube(video.stat().st_size)), out_root=tmp_path)
    with pytest.raises(StateError):
        publish.claim(LIVE, led, out_root=tmp_path)
    assert led.latest()[sb["episode_id"]]["status"] == UPLOADED


def test_pipeline_refuses_unclaimed_and_dry_run(tmp_path, video):
    _prepared(tmp_path, video)
    led = Ledger(tmp_path / "s.jsonl")
    with pytest.raises(StateError, match="not freshly claimed"):
        publish.upload(LIVE, led, client(FakeYouTube(1)), out_root=tmp_path)
    dry = config.Settings(dry_run=True, enabled=True, in_actions=True)
    with pytest.raises(StateError, match="dry run"):
        publish.claim(dry, led, out_root=tmp_path)
    with pytest.raises(StateError, match="dry run"):
        publish.upload(dry, led, client(FakeYouTube(1)), out_root=tmp_path)


def test_pipeline_records_unclear_and_never_retries(tmp_path, video):
    _prepared(tmp_path, video)
    led = Ledger(tmp_path / "s.jsonl")
    publish.claim(LIVE, led, out_root=tmp_path)
    fake = FakeYouTube(video.stat().st_size, chunk_script=[TransportError("x")] * 3,
                       status_script=[TransportError("y")] * 20)
    row = publish.upload(LIVE, led, client(fake), out_root=tmp_path)
    assert row["status"] == UNCLEAR
    with pytest.raises(StateError):
        publish.upload(LIVE, led, client(FakeYouTube(video.stat().st_size)), out_root=tmp_path)


def test_pipeline_records_quota_failure(tmp_path, video):
    _prepared(tmp_path, video)
    led = Ledger(tmp_path / "s.jsonl")
    publish.claim(LIVE, led, out_root=tmp_path)
    fake = FakeYouTube(video.stat().st_size, init=[Response(403, {}, b"quotaExceeded")])
    assert publish.upload(LIVE, led, client(fake), out_root=tmp_path)["status"] == FAILED
    assert [r["status"] for r in led.rows()] == [CLAIMED, FAILED]


def test_settings_from_env_validates(monkeypatch):
    s = config.Settings.from_env({"YT_PRIVACY": "Public", "DRY_RUN": "false", "BOT_FORMAT": "footage"})
    assert (s.privacy, s.dry_run, s.fmt) == ("public", False, "footage")
    assert config.Settings.from_env({}).privacy == "private" and config.Settings.from_env({}).dry_run is True
    with pytest.raises(config.ConfigError):
        config.Settings.from_env({"YT_PRIVACY": "everyone"})
