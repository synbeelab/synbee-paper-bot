"""Minimal Gmail REST client — refresh-token OAuth, `requests` only.

Deliberately avoids google-api-python-client: the five calls we need
(token refresh, label list/create, message list/get/modify) are a thin
wrapper, and keeping the dependency surface small keeps the Actions run fast.
"""
from __future__ import annotations

import base64
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"

# gmail.modify covers label read/write and moving messages out of SPAM.
# It cannot permanently delete anything — deletion needs the separate
# gmail.settings/full scopes, which we intentionally never request.
SCOPE = "https://www.googleapis.com/auth/gmail.modify"

_RETRY_STATUS = {429, 500, 502, 503, 504}
_BACKOFF_SECONDS = (10, 30, 60)

# Headers worth spending prompt tokens on. Authentication-Results carries the
# SPF/DKIM/DMARC verdicts, which is how a spoofed korea.ac.kr sender is told
# apart from the real one — the single most important anti-phishing signal.
_WANTED_HEADERS = (
    "From", "To", "Cc", "Reply-To", "Subject", "Date",
    "List-Unsubscribe", "Authentication-Results", "Return-Path",
)


class GmailError(RuntimeError):
    """Unrecoverable Gmail API failure."""


@dataclass(frozen=True)
class GmailMessage:
    """One spam-folder message, reduced to what the classifier needs."""

    id: str
    thread_id: str
    label_ids: tuple[str, ...]
    internal_date_ms: int
    headers: dict[str, str] = field(default_factory=dict)
    body_text: str = ""

    @property
    def sender(self) -> str:
        return self.headers.get("From", "")

    @property
    def subject(self) -> str:
        return self.headers.get("Subject", "(no subject)")

    @property
    def sender_domain(self) -> str:
        m = re.search(r"@([A-Za-z0-9._-]+)", self.sender)
        return m.group(1).lower().rstrip(">").rstrip(".") if m else ""

    @property
    def auth_results(self) -> str:
        return self.headers.get("Authentication-Results", "(none)")


def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;?", "&", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_body(payload: dict[str, Any]) -> str:
    """Depth-first walk for the best text representation of a MIME tree."""
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data and mime == "text/plain":
        return _decode_b64url(data)
    if data and mime == "text/html":
        return _strip_html(_decode_b64url(data))

    plain, html = "", ""
    for part in payload.get("parts") or []:
        got = _extract_body(part)
        if not got:
            continue
        if part.get("mimeType") == "text/plain" and not plain:
            plain = got
        elif not html:
            html = got
    return plain or html


class GmailClient:
    """Gmail REST calls with token refresh and transient-error retry."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 *, timeout: int = 30) -> None:
        if not (client_id and client_secret and refresh_token):
            raise GmailError(
                "Gmail OAuth credentials incomplete — need GMAIL_CLIENT_ID, "
                "GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._timeout = timeout
        self._session = requests.Session()
        self._access_token = ""
        self._expires_at = 0.0

    # -- auth ---------------------------------------------------------------
    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        resp = self._session.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise GmailError(
                f"token refresh failed ({resp.status_code}): {resp.text[:300]}. "
                "A revoked or expired refresh token needs "
                "`python scripts/gmail_auth_setup.py` re-run."
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._access_token

    # -- transport ----------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{API_ROOT}{path}"
        last_err = ""
        for attempt in range(len(_BACKOFF_SECONDS) + 1):
            headers = {"Authorization": f"Bearer {self._token()}"}
            resp = self._session.request(
                method, url, headers=headers, timeout=self._timeout, **kwargs
            )
            if resp.status_code < 300:
                return resp.json() if resp.content else {}
            last_err = f"{resp.status_code}: {resp.text[:300]}"
            if resp.status_code == 401:
                # Access token expired mid-run — mint a new one and go again
                # immediately; no point sleeping on an auth refresh.
                self._access_token = ""
                continue
            if resp.status_code not in _RETRY_STATUS:
                raise GmailError(f"{method} {path} failed — {last_err}")
            if attempt < len(_BACKOFF_SECONDS):
                wait = _BACKOFF_SECONDS[attempt]
                sys.stderr.write(
                    f"[gmail] {method} {path} → {resp.status_code}; retry in {wait}s\n"
                )
                time.sleep(wait)
        raise GmailError(f"{method} {path} failed after retries — {last_err}")

    # -- labels -------------------------------------------------------------
    def list_labels(self) -> dict[str, str]:
        """Map display name → label id."""
        data = self._request("GET", "/labels")
        return {lb["name"]: lb["id"] for lb in data.get("labels", [])}

    def ensure_label(self, name: str, *, background: str = "", text: str = "") -> str:
        """Return the id of `name`, creating the label if it does not exist."""
        existing = self.list_labels()
        if name in existing:
            return existing[name]
        body: dict[str, Any] = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        if background and text:
            body["color"] = {"backgroundColor": background, "textColor": text}
        created = self._request("POST", "/labels", json=body)
        return created["id"]

    # -- messages -----------------------------------------------------------
    def list_spam_message_ids(self, *, exclude_label: str = "",
                              max_results: int = 500) -> list[str]:
        """Spam-folder message ids, newest first, minus any already checked."""
        ids: list[str] = []
        params: dict[str, Any] = {
            "labelIds": "SPAM",
            "includeSpamTrash": "true",
            "maxResults": 100,
        }
        if exclude_label:
            params["q"] = f"-label:{exclude_label}"
        page_token = ""
        while len(ids) < max_results:
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", "/messages", params=params)
            ids.extend(m["id"] for m in data.get("messages", []))
            page_token = data.get("nextPageToken", "")
            if not page_token:
                break
        return ids[:max_results]

    def get_message(self, message_id: str, *, body_chars: int = 4000) -> GmailMessage:
        data = self._request(
            "GET", f"/messages/{message_id}",
            params={"format": "full"},
        )
        payload = data.get("payload") or {}
        headers = {
            h["name"]: h.get("value", "")
            for h in payload.get("headers", [])
            if h.get("name") in _WANTED_HEADERS
        }
        return GmailMessage(
            id=data["id"],
            thread_id=data.get("threadId", ""),
            label_ids=tuple(data.get("labelIds", [])),
            internal_date_ms=int(data.get("internalDate", 0)),
            headers=headers,
            body_text=_extract_body(payload)[:body_chars],
        )

    def modify(self, message_id: str, *, add: list[str] | None = None,
               remove: list[str] | None = None) -> None:
        self._request(
            "POST", f"/messages/{message_id}/modify",
            json={"addLabelIds": add or [], "removeLabelIds": remove or []},
        )
