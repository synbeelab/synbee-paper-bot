"""
SynBEE Paper Bot — daily run.

End-to-end:
  1. Collect from PubMed + bioRxiv + RSS
  2. Dedup against seen.db
  3. Stage 2 LLM filter (Gemini Flash-Lite by default)
  4. Push papers passing min_score threshold to Slack

Usage:
    python scripts/run_daily.py
    python scripts/run_daily.py --dry-run            # collect+filter, no Slack post
    python scripts/run_daily.py --no-slack           # skip post, just print
    python scripts/run_daily.py --since-days 7       # override config
    python scripts/run_daily.py --no-llm             # skip LLM filter
    python scripts/run_daily.py --limit 5            # cap for debug
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr so Korean + em-dashes don't crash on Windows cp949.
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
from synbee_bot.slack_dispatch import post_papers, post_source_alert  # noqa: E402
from synbee_bot.sources import collect_all  # noqa: E402
from synbee_bot.storage import (  # noqa: E402
    SeenDB, effective_since_days, split_persist_vs_retry,
)


def _human_log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def _advance_watermarks(db: SeenDB, succeeded: set[str], *, dry_run: bool) -> None:
    """Record that these sources made it all the way through.

    Only reached once the run has posted and persisted. Advancing earlier would
    let a crash between collection and delivery move the window past papers that
    were never actually delivered.
    """
    if dry_run:
        return
    for source in sorted(succeeded):
        db.mark_source_success(source)


def _window(db: SeenDB, source: str, configured: int, cfg, override: int | None) -> int:
    """Days to reach back for one source.

    An explicit --since-days is a deliberate backfill and wins outright.
    Otherwise the window stretches to cover every day since this source last
    delivered, so a crashed run does not leave a permanent hole.
    """
    if override:
        return override
    last = db.get_source_watermark(source)
    days = effective_since_days(configured, last, max_days=cfg.max_since_days)
    if days > configured:
        _human_log(
            f"  ↺ {source}: widening window to {days}d "
            f"(last delivered {last.isoformat() if last else 'never'})")
    return days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Collect + filter but do not push to Slack or persist verdicts")
    ap.add_argument("--no-slack", action="store_true", help="Skip Slack post")
    ap.add_argument("--no-llm", action="store_true", help="Skip LLM filter")
    ap.add_argument("--no-pubmed", action="store_true")
    ap.add_argument("--no-biorxiv", action="store_true")
    ap.add_argument("--no-rss", action="store_true")
    ap.add_argument("--since-days", type=int, help="Override since_days for all sources")
    ap.add_argument("--limit", type=int, help="Cap papers per source (debug)")
    ap.add_argument("--min-score", type=int, help="Override LLM min_score")
    args = ap.parse_args()

    cfg = load_config()
    db = SeenDB(cfg.seen_db_path)

    pubmed_days = _window(db, "pubmed", cfg.pubmed_since_days, cfg, args.since_days)
    biorxiv_days = _window(db, "biorxiv", cfg.biorxiv_since_days, cfg, args.since_days)
    rss_days = _window(db, "rss", cfg.rss_since_days, cfg, args.since_days)
    min_score = args.min_score if args.min_score is not None else cfg.llm_min_score

    # ----- Stage 1: collect -----
    _human_log(f"Collecting (pubmed={pubmed_days}d, biorxiv={biorxiv_days}d, rss={rss_days}d)…")
    collected = collect_all(
        since_days_pubmed=pubmed_days,
        since_days_biorxiv=biorxiv_days,
        since_days_rss=rss_days,
        pubmed=cfg.pubmed_enabled and not args.no_pubmed,
        biorxiv=cfg.biorxiv_enabled and not args.no_biorxiv,
        rss=cfg.rss_enabled and not args.no_rss,
    )
    flat: list[Paper] = []
    for source, papers in collected.papers.items():
        if args.limit:
            papers = papers[: args.limit]
        _human_log(f"  {source}: {len(papers)} papers")
        flat.extend(papers)

    # A source that failed is not a source that was empty. Keep going on the
    # ones that worked — the alternative used to be losing the whole run — but
    # say so loudly, and leave the failed source's watermark alone so the next
    # run refetches its window.
    for source, reason in collected.failures.items():
        _human_log(f"  ⚠️  {source} FAILED — {reason}")
    if collected.failures and not args.dry_run and cfg.slack_enabled and cfg.slack_bot_token:
        # Sent here rather than folded into the digest summary: on a day when
        # nothing passes the filter there is no digest to carry the warning.
        alert_channel = cfg.target_channel(score=0)
        if alert_channel:
            post_source_alert(cfg.slack_bot_token, alert_channel,
                              collected.failures, dt.date.today().isoformat())
    if collected.failures and not collected.succeeded:
        _human_log("❌ Every source failed. Nothing to do; window stays open for the next run.")
        db.close()
        return 1

    # Dedup within run (same DOI/PMID may appear in multiple sources)
    by_id: dict[str, Paper] = {}
    for p in flat:
        by_id.setdefault(p.id, p)
    flat = list(by_id.values())

    # ----- Dedup against DB -----
    unseen_ids = db.filter_unseen(p.id for p in flat)
    new_papers = [p for p in flat if p.id in unseen_ids]
    _human_log(f"Total {len(flat)} papers → {len(new_papers)} new (after dedup)")

    if not new_papers:
        _human_log("Nothing new. Exiting.")
        _advance_watermarks(db, collected.succeeded, dry_run=args.dry_run)
        db.close()
        return 0

    # ----- Stage 2: LLM filter -----
    if cfg.llm_enabled and not args.no_llm:
        if cfg.llm_provider == "gemini" and not cfg.gemini_api_key:
            _human_log("⚠️  GEMINI_API_KEY missing — skipping LLM filter.")
            results = [(p, Verdict("YES", None, 5, "(no LLM filter)")) for p in new_papers]
        elif cfg.llm_provider == "anthropic" and not cfg.anthropic_api_key:
            _human_log("⚠️  ANTHROPIC_API_KEY missing — skipping LLM filter.")
            results = [(p, Verdict("YES", None, 5, "(no LLM filter)")) for p in new_papers]
        else:
            api_key = (cfg.gemini_api_key if cfg.llm_provider == "gemini"
                       else cfg.anthropic_api_key)
            prompt = load_prompt(cfg.llm_prompt_path)
            chain_str = " → ".join([cfg.llm_model] + cfg.llm_fallback_models)
            _human_log(f"LLM filter ({cfg.llm_provider}/{chain_str}) on {len(new_papers)} papers…")
            results = filter_batch(
                new_papers, prompt=prompt,
                provider=cfg.llm_provider, model=cfg.llm_model,
                fallback_models=cfg.llm_fallback_models,
                api_key=api_key, parallel=cfg.llm_parallel,
                timeout=cfg.llm_timeout,
            )
    else:
        results = [(p, Verdict("YES", None, 5, "(LLM filter disabled)")) for p in new_papers]

    # ----- Sort + threshold -----
    results.sort(key=lambda pv: -pv[1].score)
    passing = [(p, v) for p, v in results if v.is_yes and v.score >= min_score]

    # Diagnostic: distribution of verdicts and which models actually responded
    verdict_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    error_count = 0
    for _, v in results:
        key = f"{v.verdict}/score={v.score}"
        verdict_counts[key] = verdict_counts.get(key, 0) + 1
        if v.model_used:
            model_counts[v.model_used] = model_counts.get(v.model_used, 0) + 1
        if v.is_error:
            error_count += 1
    _human_log(f"Filter: {len(passing)} pass / {len(results)} total (min_score={min_score})")
    _human_log(f"Verdict distribution: {dict(sorted(verdict_counts.items()))}")
    if model_counts:
        _human_log(f"Model usage: {dict(sorted(model_counts.items()))}")
    if error_count:
        _human_log(f"⚠️  {error_count} papers had SDK/parse errors (see stderr above)")

    # ----- Optional post cap (default: none) -----
    # A cap silently discards papers that already passed the filter, and they
    # would be marked seen right after, so they'd never be revisited. Uncapped by
    # default; if one is set, the excess is held back rather than dropped, so the
    # next run picks it up.
    held_back: list[tuple[Paper, Verdict]] = []
    if cfg.slack_max_posts is not None and len(passing) > cfg.slack_max_posts:
        held_back = passing[cfg.slack_max_posts:]
        passing = passing[: cfg.slack_max_posts]
        _human_log(
            f"⚠️  post cap {cfg.slack_max_posts} hit — holding back "
            f"{len(held_back)} passing papers for the next run (not marked seen)")

    # ----- Print to stdout (always) -----
    print()
    for p, v in passing:
        flag = "★" * min(5, max(1, (v.score + 1) // 2))
        print(f"{flag} [{v.score}/10] [{p.journal}] {p.title[:80]}")
        if v.one_liner:
            print(f"     KR: {v.one_liner}")
        if v.one_liner_en:
            print(f"     EN: {v.one_liner_en}")
        print(f"     {p.url}")

    # In dry-run, also show first 5 rejected papers so silent-failure modes
    # (parse errors, SDK errors) are visible.
    if args.dry_run and len(passing) < len(results):
        rejected = [(p, v) for p, v in results if not (v.is_yes and v.score >= min_score)]
        print()
        print(f"--- Rejected (showing first 5 of {len(rejected)}) ---")
        for p, v in rejected[:5]:
            print(f"[{v.verdict}/{v.score}] [{p.journal}] {p.title[:80]}")
            if v.one_liner:
                print(f"     reason: {v.one_liner}")

    # ----- Slack push -----
    # Posting happens BEFORE persisting, so a paper is only ever marked seen once
    # it has actually reached Slack. The reverse order silently loses papers when
    # a post fails. Worst case here is a duplicate if the run dies in between,
    # which is far preferable to a miss.
    posted = 0
    post_failures: list[tuple[Paper, Verdict]] = []
    if not args.no_slack and not args.dry_run and cfg.slack_enabled and cfg.slack_bot_token and passing:
        target = cfg.target_channel(score=max((v.score for _, v in passing), default=0))
        if not target:
            _human_log("⚠️  No Slack channel configured — skipping post.")
            post_failures = list(passing)
        else:
            summary = {
                "date": dt.date.today().isoformat(),
                "collected": len(flat),
                "new": len(new_papers),
                "passed": len(passing),
            }
            _human_log(f"Posting {len(passing)} to Slack channel {target}…")
            posted, post_failures = post_papers(cfg.slack_bot_token, target, passing,
                                                summary=summary)
            _human_log(f"Posted {posted} messages ({len(post_failures)} failed).")
    elif args.dry_run:
        _human_log("Dry run — Slack push skipped, DB not updated.")

    # ----- Persist verdicts (unless dry-run) -----
    # Marking a paper seen is permanent: it is excluded from every future run. So
    # only record papers we are actually done with. Anything left out is retried
    # on the next run.
    if not args.dry_run:
        persist, retry = split_persist_vs_retry(
            results, held_back=held_back, post_failures=post_failures)
        for p, v in persist:
            db.mark_seen(p, v)
        _human_log(f"Persisted {len(persist)} verdicts to {cfg.seen_db_path}")
        if retry:
            _human_log(
                f"↻ {len(retry)} papers left unseen for retry next run "
                f"(errors={error_count}, held back={len(held_back)}, "
                f"post failures={len(post_failures)})")

    # Delivered — the sources that worked may now advance. The ones that failed
    # keep their old watermark, which is what widens the next run's window.
    _advance_watermarks(db, collected.succeeded, dry_run=args.dry_run)
    if collected.failures:
        _human_log(
            f"↺ {sorted(collected.failures)} not advanced — next run will "
            f"refetch their window.")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
