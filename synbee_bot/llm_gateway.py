"""Korea University BAZE (FactChat) API gateway — OpenAI-compatible chat calls.

A model name prefixed with ``baze:`` (``baze:gemini-3.8-flash``) is routed
through the university gateway instead of the paid Gemini API. Anything else
keeps the direct path, so switching back is a one-line config change.

The gateway does not proxy the native Gemini API (``generateContent`` 404s,
measured 2026-11-07), only ``/chat/completions``. JSON mode works there for
Gemini and GPT models; Claude models reject ``response_format``, so it is only
sent to the families that accept it.

The key is read from ``BAZE_API_KEY`` here rather than threaded through every
caller: the callers already carry the Gemini key for the direct fallback, and a
missing gateway key must degrade to that fallback, not crash the run.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass

import requests

PREFIX = "baze:"
BASE_URL = "https://factchat.mindlogic-kr-api.com/v1/gateway"
KEY_ENV = "BAZE_API_KEY"

_BACKOFF_SECONDS = (10, 30, 60)
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
# Families that accept response_format={"type": "json_object"} on the gateway.
_JSON_MODE_FAMILIES = ("gemini-", "gpt-")


def is_gateway_model(model: str) -> bool:
    return model.startswith(PREFIX)


def gateway_model_id(model: str) -> str:
    return model[len(PREFIX):] if is_gateway_model(model) else model


def api_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def uses_gateway(models: list[str]) -> bool:
    return any(is_gateway_model(m) for m in models)


@dataclass(frozen=True)
class GatewayUsage:
    calls: int = 0
    failures: int = 0
    prompt: int = 0
    completion: int = 0
    reasoning: int = 0

    def plus(self, other: "GatewayUsage") -> "GatewayUsage":
        return GatewayUsage(
            calls=self.calls + other.calls,
            failures=self.failures + other.failures,
            prompt=self.prompt + other.prompt,
            completion=self.completion + other.completion,
            reasoning=self.reasoning + other.reasoning,
        )

    def summary(self) -> str:
        if not self.calls and not self.failures:
            return "no gateway calls"
        n = max(1, self.calls)
        return (f"{self.calls} ok / {self.failures} failed | "
                f"prompt {self.prompt / n:,.0f}/call | "
                f"output {self.completion / n:,.0f}/call "
                f"(reasoning {self.reasoning / n:,.0f}) | "
                f"total {self.prompt + self.completion:,} tokens")


class _Ledger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = GatewayUsage()

    def add(self, usage: GatewayUsage) -> None:
        with self._lock:
            self._total = self._total.plus(usage)

    def total(self) -> GatewayUsage:
        with self._lock:
            return self._total


USAGE = _Ledger()


class GatewayError(RuntimeError):
    """The gateway gave no usable answer; the caller should fall back."""


def _usage_from(body: dict) -> GatewayUsage:
    u = body.get("usage") or {}
    details = u.get("completion_tokens_details") or {}
    return GatewayUsage(
        calls=1,
        prompt=int(u.get("prompt_tokens") or 0),
        completion=int(u.get("completion_tokens") or 0),
        reasoning=int(details.get("reasoning_tokens") or 0),
    )


def build_payload(prompt: str, model: str, *, temperature: float,
                  json_mode: bool) -> dict:
    model_id = gateway_model_id(model)
    payload: dict = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if json_mode and model_id.startswith(_JSON_MODE_FAMILIES):
        payload["response_format"] = {"type": "json_object"}
    return payload


def chat(prompt: str, model: str, *, timeout: int, temperature: float = 0.1,
         json_mode: bool = True, key: str | None = None,
         session: requests.Session | None = None) -> str:
    """One chat-completions call with retry on transient failures.

    Returns the reply text. Raises GatewayError when no answer was obtained,
    so the caller can move on to its fallback model.
    """
    key = key if key is not None else api_key()
    if not key:
        USAGE.add(GatewayUsage(failures=1))
        raise GatewayError(f"{KEY_ENV} is not set")
    http = session or requests
    payload = build_payload(prompt, model, temperature=temperature,
                            json_mode=json_mode)
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    last = "no attempt"
    for attempt in range(len(_BACKOFF_SECONDS) + 1):
        try:
            resp = http.post(f"{BASE_URL}/chat/completions/", json=payload,
                             headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            transient = True
        else:
            if resp.status_code == 200:
                try:
                    body = resp.json()
                    text = body["choices"][0]["message"]["content"] or ""
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    last = f"malformed reply: {exc}: {resp.text[:150]}"
                    transient = False
                else:
                    USAGE.add(_usage_from(body))
                    return text
            else:
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                transient = resp.status_code in _TRANSIENT_STATUS
        if transient and attempt < len(_BACKOFF_SECONDS):
            wait = _BACKOFF_SECONDS[attempt]
            sys.stderr.write(f"[baze:{payload['model']}] {last[:120]}; "
                             f"retry in {wait}s\n")
            time.sleep(wait)
            continue
        break
    USAGE.add(GatewayUsage(failures=1))
    raise GatewayError(last[:300])
