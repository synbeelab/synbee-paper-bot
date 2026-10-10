"""Second look for papers that were rejected on their title alone.

Measured 2026-10-10 from the weekly sweep logs: after the Europe PMC backfill,
**172 of 617 papers (10/3) and 292 of 679 (9/26) still reached the filter with
no abstract** — Cell Press and Trends deposit none to Crossref, and Europe PMC
has usually not indexed a paper yet in the week it appears.

abstracts.py used to say "a later run picks them up". None did. A title-only
reject is a real verdict as far as `split_persist_vs_retry` is concerned, so it
is marked seen, and when PubMed later sends the same DOI WITH its abstract, the
DOI dedup drops it before the filter ever sees it. The first, weakest judgement
was final.

This module keeps those rejects in a queue (`title_only_rejects` in seen.db)
and, on every daily run, checks whether an abstract now exists — first in what
this very run collected (the PubMed copy that dedup is about to throw away),
then in Europe PMC. A paper that gains an abstract is judged again with it, and
posted if it passes. It is then done, whatever the verdict. A paper that never
gains one ages out after `max_age_days`; it keeps its title-only verdict, which
is exactly what happened to it before this module existed.

What is NOT queued, deliberately:
  * passing papers — they were posted; nothing was lost.
  * papers without a DOI — there is nothing to look an abstract up by.
  * error verdicts — `split_persist_vs_retry` already retries those.

Recall can only go up: this adds a second judgement, it never removes a first.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable

from .abstracts import backfill_abstracts, needs_abstract
from .models import Paper, Verdict
from .storage import SeenDB

#: Europe PMC typically indexes a new paper within one to three weeks.
#: Past a month, an abstract that has not appeared is not coming.
DEFAULT_MAX_AGE_DAYS = 30


def queue_title_only_rejects(
    db: SeenDB,
    persisted: Iterable[tuple[Paper, Verdict]],
    *,
    min_score: int,
    today: dt.date | None = None,
) -> int:
    """Queue every persisted reject that was judged without an abstract."""
    queued = 0
    for paper, verdict in persisted:
        if verdict.is_error or not paper.doi or not needs_abstract(paper):
            continue
        if verdict.is_yes and verdict.score >= min_score:
            continue
        db.queue_title_only(paper, today)
        queued += 1
    return queued


def _fresh_abstracts(collected: Iterable[Paper]) -> dict[str, str]:
    """DOI → abstract for papers THIS run collected with a usable abstract."""
    out: dict[str, str] = {}
    for p in collected:
        if p.doi and not needs_abstract(p):
            out.setdefault(p.doi.strip().lower(), p.abstract)
    return out


def recover_title_only(
    db: SeenDB,
    *,
    collected: Iterable[Paper] = (),
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    today: dt.date | None = None,
    lookup: Callable[..., list[Paper]] = backfill_abstracts,
    timeout: int = 30,
    expire: bool = True,
    log: Callable[[str], None] = lambda msg: None,
) -> list[Paper]:
    """Queued title-only rejects that now have an abstract, ready to re-judge.

    Expired entries are dropped from the queue here. Recovered ones stay
    queued until `settle_title_only` confirms they were persisted, so a run
    that dies after this call simply recovers them again next time. Pass
    ``expire=False`` on a dry run, which must leave seen.db untouched.
    """
    today = today or dt.date.today()
    queue = db.list_title_only()
    if not queue:
        return []

    expired = [p.id for p, first in queue if (today - first).days > max_age_days]
    if expired and expire:
        db.drop_title_only(expired)
    waiting = [p for p, first in queue if (today - first).days <= max_age_days]
    if not waiting:
        log(f"Title-only re-check: {len(expired)} aged out, none waiting")
        return []

    fresh = _fresh_abstracts(collected)
    from_run = [
        Paper(**{**p.to_dict(), "abstract": fresh[p.doi.strip().lower()]})
        for p in waiting if p.doi and p.doi.strip().lower() in fresh
    ]
    taken = {p.id for p in from_run}
    rest = [p for p in waiting if p.id not in taken]
    looked_up = lookup(rest, timeout=timeout) if rest else []
    from_lookup = [p for p in looked_up if not needs_abstract(p)]

    recovered = from_run + from_lookup
    log(f"Title-only re-check: {len(waiting)} waiting → {len(recovered)} now have "
        f"an abstract ({len(from_run)} from this run's sources, "
        f"{len(from_lookup)} from Europe PMC), {len(expired)} aged out")
    return recovered


def settle_title_only(db: SeenDB, recovered: Iterable[Paper],
                      persisted: Iterable[tuple[Paper, Verdict]]) -> None:
    """Retire recovered papers whose second verdict was actually recorded."""
    done = {p.id for p, _ in persisted}
    db.drop_title_only(p.id for p in recovered if p.id in done)
