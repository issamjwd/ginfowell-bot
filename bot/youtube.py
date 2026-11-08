"""YouTube upload client. The ONLY things this bot does on YouTube: videos.insert (resumable) and thumbnails.set.

Least privilege, enforced three ways:
  1. the refresh token must carry exactly the scope https://www.googleapis.com/auth/youtube.upload (checked on every
     token refresh; any extra scope = refuse to run),
  2. every request goes through `_guard`, which only allows the four URLs below (and the resumable session URL that
     Google hands back, which must point at the same upload endpoint),
  3. tests/test_endpoint_guard.py greps the whole codebase for any other googleapis/youtube endpoint.
Nothing else (no engagement, no reading other channels): no other YouTube call exists in this codebase.

Network layer is injectable (`Transport`) so tests run against a fake.
"""
from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
ALLOWED = (TOKEN_URL, UPLOAD_URL, THUMB_URL)
CHUNK = 8 * 1024 * 1024          # multiple of 256 KiB, as resumable uploads require
RETRY_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8


class YouTubeError(RuntimeError):
    """Base error."""


class ScopeError(YouTubeError):
    """The token can do more than upload. Refuse to run."""


class QuotaError(YouTubeError):
    """Daily API quota is used up. Nothing was uploaded."""


class UploadFailed(YouTubeError):
    """Certain: the video is NOT on YouTube."""


class UploadUnclear(YouTubeError):
    """The video may or may not be on YouTube. Never retried automatically."""


class EndpointError(YouTubeError):
    """A request to a URL outside the allow-list was attempted."""


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> dict:
        return json.loads(self.body.decode() or "{}")


class TransportError(OSError):
    """Network-level failure: no HTTP status was received."""


class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> Response:
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # 308 means "resume incomplete" here, never a redirect
        return None


class UrllibTransport:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method, url, headers, body, timeout):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as r:  # noqa: S310 - URL checked by _guard
                return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise TransportError(str(e)) from e


def _guard(url: str, session_ok: bool = False) -> None:
    base = url.split("?", 1)[0]
    if base in ALLOWED and (base != UPLOAD_URL or session_ok or "uploadType=resumable" in url):
        return
    raise EndpointError(f"blocked request to {base}")


def _session_ok(url: str) -> bool:
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)
    return (u.scheme == "https" and u.netloc == "www.googleapis.com" and u.path == "/upload/youtube/v3/videos"
            and "upload_id" in q)


@dataclass
class Client:
    client_id: str
    client_secret: str
    refresh_token: str
    transport: Transport = field(default_factory=UrllibTransport)
    sleep: Callable[[float], None] = time.sleep
    timeout: float = 120.0
    _token: str | None = None

    # -------------------------------------------------------------- auth
    def _send(self, method: str, url: str, headers: dict, body: bytes | None, session: bool = False) -> Response:
        _guard(url, session_ok=session)
        return self.transport.request(method, url, headers, body, self.timeout)

    def token(self, refresh: bool = False) -> str:
        if self._token and not refresh:
            return self._token
        form = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                                       "refresh_token": self.refresh_token, "grant_type": "refresh_token"}).encode()
        r = self._retry(lambda: self._send("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                           form))
        if r.status != 200:
            raise YouTubeError(f"token refresh failed: HTTP {r.status} {_err(r)}")
        try:
            data = r.json()
        except ValueError as e:
            raise YouTubeError("token response is not JSON") from e
        granted = data.get("scope")
        if not isinstance(granted, str) or set(granted.split()) != {SCOPE}:
            # fail closed: a missing scope field is treated as "unknown", not as "fine"
            raise ScopeError(f"token scopes are {granted!r}; this bot only accepts exactly {SCOPE}. "
                             "Make a new refresh token with only the youtube.upload scope (SETUP.md step 6).")
        if not data.get("access_token"):
            raise YouTubeError("token response has no access_token")
        self._token = data["access_token"]
        return self._token

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    def _retry(self, fn: Callable[[], Response]) -> Response:
        for attempt in range(MAX_RETRIES):
            try:
                r = fn()
            except TransportError as e:
                log.warning("network error (%s), retry %d", e, attempt + 1)
            else:
                if r.status not in RETRY_STATUS:
                    return r
                log.warning("HTTP %s, retry %d", r.status, attempt + 1)
            self.sleep(min(60.0, 2 ** attempt) + random.uniform(0, 1))
        raise TransportError("gave up after retries")

    # -------------------------------------------------------------- videos.insert (resumable)
    def start_upload(self, meta: dict, size: int) -> str:
        url = UPLOAD_URL + "?uploadType=resumable&part=snippet,status"
        body = json.dumps(meta).encode()

        def send() -> Response:
            headers = {**self._auth(), "Content-Type": "application/json; charset=UTF-8",
                       "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)}
            return self._send("POST", url, headers, body)

        try:
            r = self._retry(send)
            if r.status == 401:  # access token expired between refresh and use: refresh once
                self.token(refresh=True)
                r = self._retry(send)
        except TransportError as e:
            raise UploadFailed(f"could not start the upload: {e}") from e
        if r.status == 403 and b"quota" in r.body.lower():
            raise QuotaError("YouTube API quota exceeded; nothing uploaded")
        if r.status != 200 or "location" not in r.headers:
            raise UploadFailed(f"upload init HTTP {r.status} {_err(r)}")
        session = r.headers["location"]
        if not _session_ok(session):
            raise EndpointError(f"unexpected upload session URL host/path: {session.split('?')[0]}")
        return session

    def _status(self, session: str, size: int) -> tuple[int, dict | None]:
        """Ask the session how much it has. Returns (next offset, video resource if finished)."""
        r = self._retry(lambda: self._send("PUT", session, {**self._auth(), "Content-Length": "0",
                                                            "Content-Range": f"bytes */{size}"}, b"", session=True))
        if r.status in (200, 201):
            return size, _json(r)
        if r.status == 308:
            return _next_offset(r), None
        if r.status in (404, 410):
            raise UploadFailed(f"upload session expired (HTTP {r.status}); video not created")
        raise UploadUnclear(f"status query HTTP {r.status} {_err(r)}")

    def upload(self, path: Path, meta: dict) -> str:
        """Upload a video. Returns the video id. Raises UploadFailed (certainly not posted) or UploadUnclear."""
        size = path.stat().st_size
        if size == 0:
            raise UploadFailed("empty video file")
        session = self.start_upload(meta, size)
        offset, tries, refreshed = 0, 0, 0
        with path.open("rb") as fh:
            while True:
                if offset >= size:
                    # the server says it has every byte but has not answered with the video: ask, never resend
                    offset, done = self._checked_status(session, size)
                    if done:
                        return _video_id(done)
                    tries += 1
                    if tries > MAX_RETRIES or offset >= size:
                        raise UploadUnclear("all bytes sent but YouTube never confirmed the video")
                    continue
                fh.seek(offset)
                chunk = fh.read(CHUNK)
                end = offset + len(chunk) - 1
                headers = {**self._auth(), "Content-Length": str(len(chunk)),
                           "Content-Range": f"bytes {offset}-{end}/{size}"}
                try:
                    r = self._send("PUT", session, headers, chunk, session=True)
                except TransportError as e:
                    r = None
                    log.warning("chunk at %d: %s", offset, e)
                if r is not None and r.status in (200, 201):
                    return _video_id(_json(r))
                if r is not None and r.status == 308:
                    new = _next_offset(r)
                    if new > offset:
                        offset, tries = new, 0
                        continue
                    offset = new  # no progress: counts as a failed attempt below
                elif r is not None and r.status == 401 and refreshed < 2:
                    refreshed += 1
                    self.token(refresh=True)
                    continue
                elif r is not None and r.status not in RETRY_STATUS:
                    if r.status in (404, 410):
                        raise UploadFailed(f"session gone (HTTP {r.status})")
                    raise UploadUnclear(f"chunk HTTP {r.status} {_err(r)}")
                tries += 1
                if tries > MAX_RETRIES:
                    raise UploadUnclear("too many failed chunk attempts")
                self.sleep(min(60.0, 2 ** tries) + random.uniform(0, 1))
                offset, done = self._checked_status(session, size)
                if done:
                    return _video_id(done)

    def _checked_status(self, session: str, size: int) -> tuple[int, dict | None]:
        try:
            return self._status(session, size)
        except TransportError as e:
            raise UploadUnclear(f"lost contact while checking the upload: {e}") from e

    # -------------------------------------------------------------- thumbnails.set
    def set_thumbnail(self, video_id: str, jpg: Path) -> bool:
        data = jpg.read_bytes()
        if len(data) > 2 * 1024 * 1024:
            raise YouTubeError("thumbnail over 2 MB")
        url = THUMB_URL + "?" + urllib.parse.urlencode({"videoId": video_id})
        try:
            r = self._retry(lambda: self._send("POST", url, {**self._auth(), "Content-Type": "image/jpeg"}, data))
        except TransportError as e:
            log.warning("thumbnail not set: %s", e)
            return False
        if r.status != 200:
            log.warning("thumbnail not set: HTTP %s %s (custom thumbnails need a phone-verified channel)",
                        r.status, _err(r))
            return False
        return True


CATEGORIES = {"15", "17", "19", "20", "26", "27", "28"}  # Pets&Animals, Sports, Travel, Gaming, Howto, Education, Sci&Tech


def video_meta(sb: dict, privacy: str) -> dict:
    cat = str(sb.get("category_id") or "20")
    return {
        "snippet": {"title": sb["title"], "description": sb["description"], "tags": sb.get("tags", []),
                    "categoryId": cat if cat in CATEGORIES else "20",
                    "defaultLanguage": "en", "defaultAudioLanguage": "en"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True,
                   "license": "youtube", "containsSyntheticMedia": bool(sb.get("synthetic", False))},
    }


def _json(r: Response) -> dict:
    try:
        data = r.json()
    except ValueError as e:
        raise UploadUnclear("YouTube answered success but the body is not JSON") from e
    if not isinstance(data, dict):
        raise UploadUnclear("YouTube answered success with an unexpected body")
    return data


def _video_id(resource: dict) -> str:
    vid = resource.get("id")
    if not isinstance(vid, str) or not vid:
        raise UploadUnclear("upload finished but no video id came back")
    return vid


def _next_offset(r: Response) -> int:
    rng = r.headers.get("range")
    if not rng:
        return 0
    try:
        return int(rng.rsplit("-", 1)[1]) + 1
    except (IndexError, ValueError) as e:
        raise UploadUnclear(f"unreadable Range header {rng!r}") from e


def _err(r: Response) -> str:
    try:
        e = r.json().get("error")
        if isinstance(e, dict):
            return str(e.get("message", ""))[:200]
        return str(e or r.json().get("error_description", ""))[:200]
    except (ValueError, AttributeError):
        return r.body[:200].decode(errors="replace")
