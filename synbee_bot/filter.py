"""Stage 2 LLM relevance filter — Gemini / Anthropic with model fallback chain."""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from pathlib import Path

from .models import Paper, Verdict


def _render_prompt(template: str, paper: Paper) -> str:
    return (template
            .replace("{title}", paper.title or "")
            .replace("{journal}", paper.journal or "")
            .replace("{year}", str(paper.year or ""))
            .replace("{authors}", paper.authors_short(5))
            .replace("{abstract}", paper.abstract or "(abstract unavailable)"))


def _parse_verdict(text: str, *, model_used: str = "") -> Verdict:
    """Extract the JSON line from LLM output."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        sys.stderr.write(f"[filter] parse failed (no JSON). raw: {text[:200]!r}\n")
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner="(parse failed: no JSON)",
                       one_liner_en="(parse failed)", raw_response=text,
                       is_error=True, model_used=model_used)
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[filter] bad JSON: {e}. raw: {m.group(0)[:200]!r}\n")
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner="(parse failed: bad JSON)",
                       one_liner_en="(parse failed)", raw_response=text,
                       is_error=True, model_used=model_used)
    return Verdict(
        verdict=str(data.get("verdict", "NO")).upper(),
        mission=data.get("mission"),
        score=int(data.get("score", 0)) if data.get("score") is not None else 0,
        one_liner=str(data.get("one_liner", "")),
        one_liner_en=str(data.get("one_liner_en", "")),
        raw_response=text,
        is_error=False,
        model_used=model_used,
    )


# ---------------------------------------------------------------------------
# Provider: Gemini  (uses google-genai)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Token accounting
#
# Without this, the only way to answer "what is this bot costing?" is to guess
# from wall-clock time. Every provider call adds to the tally; run scripts print
# format_usage_summary() at the end so each run's cost lands in the CI log.
# ---------------------------------------------------------------------------
_USAGE_LOCK = Lock()
_USAGE: dict[str, int] = {"calls": 0, "in": 0, "out": 0, "think": 0, "cached": 0}

# USD per 1M tokens (input, output). Output includes thinking tokens.
_PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


def reset_usage() -> None:
    with _USAGE_LOCK:
        for k in _USAGE:
            _USAGE[k] = 0


def get_usage() -> dict[str, int]:
    with _USAGE_LOCK:
        return dict(_USAGE)


def _record_usage(usage: object) -> None:
    """Accumulate one response's usage_metadata (provider-agnostic, best-effort)."""
    if usage is None:
        return
    with _USAGE_LOCK:
        _USAGE["calls"] += 1
        _USAGE["in"] += getattr(usage, "prompt_token_count", None) or 0
        _USAGE["out"] += getattr(usage, "candidates_token_count", None) or 0
        _USAGE["think"] += getattr(usage, "thoughts_token_count", None) or 0
        _USAGE["cached"] += getattr(usage, "cached_content_token_count", None) or 0


def _record_anthropic_usage(usage: object) -> None:
    if usage is None:
        return
    with _USAGE_LOCK:
        _USAGE["calls"] += 1
        _USAGE["in"] += getattr(usage, "input_tokens", None) or 0
        _USAGE["out"] += getattr(usage, "output_tokens", None) or 0


def format_usage_summary(model: str) -> str:
    """One-line cost report. Priced at `model`; fallback-model calls are folded in,
    so treat the figure as an upper bound when a fallback actually fired."""
    u = get_usage()
    if not u["calls"]:
        return "LLM usage: no calls"
    in_price, out_price = _PRICES.get(model, (0.0, 0.0))
    billed_out = u["out"] + u["think"]
    # Gemini bills implicitly-cached input at 25% of the normal input rate.
    # `cached` is a subset of `in`, so discount that slice rather than adding it.
    fresh_in = max(0, u["in"] - u["cached"])
    cost = (fresh_in * in_price
            + u["cached"] * in_price * 0.25
            + billed_out * out_price) / 1_000_000
    per_call = cost / u["calls"]
    price_note = "" if model in _PRICES else "  (unpriced model — cost shown as $0)"
    return (
        f"LLM usage: {u['calls']} calls  "
        f"in={u['in']:,} (cached {u['cached']:,})  "
        f"out={u['out']:,}  thinking={u['think']:,}  "
        f"≈${cost:.4f} (${per_call:.5f}/call){price_note}"
    )


def _filter_with_gemini(paper: Paper, prompt: str, model: str, api_key: str,
                        timeout: int = 30, thinking_budget: int = 1024) -> Verdict:
    """Single-model Gemini call with retry on transient errors. is_error=True
    on any unrecoverable failure so the caller can try a fallback model."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.stderr.write("google-genai not installed (run: pip install google-genai).\n")
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner="(gemini sdk missing)",
                       one_liner_en="(gemini sdk missing)",
                       is_error=True, model_used=model)
    full_prompt = _render_prompt(prompt, paper)
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=timeout * 1000),  # ms
    )

    max_attempts = 3
    backoff = [10, 30, 60]
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    # Gemini 2.5 thinks by default with a dynamic budget. Measured
                    # ~1,900 thinking tokens per abstract vs ~140 tokens of useful
                    # JSON — thinking is billed at the output rate, so it was ~85%
                    # of the bill. Capping at 1024 cuts cost ~48%.
                    #
                    # Do NOT lower this much further: at budget=0 the model
                    # misscored borderline papers badly (score 7-8 -> 1), i.e. it
                    # silently drops relevant papers. 1024 was the safe knee.
                    # thinking_budget=-1 restores the old dynamic behaviour
                    # (uncapped); the output cap is then omitted, since we cannot
                    # know how much the model will think.
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=thinking_budget),
                    # max_output_tokens COUNTS THINKING TOKENS in Gemini 2.5.
                    # A small absolute cap starves the JSON and every parse fails,
                    # so this must stay tied to the thinking budget.
                    max_output_tokens=(
                        thinking_budget + 500 if thinking_budget >= 0 else None),
                ),
            )
            _record_usage(getattr(resp, "usage_metadata", None))
            return _parse_verdict(resp.text or "", model_used=model)
        except Exception as e:
            last_err = e
            err_str = str(e)
            is_rate_limit = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
            is_daily_quota = "FreeTier" in err_str or "PerDay" in err_str
            is_server_overload = (
                "503" in err_str or "UNAVAILABLE" in err_str
                or "500" in err_str or "INTERNAL" in err_str
                or "504" in err_str or "DEADLINE_EXCEEDED" in err_str
            )
            if is_daily_quota:
                sys.stderr.write(f"[gemini:{model}] DAILY QUOTA EXHAUSTED.\n")
                break
            if (is_rate_limit or is_server_overload) and attempt < max_attempts - 1:
                wait = backoff[attempt]
                kind = "rate limit" if is_rate_limit else "server overload"
                sys.stderr.write(f"[gemini:{model}] {kind} (attempt {attempt+1}); sleeping {wait}s\n")
                time.sleep(wait)
                continue
            break
    sys.stderr.write(f"[gemini:{model}] error on {paper.id[:40]}: {type(last_err).__name__}: {str(last_err)[:200]}\n")
    return Verdict(verdict="NO", mission=None, score=0,
                   one_liner=f"(gemini error on {model}: {last_err!r})"[:300],
                   one_liner_en=f"(gemini error on {model})",
                   is_error=True, model_used=model)


# ---------------------------------------------------------------------------
# Provider: Anthropic Claude
# ---------------------------------------------------------------------------
def _filter_with_anthropic(paper: Paper, prompt: str, model: str, api_key: str,
                           timeout: int = 30) -> Verdict:
    try:
        import anthropic
    except ImportError:
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner="(anthropic sdk missing)",
                       one_liner_en="(anthropic sdk missing)",
                       is_error=True, model_used=model)
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    full_prompt = _render_prompt(prompt, paper)
    try:
        msg = client.messages.create(
            model=model, max_tokens=400, temperature=0.1,
            messages=[{"role": "user", "content": full_prompt}],
        )
        _record_anthropic_usage(getattr(msg, "usage", None))
        text = "".join(block.text for block in msg.content if hasattr(block, "text"))
        return _parse_verdict(text, model_used=model)
    except Exception as e:
        sys.stderr.write(f"[anthropic:{model}] error: {type(e).__name__}: {str(e)[:200]}\n")
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner=f"(anthropic error: {e!r})"[:300],
                       one_liner_en="(anthropic error)",
                       is_error=True, model_used=model)


# ---------------------------------------------------------------------------
# Public API — single paper (with fallback chain)
# ---------------------------------------------------------------------------
def filter_paper(paper: Paper, prompt: str, *, provider: str, model: str,
                 fallback_models: list[str] | None = None,
                 api_key: str, timeout: int = 30,
                 thinking_budget: int = 1024) -> Verdict:
    """Filter a paper using `model`; on transient/SDK errors, try each model
    in `fallback_models` in order. Returns first successful (non-error) verdict,
    or the last error verdict if all attempts fail."""
    chain = [model] + list(fallback_models or [])
    last_v: Verdict | None = None
    for i, m in enumerate(chain):
        if provider == "gemini":
            v = _filter_with_gemini(paper, prompt, m, api_key, timeout,
                                    thinking_budget=thinking_budget)
        elif provider == "anthropic":
            v = _filter_with_anthropic(paper, prompt, m, api_key, timeout)
        else:
            raise ValueError(f"Unknown provider: {provider}")
        last_v = v
        if not v.is_error:
            return v
        # Error → try next model in chain
        if i < len(chain) - 1:
            sys.stderr.write(f"[filter] {m} failed; falling back to {chain[i+1]}\n")
    return last_v  # all failed — return last error verdict


def filter_batch(papers: list[Paper], *, prompt: str, provider: str,
                 model: str, fallback_models: list[str] | None = None,
                 api_key: str, parallel: int = 4,
                 timeout: int = 30,
                 thinking_budget: int = 1024) -> list[tuple[Paper, Verdict]]:
    if not papers:
        return []
    out: list[tuple[Paper, Verdict]] = []
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {
            ex.submit(filter_paper, p, prompt, provider=provider,
                      model=model, fallback_models=fallback_models,
                      api_key=api_key, timeout=timeout,
                      thinking_budget=thinking_budget): p
            for p in papers
        }
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                v = fut.result()
            except Exception as e:
                v = Verdict(verdict="NO", mission=None, score=0,
                            one_liner=f"(future error: {e!r})",
                            one_liner_en="(future error)",
                            is_error=True)
            out.append((p, v))
    return out


def load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")
