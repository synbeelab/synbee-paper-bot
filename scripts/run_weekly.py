"""
SynBEE Paper Bot — WEEKLY journal-sweep digest ("delta" vs daily).

Queries PubMed by JOURNAL ONLY (no keyword gate) for the last N days, dedups
against the SAME seen.db the daily bot uses (so daily-caught papers are
excluded), LLM-filters with Anthropic Haiku, and posts the delta to the weekly
Slack channel (#논문-알림).

Usage:
    python scripts/run_weekly.py
    python scripts/run_weekly.py --dry-run
    python scripts/run_weekly.py --no-llm --since-days 2 --limit 10
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot.config import load_config  # noqa: E402
from synbee_bot.filter import filter_batch, load_prompt  # noqa: E402
from synbee_bot.models import Paper, Verdict  # noqa: E402
from synbee_bot.slack_dispatch import (  # noqa: E402
    make_slack_client, post_papers, post_source_alert, post_summary,
)
from synbee_bot.sources import fetch_from_pubmed_weekly  # noqa: E402
from synbee_bot.storage import (  # noqa: E402
    SeenDB, effective_since_days, split_persist_vs_retry,
)

WEEKLY_TITLE = "🐝 SynBEE 주간 논문 다이제스트 (delta)"

# Tracked separately from the daily bot's "pubmed": a different query (journal
# only, no keyword gate) on a different cadence, so it owns its own window.
WEEKLY_SOURCE = "weekly_pubmed"


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def _post_zero_summary(cfg, args: argparse.Namespace, *,
                       collected: int, new: int, passed: int) -> None:
    """Post a summary-only card when there is nothing to push.

    Silence is not an acceptable output: the user cannot tell "no new papers
    this week" apart from "the workflow is broken". The 2026-07-27 scheduled
    run hit exactly this and looked identical to a dead bot.
    """
    if args.dry_run or args.no_slack:
        return
    if not (cfg.slack_enabled and cfg.slack_bot_token and cfg.weekly_channel):
        return
    stats = {
        "date": dt.date.today().isoformat(),
        "collected": collected,
        "new": new,
        "passed": passed,
        "posted": 0,
    }
    try:
        post_summary(make_slack_client(cfg.slack_bot_token),
                     cfg.weekly_channel, stats, title=WEEKLY_TITLE)
    except Exception as e:  # a failed report must never kill the run
        sys.stderr.write(f"  ! zero-summary post failed: {e}\n")


def _alert_sweep_failure(cfg, args: argparse.Namespace, reason: str) -> None:
    """Tell Slack the sweep died.

    Without this the week is indistinguishable from a quiet one — worse, the
    zero report the reader is used to seeing never arrives either, so the
    absence itself carries no information.
    """
    if args.dry_run or args.no_slack:
        return
    if not (cfg.slack_enabled and cfg.slack_bot_token and cfg.weekly_channel):
        return
    post_source_alert(cfg.slack_bot_token, cfg.weekly_channel,
                      {"weekly PubMed sweep": reason}, dt.date.today().isoformat())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-slack", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--since-days", type=int)
    ap.add_argument("--limit", type=int, help="Cap papers fetched (debug)")
    ap.add_argument("--min-score", type=int)
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.weekly_enabled:
        _log("weekly disabled in config. Exiting.")
        return 0

    min_score = args.min_score if args.min_score is not None else cfg.weekly_min_score
    db = SeenDB(cfg.seen_db_path)

    # A weekly run that dies leaves a 7-day hole, and the next run starts after
    # it. Reach back to the last delivery instead of assuming the previous run
    # worked. Journal-only sweep = these papers have no other route in.
    if args.since_days:
        since = args.since_days
    else:
        last = db.get_source_watermark(WEEKLY_SOURCE)
        since = effective_since_days(cfg.weekly_since_days, last,
                                     max_days=cfg.max_since_days)
        if since > cfg.weekly_since_days:
            _log(f"  ↺ widening window to {since}d "
                 f"(last delivered {last.isoformat() if last else 'never'})")

    _log(f"Weekly keyword+journal PubMed sweep (since {since}d)…")
    try:
        flat = fetch_from_pubmed_weekly(since_days=since)
    except Exception as e:
        # One source means nothing to fall back on, so this run delivers
        # nothing — but it must not look like a quiet week, and the watermark
        # must stay put so next week's sweep covers these days too.
        reason = f"{type(e).__name__}: {e}"
        _log(f"❌ weekly sweep FAILED — {reason}")
        _alert_sweep_failure(cfg, args, reason)
        db.close()
        return 1
    if args.limit:
        flat = flat[: args.limit]
    _log(f"  pubmed(journal-only): {len(flat)} papers")

    by_id: dict[str, Paper] = {}
    for p in flat:
        by_id.setdefault(p.id, p)
    flat = list(by_id.values())

    unseen_ids = db.filter_unseen(p.id for p in flat)
    new_papers = [p for p in flat if p.id in unseen_ids]
    _log(f"Total {len(flat)} → {len(new_papers)} new after dedup vs daily seen.db")

    if not new_papers:
        _log("No delta papers (all already seen by daily). Reporting zero and exiting.")
        _post_zero_summary(cfg, args, collected=len(flat), new=0, passed=0)
        # Zero delta is a real answer, not a failure: the sweep ran and found
        # nothing new. Advance, or a quiet stretch widens the window forever.
        if not args.dry_run:
            db.mark_source_success(WEEKLY_SOURCE)
        db.close()
        return 0

    if cfg.llm_enabled and not args.no_llm:
        provider = cfg.weekly_llm_provider
        api_key = cfg.anthropic_api_key if provider == "anthropic" else cfg.gemini_api_key
        if not api_key:
            _log(f"⚠️  API key for provider '{provider}' missing — skipping LLM filter.")
            results = [(p, Verdict("YES", None, 5, "(no LLM filter)")) for p in new_papers]
        else:
            prompt = load_prompt(cfg.llm_prompt_path)
            _log(f"LLM filter ({provider}/{cfg.weekly_llm_model}) on {len(new_papers)} papers…")
            results = filter_batch(
                new_papers, prompt=prompt,
                provider=provider, model=cfg.weekly_llm_model,
                fallback_models=cfg.weekly_llm_fallback_models,
                api_key=api_key, parallel=8, timeout=cfg.llm_timeout,
            )
    else:
        results = [(p, Verdict("YES", None, 5, "(LLM filter disabled)")) for p in new_papers]

    results.sort(key=lambda pv: -pv[1].score)
    passing = [(p, v) for p, v in results if v.is_yes and v.score >= min_score]
    _log(f"Filter: {len(passing)} pass / {len(results)} total (min_score={min_score})")

    # Report zero explicitly, but do NOT return here: the split_persist_vs_retry
    # bookkeeping below still has to run so judged-NO papers get marked seen
    # (otherwise every run re-sends the same rejects to the LLM).
    if not passing:
        _log("Nothing passed the filter — posting a zero report so silence is never the output.")
        _post_zero_summary(cfg, args, collected=len(flat), new=len(new_papers), passed=0)

    # Optional post cap (default: none). Excess is held back for the next run
    # rather than dropped, since a dropped paper would be marked seen and lost.
    held_back: list[tuple[Paper, Verdict]] = []
    if cfg.weekly_max_posts is not None and len(passing) > cfg.weekly_max_posts:
        held_back = passing[cfg.weekly_max_posts:]
        passing = passing[: cfg.weekly_max_posts]
        _log(f"⚠️  post cap {cfg.weekly_max_posts} hit — holding back "
             f"{len(held_back)} passing papers for the next run (not marked seen)")

    print()
    for p, v in passing:
        flag = "★" * min(5, max(1, (v.score + 1) // 2))
        print(f"{flag} [{v.score}/10] [{p.journal}] {p.title[:80]}")
        if v.one_liner:
            print(f"     KR: {v.one_liner}")
        print(f"     {p.url}")

    # Post BEFORE persisting: a paper is only marked seen once it actually
    # reached Slack, otherwise a failed post loses it permanently.
    posted = 0
    post_failures: list[tuple[Paper, Verdict]] = []
    if (not args.no_slack and not args.dry_run and cfg.slack_enabled
            and cfg.slack_bot_token and cfg.weekly_channel and passing):
        summary = {
            "date": dt.date.today().isoformat(),
            "collected": len(flat),
            "new": len(new_papers),
            "passed": len(passing),
        }
        _log(f"Posting {len(passing)} to weekly channel {cfg.weekly_channel}…")
        posted, post_failures = post_papers(cfg.slack_bot_token, cfg.weekly_channel,
                                            passing, summary=summary, title=WEEKLY_TITLE)
        _log(f"Posted {posted} messages ({len(post_failures)} failed).")
    elif args.dry_run:
        _log("Dry run — no Slack post, DB not updated.")

    # Only record papers we are done with; the rest are retried next run.
    if not args.dry_run:
        persist, retry = split_persist_vs_retry(
            results, held_back=held_back, post_failures=post_failures)
        for p, v in persist:
            db.mark_seen(p, v)
        _log(f"Persisted {len(persist)} verdicts to seen.db")
        if retry:
            error_count = sum(1 for _, v in results if v.is_error)
            _log(f"↻ {len(retry)} papers left unseen for retry next run "
                 f"(errors={error_count}, held back={len(held_back)}, "
                 f"post failures={len(post_failures)})")
        # Delivered — safe to move the window forward.
        db.mark_source_success(WEEKLY_SOURCE)

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
