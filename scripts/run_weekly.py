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
from synbee_bot.slack_dispatch import post_papers  # noqa: E402
from synbee_bot.sources import fetch_from_pubmed_journals_only  # noqa: E402
from synbee_bot.storage import SeenDB  # noqa: E402

WEEKLY_TITLE = "🐝 SynBEE 주간 논문 다이제스트 (delta)"


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


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

    since = args.since_days or cfg.weekly_since_days
    min_score = args.min_score if args.min_score is not None else cfg.weekly_min_score
    db = SeenDB(cfg.seen_db_path)

    _log(f"Weekly journal-only PubMed sweep (since {since}d)…")
    flat = fetch_from_pubmed_journals_only(since_days=since)
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
        _log("No delta papers (all already seen by daily). Exiting.")
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
                api_key=api_key, parallel=cfg.llm_parallel, timeout=cfg.llm_timeout,
            )
    else:
        results = [(p, Verdict("YES", None, 5, "(LLM filter disabled)")) for p in new_papers]

    results.sort(key=lambda pv: -pv[1].score)
    passing = [(p, v) for p, v in results if v.is_yes and v.score >= min_score]
    _log(f"Filter: {len(passing)} pass / {len(results)} total (min_score={min_score})")
    passing = passing[: cfg.weekly_max_posts]

    print()
    for p, v in passing:
        flag = "★" * min(5, max(1, (v.score + 1) // 2))
        print(f"{flag} [{v.score}/10] [{p.journal}] {p.title[:80]}")
        if v.one_liner:
            print(f"     KR: {v.one_liner}")
        print(f"     {p.url}")

    if not args.dry_run:
        for p, v in results:
            db.mark_seen(p, v)
        _log(f"Persisted {len(results)} verdicts to seen.db")

    posted = 0
    if (not args.no_slack and not args.dry_run and cfg.slack_enabled
            and cfg.slack_bot_token and cfg.weekly_channel and passing):
        summary = {
            "date": dt.date.today().isoformat(),
            "collected": len(flat),
            "new": len(new_papers),
            "passed": len(passing),
        }
        _log(f"Posting {len(passing)} to weekly channel {cfg.weekly_channel}…")
        posted, failed = post_papers(cfg.slack_bot_token, cfg.weekly_channel,
                                     passing, summary=summary, title=WEEKLY_TITLE)
        _log(f"Posted {posted} messages ({failed} failed).")
    elif args.dry_run:
        _log("Dry run — no Slack post, DB not updated.")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
