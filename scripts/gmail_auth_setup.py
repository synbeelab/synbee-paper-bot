#!/usr/bin/env python
"""One-time helper: mint a Gmail refresh token for the spam-rescue bot.

Run this ONCE on a machine with a browser:

    python scripts/gmail_auth_setup.py --client-id XXX --client-secret YYY

It opens Google's consent screen, catches the redirect on localhost, and
prints the refresh token to paste into the GMAIL_REFRESH_TOKEN GitHub secret.
The token is printed, never written to disk.

Requires an OAuth client of type **Desktop app** (Google Cloud Console →
APIs & Services → Credentials) in a project with the Gmail API enabled.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"

_PAGE = (
    "<html><body style='font-family:sans-serif;padding:3rem'>"
    "<h2>{title}</h2><p>{body}</p></body></html>"
)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot handler that captures ?code= from the OAuth redirect."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        ok = "code" in _CallbackHandler.result
        page = _PAGE.format(
            title="인증 완료" if ok else "인증 실패",
            body=("이 창을 닫고 터미널로 돌아가세요."
                  if ok else f"오류: {_CallbackHandler.result.get('error', 'unknown')}"),
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def log_message(self, *_args: object) -> None:
        pass  # keep the console clean


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a Gmail refresh token")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()

    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}"
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_url = f"{AUTH_URL}?" + urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",          # force a refresh token even on re-auth
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print(f"\n브라우저에서 Google 로그인 창이 열립니다. 열리지 않으면 이 URL을 직접 여세요:\n\n{auth_url}\n")
    webbrowser.open(auth_url)

    # handle_request() serves exactly one request, then the thread ends.
    deadline = time.monotonic() + 300
    while not _CallbackHandler.result and time.monotonic() < deadline:
        time.sleep(0.5)
    result = _CallbackHandler.result
    if not result:
        sys.stderr.write("시간 초과 — 인증이 완료되지 않았습니다.\n")
        return 1
    if result.get("state") != state:
        sys.stderr.write("state 불일치 — 중단합니다.\n")
        return 1
    if "code" not in result:
        sys.stderr.write(f"인증 거부됨: {result.get('error', 'unknown')}\n")
        return 1

    resp = requests.post(TOKEN_URL, data={
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "code": result["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=30)
    if resp.status_code != 200:
        sys.stderr.write(f"토큰 교환 실패 ({resp.status_code}): {resp.text[:400]}\n")
        return 1

    refresh_token = resp.json().get("refresh_token", "")
    if not refresh_token:
        sys.stderr.write(
            "refresh_token이 반환되지 않았습니다. "
            "https://myaccount.google.com/permissions 에서 이 앱의 권한을 제거한 뒤 다시 실행하세요.\n"
        )
        return 1

    print("\n" + "=" * 72)
    print("GitHub repo secret에 아래 3개를 등록하세요:")
    print("  Settings → Secrets and variables → Actions → New repository secret\n")
    print(f"  GMAIL_CLIENT_ID     = {args.client_id}")
    print(f"  GMAIL_CLIENT_SECRET = {args.client_secret}")
    print(f"  GMAIL_REFRESH_TOKEN = {refresh_token}")
    print("=" * 72 + "\n")
    print("이 토큰은 파일로 저장되지 않았습니다. 터미널 스크롤백도 정리해 두세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
