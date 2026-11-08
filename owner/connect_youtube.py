"""One-time: let the PIXELHELM channel sign the bot in, then hand the key straight to GitHub.

Run by owner/connect.ps1. Opens Google's own sign-in page in the browser; the owner picks the PIXELHELM channel and
presses Allow. The client id, client secret and refresh token go from memory into `gh secret set` (stdin), and are
never printed or written to disk. Standard library only.

    python -I connect_youtube.py <client_secret.json> <owner/repo> <gh.exe> <env|repo>
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
WAIT_SECONDS = 600

DONE_PAGE = (
    "<html><body style='font-family:sans-serif;background:#111;color:#eee;padding:48px'>"
    "<h2>{}</h2><p>You can close this tab.</p></body></html>"
)


def read_client(path: str) -> tuple[str, str]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    c = data.get("installed") or data.get("web")
    if not c or not c.get("client_id") or not c.get("client_secret"):
        sys.exit("That file isn't a Google OAuth client file (client_secret_....json).")
    return c["client_id"], c["client_secret"]


def sign_in(client_id: str) -> tuple[str, str, str]:
    """Return (code, redirect_uri, verifier) after the owner presses Allow."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 5  # a silent local connection can't hold up sign-in

        def do_GET(self):  # noqa: N802 (http.server name)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Connected. The bot can now upload to the channel." if ok else "Not connected (you pressed Cancel)."
            self.wfile.write(DONE_PAGE.format(msg).encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 2
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent select_account",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    print("A Google page is opening. Choose the PIXELHELM channel, then:")
    print('  "Google hasn\'t verified this app" -> Advanced -> Go to PIXELHELM channel bot -> Continue.')
    print("If no Google page opened, copy this whole link into your browser (it holds no secret):")
    print(url)
    webbrowser.open(url)
    deadline = time.monotonic() + WAIT_SECONDS
    while not got and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in got:
        sys.exit("Google sign-in didn't finish (" + got.get("error", "timed out") + "). Run connect again.")
    return got["code"], redirect, verifier


def exchange(client_id: str, client_secret: str, code: str, redirect: str, verifier: str) -> str:
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r)
    scopes = set((tok.get("scope") or "").split())
    if scopes != {SCOPE}:
        sys.exit(f"Google gave the wrong permissions ({sorted(scopes)}); only youtube.upload is allowed. Run again.")
    if not tok.get("refresh_token"):
        sys.exit("Google didn't send a long-lasting key. Remove the app at myaccount.google.com/permissions and run again.")
    return tok["refresh_token"]


def store(gh: str, repo: str, values: dict[str, str], use_env: bool) -> str:
    """Put all three secrets in one place: the `youtube` environment, or the repository when the plan has none.

    connect.ps1 decides which (it checks the environment exists), so a partial failure never splits them.
    """
    where = ["--env", "youtube"] if use_env else []
    for name, value in values.items():
        r = subprocess.run([gh, "secret", "set", name, "--repo", repo, *where],
                           input=value, text=True, capture_output=True)
        if r.returncode != 0:
            sys.exit(f"GitHub didn't accept {name}. Run connect again (it overwrites all three).")
    return "the youtube environment" if use_env else "the repository's secrets"


def main() -> None:
    if len(sys.argv) != 5 or sys.argv[4] not in ("env", "repo"):
        sys.exit(__doc__)
    client_path, repo, gh, where = sys.argv[1:]
    client_id, client_secret = read_client(client_path)
    code, redirect, verifier = sign_in(client_id)
    refresh = exchange(client_id, client_secret, code, redirect, verifier)
    where = store(gh, repo, {
        "YT_CLIENT_ID": client_id,
        "YT_CLIENT_SECRET": client_secret,
        "YT_REFRESH_TOKEN": refresh,
    }, where == "env")
    print(f"YouTube connected (upload-only key saved in {where}).")


if __name__ == "__main__":
    main()
