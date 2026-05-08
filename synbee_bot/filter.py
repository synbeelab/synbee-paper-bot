"""Stage 2 LLM relevance filter — Gemini / Anthropic with model fallback chain."""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
def _filter_with_gemini(paper: Paper, prompt: str, model: str, api_key: str,
                        timeout: int = 30) -> Verdict:
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
                ),
            )
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
                 api_key: str, timeout: int = 30) -> Verdict:
    """Filter a paper using `model`; on transient/SDK errors, try each model
    in `fallback_models` in order. Returns first successful (non-error) verdict,
    or the last error verdict if all attempts fail."""
    chain = [model] + list(fallback_models or [])
    last_v: Verdict | None = None
    for i, m in enumerate(chain):
        if provider == "gemini":
            v = _filter_with_gemini(paper, prompt, m, api_key, timeout)
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
                 timeout: int = 30) -> list[tuple[Paper, Verdict]]:
    if not papers:
        return []
    out: list[tuple[Paper, Verdict]] = []
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {
            ex.submit(filter_paper, p, prompt, provider=provider,
                      model=model, fallback_models=fallback_models,
                      api_key=api_key, timeout=timeout): p
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
