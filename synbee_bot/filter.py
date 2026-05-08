"""Stage 2 LLM relevance filter — Gemini Flash-Lite by default."""
from __future__ import annotations

import json
import re
import sys
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


def _parse_verdict(text: str) -> Verdict:
    """Extract the JSON line from LLM output."""
    # Try strict JSON first
    text = text.strip()
    # Strip code fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M)
    # Find first {...} block
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner="(parse failed: no JSON)", raw_response=text)
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner="(parse failed: bad JSON)", raw_response=text)
    return Verdict(
        verdict=str(data.get("verdict", "NO")).upper(),
        mission=data.get("mission"),
        score=int(data.get("score", 0)) if data.get("score") is not None else 0,
        one_liner=str(data.get("one_liner", "")),
        raw_response=text,
    )


# ---------------------------------------------------------------------------
# Provider: Gemini
# ---------------------------------------------------------------------------
def _filter_with_gemini(paper: Paper, prompt: str, model: str, api_key: str,
                        timeout: int = 30) -> Verdict:
    try:
        import google.generativeai as genai
    except ImportError:
        sys.stderr.write("google-generativeai not installed.\n")
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner="(gemini sdk missing)")
    genai.configure(api_key=api_key)
    full_prompt = _render_prompt(prompt, paper)
    try:
        m = genai.GenerativeModel(
            model,
            generation_config={"response_mime_type": "application/json",
                               "temperature": 0.1},
        )
        resp = m.generate_content(full_prompt, request_options={"timeout": timeout})
        return _parse_verdict(resp.text or "")
    except Exception as e:
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner=f"(gemini error: {e!r})")


# ---------------------------------------------------------------------------
# Provider: Anthropic Claude
# ---------------------------------------------------------------------------
def _filter_with_anthropic(paper: Paper, prompt: str, model: str, api_key: str,
                           timeout: int = 30) -> Verdict:
    try:
        import anthropic
    except ImportError:
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner="(anthropic sdk missing)")
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    full_prompt = _render_prompt(prompt, paper)
    try:
        msg = client.messages.create(
            model=model, max_tokens=400, temperature=0.1,
            messages=[{"role": "user", "content": full_prompt}],
        )
        text = "".join(block.text for block in msg.content if hasattr(block, "text"))
        return _parse_verdict(text)
    except Exception as e:
        return Verdict(verdict="NO", mission=None, score=0,
                       one_liner=f"(anthropic error: {e!r})")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def filter_paper(paper: Paper, prompt: str, *, provider: str, model: str,
                 api_key: str, timeout: int = 30) -> Verdict:
    if provider == "gemini":
        return _filter_with_gemini(paper, prompt, model, api_key, timeout)
    if provider == "anthropic":
        return _filter_with_anthropic(paper, prompt, model, api_key, timeout)
    raise ValueError(f"Unknown provider: {provider}")


def filter_batch(papers: list[Paper], *, prompt: str, provider: str,
                 model: str, api_key: str, parallel: int = 4,
                 timeout: int = 30) -> list[tuple[Paper, Verdict]]:
    if not papers:
        return []
    out: list[tuple[Paper, Verdict]] = []
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {
            ex.submit(filter_paper, p, prompt, provider=provider,
                      model=model, api_key=api_key, timeout=timeout): p
            for p in papers
        }
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                v = fut.result()
            except Exception as e:
                v = Verdict(verdict="NO", mission=None, score=0,
                            one_liner=f"(future error: {e!r})")
            out.append((p, v))
    return out


def load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")
