"""The owner's rule: the bot ONLY makes and posts videos on its own channel. Prove no other YouTube call exists."""
import re
from pathlib import Path

import pytest

from bot import youtube

ROOT = Path(__file__).resolve().parent.parent
CODE = [p for d in ("bot", "tools", "blender") for p in (ROOT / d).rglob("*.py")]
WORKFLOWS = list((ROOT / ".github").rglob("*.yml"))

ALLOWED_URLS = {
    "https://oauth2.googleapis.com/token",
    "https://www.googleapis.com/upload/youtube/v3/videos",
    "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
    "https://www.googleapis.com/auth/youtube.upload",  # the scope id itself
}
FORBIDDEN_WORDS = [
    "commentThreads", "comments.insert", "videos.rate", "/rate", "subscriptions", "playlistItems", "playlists",
    "channels.update", "liveBroadcasts", "captions.insert", "search.list", "/youtube/v3/search", "/youtube/v3/comments",
    "/youtube/v3/channels", "/youtube/v3/activities", "youtube.force-ssl", "auth/youtube\"", "auth/youtube'",
    "youtubepartner", "youtube.readonly", "googleapiclient",
]


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def test_only_allowed_google_urls_appear_in_code():
    found = set()
    for p in CODE + WORKFLOWS:
        for m in re.finditer(r"https://[a-z0-9.-]*google(?:apis)?\.com[^\s\"'?)]*", _text(p)):
            found.add(m.group(0).rstrip(".,"))
    assert found, "the guard found no Google URLs at all, the regex is broken"
    assert found <= ALLOWED_URLS, f"unexpected Google endpoints: {sorted(found - ALLOWED_URLS)}"


def test_no_forbidden_youtube_operations_in_code():
    hits = []
    for p in CODE + WORKFLOWS:
        t = _text(p)
        for w in FORBIDDEN_WORDS:
            if w in t:
                hits.append(f"{p.relative_to(ROOT)}: {w}")
    assert not hits, hits


def test_scope_is_upload_only():
    assert youtube.SCOPE == "https://www.googleapis.com/auth/youtube.upload"
    for p in CODE:
        for m in re.finditer(r"googleapis\.com/auth/[a-z.\-]+", _text(p)):
            assert m.group(0) == "googleapis.com/auth/youtube.upload", f"{p}: {m.group(0)}"


@pytest.mark.parametrize("url", [
    "https://www.googleapis.com/youtube/v3/commentThreads?part=snippet",
    "https://www.googleapis.com/youtube/v3/videos/rate?id=x&rating=like",
    "https://www.googleapis.com/youtube/v3/subscriptions",
    "https://www.googleapis.com/upload/youtube/v3/videos?part=snippet",  # non-resumable insert is not allowed either
    "https://evil.example.com/upload/youtube/v3/videos?uploadType=resumable",
])
def test_runtime_guard_blocks_everything_else(url):
    with pytest.raises(youtube.EndpointError):
        youtube._guard(url)


def test_runtime_guard_allows_exactly_the_upload_calls():
    youtube._guard(youtube.TOKEN_URL)
    youtube._guard(youtube.UPLOAD_URL + "?uploadType=resumable&part=snippet,status")
    youtube._guard(youtube.THUMB_URL + "?videoId=abc")
    youtube._guard(youtube.UPLOAD_URL + "?uploadType=resumable&upload_id=xyz", session_ok=True)
