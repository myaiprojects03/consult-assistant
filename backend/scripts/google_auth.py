"""Sign the assistant in to Google once and save the refresh token to .env.

    python -m scripts.google_auth

Needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env (a "Desktop app" OAuth client). A browser opens;
sign in with the Google account whose calendar and Gmail the assistant should use and allow access.
The token is written straight into .env and never printed.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.config import PROJECT_ROOT, get_settings
from app.integrations.google import SCOPES, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def save_to_env(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", lambda _: line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")


def main() -> int:
    s = get_settings()
    if not s.google_client_id or not s.google_client_secret:
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first (see docs/SETUP.md, Google).")
        return 1
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if q.get("state") == state:
                result.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in q and q.get("state") == state
            self.wfile.write(("<p>Done. You can close this tab and go back to the terminal.</p>" if ok else
                              "<p>Sign-in did not complete. Go back to the terminal.</p>").encode())

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urlencode({
        "client_id": s.google_client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print("Opening Google sign-in in your browser. If it does not open, paste this address into it:\n")
    print(url + "\n")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()
    if "code" not in result:
        print(f"Sign-in did not complete ({result.get('error', 'no answer within 5 minutes')}).")
        return 1

    r = httpx.post(TOKEN_URL, data={
        "code": result["code"], "client_id": s.google_client_id, "client_secret": s.google_client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}, timeout=20)
    if r.status_code != 200:
        print(f"Google refused the sign-in: {r.text[:300]}")
        return 1
    data = r.json()
    granted = set(data.get("scope", "").split())
    missing = [sc.rsplit("/", 1)[-1] for sc in SCOPES if sc not in granted]
    if missing:
        print(f"Some permissions were not ticked on the consent screen: {', '.join(missing)}. Run this again "
              "and allow all of them.")
        return 1
    if "refresh_token" not in data:
        print("Google did not return a refresh token. Remove the app at https://myaccount.google.com/permissions "
              "and run this again.")
        return 1
    env = PROJECT_ROOT / ".env"
    save_to_env(env, "GOOGLE_REFRESH_TOKEN", data["refresh_token"])
    print(f"Signed in. GOOGLE_REFRESH_TOKEN saved to {env}. Restart the backend to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
