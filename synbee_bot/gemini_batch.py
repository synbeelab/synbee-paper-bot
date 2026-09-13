"""Stage 2 filter over the Gemini Batch API — same verdicts, half the price.

Why this and nothing else
-------------------------
Measured 2026-08-30 on 30 real papers from the week's sweep, gemini-2.5-flash:

    prompt 1,356 tok · output 112 tok · **thinking 1,384 tok** → $0.00405/call
    thinking alone is 85.4% of the bill.

Every cheap-looking lever aims at that 85% and every one of them costs papers.
`thinking_budget=0` was measured on the same 30: 4 of the 5 papers that pass at
min_score 6 stopped passing. `thinking_budget=2048` — MORE thinking than the
dynamic default actually spends — still flipped one, so the filter's own
run-to-run variance is on the order of one borderline paper in five, and no
budget setting can be shown to be safe without a replay gate much larger than
the saving justifies.

Batch mode is the exception: identical model, identical prompt, identical
(unset, therefore dynamic) thinking, billed at exactly half. Input $0.30 →
$0.15/M, output-plus-thinking $2.50 → $1.25/M. The verdict distribution is not
approximately preserved, it is the same computation. The only thing traded is
latency, and a weekly digest has latency to spare.

The delivery guarantee is unchanged
-----------------------------------
Everything here is best-effort. Submission failure, job failure, a job that is
still running at the deadline, a malformed line in the result file — every path
returns the affected papers as "unfinished" so the caller filters them the
interactive way and posts on time. Worst case is today's cost and today's
schedule; there is no path where a paper is dropped because the batch was slow.

A job we stop waiting for is cancelled, so an abandoned batch is not billed
alongside the interactive re-run that replaced it.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from .filter import _parse_verdict, _render_prompt
from .models import Paper, Verdict

TERMINAL_STATES = {
    "JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED",
}


def _extract_text(response: dict) -> str:
    """Pull the model text out of a serialized GenerateContentResponse."""
    if not isinstance(response, dict):
        return ""
    if isinstance(response.get("text"), str):        # some SDK versions flatten it
        return response["text"]
    for cand in response.get("candidates") or []:
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if text:
            return text
    return ""


def _usage(response: dict) -> tuple[int, int, int]:
    """(prompt, output, thinking) tokens — logged so the bill stays visible."""
    u = (response or {}).get("usageMetadata") or {}
    return (int(u.get("promptTokenCount") or 0),
            int(u.get("candidatesTokenCount") or 0),
            int(u.get("thoughtsTokenCount") or 0))


def _write_requests(papers: list[Paper], prompt: str, path: Path) -> None:
    """One JSONL line per paper, keyed by position so results cannot be mismatched.

    The Batch API does not promise that inline responses come back in request
    order, and a verdict attached to the wrong paper is a worse bug than any
    amount of overspend — so every request carries an explicit key.
    """
    with path.open("w", encoding="utf-8") as f:
        for i, paper in enumerate(papers):
            f.write(json.dumps({
                "key": f"p{i}",
                "request": {
                    "contents": [{
                        "role": "user",
                        "parts": [{"text": _render_prompt(prompt, paper)}],
                    }],
                    "generation_config": {
                        "temperature": 0.1,
                        "response_mime_type": "application/json",
                    },
                },
            }, ensure_ascii=False) + "\n")


def _cancel(client, job_name: str, log) -> None:
    try:
        client.batches.cancel(name=job_name)
        log(f"  batch {job_name} cancelled (not billed for the interactive re-run)")
    except Exception as e:  # cancellation is a courtesy, not a requirement
        sys.stderr.write(f"  ! could not cancel batch {job_name}: {e}\n")


def filter_batch_offline(
    papers: list[Paper], *, prompt: str, model: str, api_key: str,
    deadline_seconds: int, poll_seconds: int = 30,
    log=lambda msg: None,
) -> tuple[list[tuple[Paper, Verdict]], list[Paper]]:
    """Filter `papers` through the Batch API.

    Returns (results, unfinished). `unfinished` is whatever the batch did not
    deliver a usable verdict for — the caller must run those through the normal
    interactive filter. Both lists together always cover every input paper.
    """
    if not papers:
        return [], []

    try:
        from google import genai
    except ImportError:
        sys.stderr.write("google-genai not installed — batch filter unavailable.\n")
        return [], list(papers)

    started = time.monotonic()
    tmpdir = Path(tempfile.mkdtemp(prefix="synbee-batch-"))
    src = tmpdir / "requests.jsonl"

    try:
        client = genai.Client(api_key=api_key)
        _write_requests(papers, prompt, src)
        size_mb = src.stat().st_size / 1e6
        log(f"Batch filter: submitting {len(papers)} papers ({size_mb:.1f} MB) "
            f"to {model} (deadline {deadline_seconds // 60} min)…")
        uploaded = client.files.upload(
            file=str(src),
            config={"display_name": "synbee-batch-requests", "mime_type": "jsonl"},
        )
        job = client.batches.create(
            model=model, src=uploaded.name,
            config={"display_name": f"synbee-weekly-{dt.date.today().isoformat()}"},
        )
    except Exception as e:
        sys.stderr.write(f"[batch] submit failed ({type(e).__name__}: {e}) — "
                         f"falling back to interactive for all {len(papers)}\n")
        return [], list(papers)
    finally:
        # The request file lives only until the upload is accepted.
        shutil.rmtree(tmpdir, ignore_errors=True)

    job_name = job.name
    log(f"  batch job {job_name} submitted")

    # --- poll to the deadline --------------------------------------------
    state = ""
    while True:
        try:
            job = client.batches.get(name=job_name)
            state = job.state.name if job.state else ""
        except Exception as e:
            sys.stderr.write(f"[batch] poll failed ({type(e).__name__}: {e})\n")
            state = ""
        if state in TERMINAL_STATES:
            break
        elapsed = time.monotonic() - started
        if elapsed >= deadline_seconds:
            log(f"  ⏱ batch still {state or 'unknown'} after "
                f"{elapsed / 60:.0f} min — falling back to interactive")
            _cancel(client, job_name, log)
            return [], list(papers)
        time.sleep(poll_seconds)

    elapsed = time.monotonic() - started
    if state != "JOB_STATE_SUCCEEDED":
        log(f"  ✗ batch ended {state} after {elapsed / 60:.1f} min — "
            f"falling back to interactive")
        return [], list(papers)
    log(f"  ✓ batch succeeded in {elapsed / 60:.1f} min")

    # --- collect ----------------------------------------------------------
    try:
        raw = client.files.download(file=job.dest.file_name)
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except Exception as e:
        sys.stderr.write(f"[batch] result download failed "
                         f"({type(e).__name__}: {e})\n")
        return [], list(papers)

    verdicts: dict[int, Verdict] = {}
    tok_in = tok_out = tok_think = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            idx = int(str(row.get("key", "")).lstrip("p"))
        except (ValueError, json.JSONDecodeError):
            continue
        if not 0 <= idx < len(papers):
            continue
        response = row.get("response")
        if not response:
            continue                       # per-request error → interactive retry
        body = _extract_text(response)
        if not body:
            continue
        v = _parse_verdict(body, model_used=model)
        if v.is_error:
            continue                       # unparseable → interactive retry
        verdicts[idx] = v
        a, b, c = _usage(response)
        tok_in, tok_out, tok_think = tok_in + a, tok_out + b, tok_think + c

    results = [(papers[i], v) for i, v in sorted(verdicts.items())]
    unfinished = [p for i, p in enumerate(papers) if i not in verdicts]
    if tok_in or tok_think:
        log(f"  batch tokens: prompt {tok_in:,} · output {tok_out:,} · "
            f"thinking {tok_think:,} (billed at 50%)")
    log(f"  batch delivered {len(results)}/{len(papers)} verdicts"
        + (f", {len(unfinished)} to interactive" if unfinished else ""))
    return results, unfinished
