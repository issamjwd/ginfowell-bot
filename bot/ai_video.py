"""Optional paid AI-video plug-in. OFF by default.

On only when ALL of these hold:
  * secrets AI_VIDEO_API_KEY and AI_VIDEO_PROVIDER are set, and variable AI_VIDEO_ENDPOINT is set,
  * the provider has hosts listed in bot/data/ai_video_hosts.json (committed, so a host can only be added by a
    reviewed commit on main, not by a runtime variable), and
  * AI_VIDEO_ENDPOINT's host is in that provider's "api" list.
When it produces a clip, the clip replaces the first animated scene and the storyboard gets "synthetic": true
(YouTube's `status.containsSyntheticMedia` flag + a line in the description). Any failure -> the free drawn scene.

Network rules: https only, redirects never followed, the API is only called on its listed "api" hosts and the video
only downloaded from its listed "media" hosts, size-capped, and checked to be an MP4 before ffmpeg sees it (ffmpeg
then opens it with `-f mp4 -protocol_whitelist file`, so the file can't point ffmpeg at the network).

Request shape (generic): POST {endpoint} {"prompt", "image_base64", "duration", "aspect_ratio"} -> {"id"};
GET {endpoint}/{id} -> {"status": "succeeded", "video_url"}. The "higgsfield" entry uses that shape and is NOT
verified against Higgsfield's live API (written offline): check their docs and fill the host lists before enabling.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

log = logging.getLogger(__name__)
HOSTS_FILE = config.DATA / "ai_video_hosts.json"
MAX_BYTES = 200 * 1024 * 1024


class AIVideoError(RuntimeError):
    """The plug-in refused or failed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _hosts(provider: str) -> dict[str, set[str]]:
    data = json.loads(HOSTS_FILE.read_text(encoding="utf-8")).get("providers", {}).get(provider, {})
    return {"api": set(data.get("api", [])), "media": set(data.get("media", []))}


def enabled(env: dict | None = None) -> bool:
    e = os.environ if env is None else env
    provider = (e.get("AI_VIDEO_PROVIDER") or "").strip().lower()
    if not (e.get("AI_VIDEO_API_KEY") and provider and e.get("AI_VIDEO_ENDPOINT")):
        return False
    hosts = _hosts(provider)
    if not hosts["api"] or not hosts["media"]:
        log.warning("AI video: provider %r has no allowed hosts in ai_video_hosts.json; staying off", provider)
        return False
    if urllib.parse.urlparse(e["AI_VIDEO_ENDPOINT"]).netloc not in hosts["api"]:
        log.warning("AI video: AI_VIDEO_ENDPOINT host is not in the allow-list; staying off")
        return False
    return True


def check_url(url: str, allowed: set[str]) -> str:
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.netloc not in allowed or u.username or u.password:
        raise AIVideoError(f"URL host {u.netloc!r} is not allowed")
    return url


def _open(req: urllib.request.Request, timeout: int):
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)  # noqa: S310 - checked hosts


def _call(method: str, url: str, key: str, api_hosts: set[str], body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(check_url(url, api_hosts), data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with _open(req, timeout) as r:
        out = json.loads(r.read(1_000_000).decode())
    if not isinstance(out, dict):
        raise AIVideoError("unexpected API answer")
    return out


def download(url: str, media_hosts: set[str], out: Path) -> Path:
    with _open(urllib.request.Request(check_url(url, media_hosts)), 120) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise AIVideoError("AI clip too large")
    if data[4:8] != b"ftyp":
        raise AIVideoError("AI clip is not an MP4 file")
    out.write_bytes(data)
    return out


def generate(first_frame, prompt: str, seconds: float, out: Path, poll_s: float = 10,
             max_wait_s: float = 900) -> Path | None:
    """Return the downloaded mp4 path, or None (logged) on any failure. first_frame is a PIL image."""
    if not enabled():
        return None
    provider = os.environ["AI_VIDEO_PROVIDER"].strip().lower()
    hosts = _hosts(provider)
    key, endpoint = os.environ["AI_VIDEO_API_KEY"], os.environ["AI_VIDEO_ENDPOINT"].rstrip("/")
    try:
        buf = io.BytesIO()
        first_frame.convert("RGB").save(buf, "JPEG", quality=90)
        job = _call("POST", endpoint, key, hosts["api"],
                    {"prompt": prompt, "image_base64": base64.b64encode(buf.getvalue()).decode(),
                     "duration": round(seconds, 1), "aspect_ratio": "9:16"})
        job_id = urllib.parse.quote(str(job["id"]), safe="")
        waited = 0.0
        while waited < max_wait_s:
            st = _call("GET", f"{endpoint}/{job_id}", key, hosts["api"])
            if st.get("status") == "succeeded":
                return download(str(st["video_url"]), hosts["media"], out)
            if st.get("status") in ("failed", "canceled"):
                raise AIVideoError(f"provider says {st.get('status')}")
            time.sleep(poll_s)
            waited += poll_s
        raise TimeoutError("AI video took too long")
    except Exception as e:  # noqa: BLE001 - optional plug-in: never blocks an episode
        log.warning("AI video skipped (%s)", type(e).__name__)
        return None
