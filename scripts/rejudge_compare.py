"""Re-judge stored verdicts with a candidate filter prompt and diff the result.

A prompt change is a recall change, and the only way to see its size is to run
both prompts over papers whose current verdict we already know. This reads
`seen.db`, replays a sample through the candidate prompt, and reports the two
directions separately:

  GAINED — papers the stored prompt rejected that the candidate passes. These
           are what a widening is for; each one has to be read to confirm it is
           wanted, so they are listed in full.
  LOST   — papers the stored prompt passed that the candidate rejects. A
           widening is not supposed to produce any of these, so any hit here is
           a regression and the run says so.

Sampling is stratified by score so the rejects that matter (the near-misses)
are not drowned out by the 2-point chemistry papers, and the stored verdict is
never written back — this script only reads `seen.db`.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from synbee_bot.filter import filter_batch, load_prompt  # noqa: E402
from synbee_bot.models import Paper  # noqa: E402

# Sample sizes per stored score. Score 3 is where the near-misses live, so it
# gets the most; score 2 is sampled only to prove the widening does not reach
# into the chemistry/clinical bulk.
DEFAULT_REJECT_QUOTA = {4: 60, 3: 240, 2: 120}


def _row_to_paper(row: sqlite3.Row) -> Paper:
    return Paper(
        id=row["id"],
        source=row["source"],
        title=row["title"] or "",
        abstract=row["abstract"] or "",
        journal=row["journal"] or "",
        year=row["year"],
        doi=row["doi"],
        url=row["url"] or "",
        authors=[],
        published=None,
    )


def reject_population(conn: sqlite3.Connection, since: str,
                      quota: dict[int, int]) -> dict[int, int]:
    """How many stored rejects exist per score, not how many were sampled.

    The GAINED count is a rate measured on the sample; the number of papers a
    prompt change would actually recover is that rate times this population.
    Reported so the two are never confused.
    """
    return {
        score: conn.execute(
            "SELECT COUNT(*) FROM seen WHERE verdict='NO' AND score=? AND pushed_at>=?",
            (score, since),
        ).fetchone()[0]
        for score in sorted(quota, reverse=True)
    }


def _sample(conn: sqlite3.Connection, since: str, quota: dict[int, int],
            rng: random.Random) -> tuple[list[Paper], list[Paper]]:
    conn.row_factory = sqlite3.Row
    rejects: list[Paper] = []
    for score, n in sorted(quota.items(), reverse=True):
        rows = conn.execute(
            "SELECT * FROM seen WHERE verdict='NO' AND score=? AND pushed_at>=?",
            (score, since),
        ).fetchall()
        rng.shuffle(rows)
        rejects.extend(_row_to_paper(r) for r in rows[:n])

    passes = [
        _row_to_paper(r)
        for r in conn.execute(
            "SELECT * FROM seen WHERE verdict='YES' AND score>=6 AND pushed_at>=?",
            (since,),
        ).fetchall()
    ]
    return rejects, passes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/seen.db")
    ap.add_argument("--prompt", default="config/filter_prompt.md",
                    help="Candidate prompt to evaluate")
    ap.add_argument("--since", default="2026-09-06",
                    help="Only replay verdicts recorded on or after this date")
    ap.add_argument("--min-score", type=int, default=6)
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--fallback", default="gemini-2.5-flash-lite")
    ap.add_argument("--seed", type=int, default=20261014)
    ap.add_argument("--out", default="rejudge_report.json")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        sys.stderr.write("GEMINI_API_KEY is not set\n")
        return 1

    conn = sqlite3.connect(args.db)
    rejects, passes = _sample(conn, args.since, DEFAULT_REJECT_QUOTA,
                              random.Random(args.seed))
    stored = {
        r["id"]: (r["verdict"], r["score"])
        for r in conn.execute("SELECT id, verdict, score FROM seen")
    }

    population = reject_population(conn, args.since, DEFAULT_REJECT_QUOTA)

    papers = rejects + passes
    print(f"Re-judging {len(papers)} papers "
          f"({len(rejects)} stored NO, {len(passes)} stored YES) "
          f"with {args.prompt}", flush=True)
    print("Stored rejects in window (sampled / total): "
          + ", ".join(f"{s}점 {min(DEFAULT_REJECT_QUOTA[s], n)}/{n}"
                      for s, n in population.items()), flush=True)

    prompt = load_prompt(ROOT / args.prompt)
    verdicts = filter_batch(
        papers, prompt=prompt, provider="gemini", model=args.model,
        fallback_models=[args.fallback], api_key=api_key,
        parallel=args.parallel,
    )

    gained, lost, errors = [], [], 0
    dist: collections.Counter = collections.Counter()
    # filter_batch yields (paper, verdict) pairs in completion order, not input
    # order — pairing by position would mislabel every result.
    for paper, v in verdicts:
        if v.is_error:
            errors += 1
            continue
        old_verdict, old_score = stored.get(paper.id, ("NO", 0))
        now_pass = v.verdict == "YES" and v.score >= args.min_score
        was_pass = old_verdict == "YES" and old_score >= args.min_score
        dist[(old_score, v.score)] += 1
        entry = {
            "id": paper.id, "title": paper.title, "journal": paper.journal,
            "source": paper.source, "old_score": old_score,
            "new_score": v.score, "mission": v.mission,
            "one_liner": v.one_liner,
        }
        if now_pass and not was_pass:
            gained.append(entry)
        elif was_pass and not now_pass:
            lost.append(entry)

    report = {
        "prompt": args.prompt, "since": args.since,
        "sampled_rejects": len(rejects), "sampled_passes": len(passes),
        "reject_population": population,
        "errors": errors, "gained": gained, "lost": lost,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    reject_rate = len(gained) / max(1, len(rejects))
    print(f"\nGAINED {len(gained)} of {len(rejects)} sampled rejects "
          f"({reject_rate:.1%})")
    print(f"LOST   {len(lost)} of {len(passes)} stored passes")
    print(f"errors {errors}\n")

    by_journal = collections.Counter(g["journal"] for g in gained)
    print("GAINED by journal:")
    for journal, n in by_journal.most_common(20):
        print(f"  {n:3}  {journal}")

    print("\nGAINED papers:")
    for g in sorted(gained, key=lambda g: -g["new_score"]):
        print(f"  [{g['old_score']}→{g['new_score']}] {g['journal']} | "
              f"{g['title'][:95]}")
        print(f"        {g['one_liner'][:150]}")

    if lost:
        print("\n⚠ LOST — a widening must not drop these:")
        for entry in lost:
            print(f"  [{entry['old_score']}→{entry['new_score']}] "
                  f"{entry['journal']} | {entry['title'][:95]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
