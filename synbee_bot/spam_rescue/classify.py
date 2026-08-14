"""Gemini spam/legitimate classification for one spam-folder message."""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .gmail import GmailMessage

_BACKOFF_SECONDS = (10, 30, 60)


@dataclass(frozen=True)
class Judgment:
    """One classifier verdict. `is_error` means we never got a real answer."""

    verdict: str            # "RESCUE" | "SPAM"
    category: str
    confidence: int         # 0-10
    reason: str
    model_used: str = ""
    is_error: bool = False

    @property
    def is_rescue(self) -> bool:
        return self.verdict.upper() == "RESCUE" and not self.is_error


def _error(reason: str, model: str) -> Judgment:
    return Judgment(verdict="SPAM", category="other", confidence=0,
                    reason=reason[:200], model_used=model, is_error=True)


def render_prompt(template: str, msg: GmailMessage, *, body_chars: int) -> str:
    body = msg.body_text[:body_chars] or "(empty body)"
    fields = {
        "{sender}": msg.sender or "(unknown)",
        "{reply_to}": msg.headers.get("Reply-To", "(none)"),
        "{to}": msg.headers.get("To", "(none)"),
        "{subject}": msg.subject,
        "{date}": msg.headers.get("Date", "(unknown)"),
        "{auth_results}": msg.auth_results,
        "{list_unsubscribe}": msg.headers.get("List-Unsubscribe", "(none)"),
        "{body_chars}": str(body_chars),
        "{body}": body,
    }
    out = template
    for key, value in fields.items():
        out = out.replace(key, value)
    return out


def parse_judgment(text: str, *, model_used: str = "") -> Judgment:
    """Pull the JSON object out of the model's reply."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        return _error(f"(parse failed: no JSON) {cleaned[:120]!r}", model_used)
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return _error(f"(parse failed: {exc})", model_used)

    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict not in {"RESCUE", "SPAM"}:
        return _error(f"(bad verdict: {verdict!r})", model_used)
    try:
        confidence = int(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    return Judgment(
        verdict=verdict,
        category=str(data.get("category", "other")),
        confidence=max(0, min(10, confidence)),
        reason=str(data.get("reason", "")).strip()[:200],
        model_used=model_used,
    )


def _call_gemini(prompt: str, model: str, api_key: str, timeout: int) -> Judgment:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return _error("(google-genai not installed)", model)

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=timeout * 1000),  # ms
    )
    last_err: Exception | None = None
    for attempt in range(len(_BACKOFF_SECONDS) + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            return parse_judgment(resp.text or "", model_used=model)
        except Exception as exc:  # SDK raises a wide range of transport errors
            last_err = exc
            err = str(exc)
            if "FreeTier" in err or "PerDay" in err:
                sys.stderr.write(f"[gemini:{model}] daily quota exhausted\n")
                break
            transient = any(code in err for code in
                            ("429", "RESOURCE_EXHAUSTED", "500", "INTERNAL",
                             "503", "UNAVAILABLE", "504", "DEADLINE_EXCEEDED"))
            if transient and attempt < len(_BACKOFF_SECONDS):
                wait = _BACKOFF_SECONDS[attempt]
                sys.stderr.write(f"[gemini:{model}] transient error; retry in {wait}s\n")
                time.sleep(wait)
                continue
            break
    return _error(f"(gemini error: {type(last_err).__name__}: {str(last_err)[:150]})", model)


def classify(msg: GmailMessage, template: str, *, model: str,
             fallback_models: list[str] | None = None, api_key: str,
             body_chars: int = 3000, timeout: int = 30) -> Judgment:
    """Classify one message, walking the fallback chain on transient failure."""
    prompt = render_prompt(template, msg, body_chars=body_chars)
    chain = [model, *(fallback_models or [])]
    result = _error("(no model attempted)", model)
    for index, candidate in enumerate(chain):
        result = _call_gemini(prompt, candidate, api_key, timeout)
        if not result.is_error:
            return result
        if index < len(chain) - 1:
            sys.stderr.write(f"[classify] {candidate} failed; trying {chain[index + 1]}\n")
    return result


def load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")
