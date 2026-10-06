"""Pipeline watchdog: notice what the bots cannot notice about themselves.

Every past outage of this repo was *silent* in the same way — the workflow went
green while papers went missing:

* a source returned a short list (bioRxiv read only its first page for weeks),
* a feed answered with an HTML page that parsed as "0 entries" (Metabolic
  Engineering's dead ScienceDirect URL, Nature's cookie bounce),
* a sweep failed, the run still succeeded, and the catch-up guard then counted
  the day as delivered (weekly PubMed, 2026-09-19 — a week's delay),
* GitHub dropped the schedule event, or auto-disabled the workflow.

The watchdog runs once a day after the catch-up window and looks at outcomes,
not exit codes. It repairs what is safe to repair unattended — re-enable a
workflow GitHub disabled for inactivity, re-dispatch a workflow whose day is
missing or whose source failed *today* — and reports the rest. It never edits
code, config, or seen.db. A re-dispatch is safe by construction: seen.db dedup
makes the rerun post only what the failed run did not.

Pure decision logic lives here; I/O lives in scripts/run_watchdog.py.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .catchup import KST, kst_day, parse_github_ts
from .dedup import doi_kind, title_key, titles_may_match


@dataclass(frozen=True)
class Pipeline:
    """A scheduled workflow and what a complete delivery looks like."""
    workflow_file: str
    #: Weekday numbers (Mon=0) on which a delivery is expected, in KST.
    days: frozenset[int]
    #: Job that does the work (the guard can skip it on a "successful" run).
    work_job: str
    #: seen.db watermark keys that a complete delivery advances.
    sources: tuple[str, ...]

    @property
    def period_days(self) -> int:
        return 1 if len(self.days) == 7 else 7


PIPELINES: tuple[Pipeline, ...] = (
    Pipeline("daily.yml", frozenset(range(7)), "run", ("pubmed", "biorxiv", "rss")),
    Pipeline("weekly.yml", frozenset({5}), "run", ("weekly_pubmed", "crossref_toc")),
)

#: The watchdog is scheduled for the afternoon, after the day's last catch-up,
#: but GitHub delivers schedule events hours late — on 2026-10-05 the 16:17 KST
#: run arrived at 00:57 KST the next day. Read as "today", that run checked a
#: day whose daily was not due for another seven hours, reported it missing and
#: re-dispatched it. A run before this KST hour is a late check of yesterday.
DAY_ROLLOVER_HOUR_KST = 12


def checked_day(now: datetime) -> date:
    """The KST day a watchdog run arriving at `now` is responsible for."""
    local = now.astimezone(KST)
    if local.hour < DAY_ROLLOVER_HOUR_KST:
        return local.date() - timedelta(days=1)
    return local.date()


#: Workflows GitHub may auto-disable after 60 idle days. A workflow somebody
#: disabled by hand (`disabled_manually`) is a decision, not a fault.
AUTO_DISABLED = "disabled_inactivity"


@dataclass
class Report:
    actions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(self.actions or self.problems)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeliveryVerdict:
    #: "ok" | "missing" | "in_progress" | "stale_sources" | "not_due"
    status: str
    detail: str = ""
    stale: tuple[str, ...] = ()
    #: True when re-running the workflow is a sensible repair.
    should_dispatch: bool = False


def delivery_verdict(
    pipeline: Pipeline,
    runs: Iterable[Mapping[str, Any]],
    work_job_ran: Mapping[int, bool],
    watermarks: Mapping[str, date | None],
    *,
    now: datetime,
) -> DeliveryVerdict:
    """Did the checked day's (KST) delivery of `pipeline` happen, and was it
    complete? The checked day is `checked_day(now)`, not the calendar day.

    `runs` are GitHub run records, newest first. `work_job_ran[run_id]` says
    whether that run's work job actually executed and succeeded (a run whose
    work job the guard skipped is not a delivery).
    """
    today = checked_day(now)
    if today.weekday() not in pipeline.days:
        return DeliveryVerdict("not_due")

    todays = [r for r in runs if _run_day(r) == today]
    if any(r.get("status") in ("queued", "in_progress", "waiting", "pending")
           for r in todays):
        return DeliveryVerdict("in_progress", "a run is still going")

    delivered = [r for r in todays
                 if r.get("conclusion") == "success" and work_job_ran.get(r["id"])]
    if not delivered:
        failed = [r for r in todays if r.get("conclusion") not in (None, "success")]
        detail = (f"{len(failed)} run(s) today, none delivered" if failed
                  else "no run today — schedule event dropped or delayed")
        return DeliveryVerdict("missing", detail, should_dispatch=True)

    # The watermark is written with the runner's UTC date at the end of the run.
    run_utc_day = min(parse_github_ts(r["created_at"]).date() for r in delivered)
    stale = tuple(s for s in pipeline.sources
                  if watermarks.get(s) is None or watermarks[s] < run_utc_day)
    if not stale:
        return DeliveryVerdict("ok")

    # Only a source that failed *this period* is worth a rerun. One that has
    # been down for longer is an outage the run already alerts on every time;
    # rerunning it again only repeats that alert.
    previous_due = run_utc_day - timedelta(days=pipeline.period_days)
    fresh = tuple(s for s in stale
                  if watermarks.get(s) is not None and watermarks[s] >= previous_due)
    parts = [f"{s} (last ok {watermarks.get(s) or 'never'})" for s in stale]
    return DeliveryVerdict("stale_sources", ", ".join(parts), stale=stale,
                           should_dispatch=bool(fresh))


def _run_day(record: Mapping[str, Any]) -> date | None:
    try:
        return kst_day(parse_github_ts(record.get("created_at") or ""))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Content anomalies (read from seen.db rows)
# ---------------------------------------------------------------------------
def find_duplicate_posts(rows: Iterable[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Groups of *posted* rows that are the same paper under different ids.

    Same rule as synbee_bot.dedup: a shared DOI, or a shared title where at
    least one record has no DOI (a preprint and its journal version both have
    DOIs and are not duplicates).
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("doi"):
            groups.setdefault("doi:" + str(row["doi"]).lower(), []).append(row)
        key = title_key(row.get("title"))
        if key:
            groups.setdefault("t:" + key, []).append(row)
    out: list[list[Mapping[str, Any]]] = []
    reported: set[frozenset[str]] = set()
    for name, members in groups.items():
        ids = frozenset(str(m["id"]) for m in members)
        if len(ids) < 2 or ids in reported:
            continue
        if name.startswith("t:"):
            kinds = [doi_kind(m.get("doi")) for m in members]
            if not any(titles_may_match(a, b)
                       for i, a in enumerate(kinds) for b in kinds[i + 1:]):
                continue
        reported.add(ids)
        out.append(members)
    return out


def volume_drops(
    daily_counts: Mapping[str, Mapping[date, int]],
    *,
    today: date,
    min_median: float = 10.0,
    ratio: float = 0.25,
) -> list[str]:
    """Sources whose intake today fell below `ratio` × their 14-day median.

    This is the check that would have caught bioRxiv's first-page-only
    truncation (3–5 papers a day against a real ~20+) — but only once a
    healthy baseline exists, so it never fires on a source with no history.
    """
    findings = []
    for source, by_day in sorted(daily_counts.items()):
        history = [by_day.get(today - timedelta(days=d), 0) for d in range(1, 15)]
        if sum(1 for n in history if n) < 7:
            continue  # not enough history to call anything abnormal
        median = statistics.median(history)
        got = by_day.get(today, 0)
        if median >= min_median and got < ratio * median:
            findings.append(f"{source}: {got} papers today vs 14-day median {median:g}")
    return findings


def silent_feeds(
    feed_counts: Mapping[str, Mapping[date, int]],
    *,
    today: date,
    quiet_days: int = 4,
    min_before: int = 5,
    rhythm_slack: float = 1.5,
) -> list[str]:
    """RSS feeds that have gone quiet for longer than their own rhythm allows.

    A fixed window misreads issue-based feeds: Cell's `current.rss` lists only
    the current issue, so new items arrive every 14 days and a 4-day rule
    called it dead on 10 days of every 14 (2026-10-06). The allowed silence is
    therefore `rhythm_slack` × the longest gap the feed has shown between
    active days, never less than `quiet_days`. A feed with fewer than three
    active days has no measurable rhythm and is not judged — the weekly
    Crossref sweep still covers those journals.
    """
    findings = []
    for feed, by_day in sorted(feed_counts.items()):
        active = sorted(d for d, n in by_day.items() if n and d <= today)
        if len(active) < 3 or sum(by_day[d] for d in active) < min_before:
            continue
        longest_gap = max((b - a).days for a, b in zip(active, active[1:]))
        allowed = max(quiet_days, math.ceil(rhythm_slack * longest_gap))
        silence = (today - active[-1]).days
        if silence >= allowed:
            findings.append(f"RSS '{feed}': 0 papers in {silence} days "
                            f"(normally at most {longest_gap} days apart)")
    return findings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(report: Report, *, today: date) -> str:
    lines = [f"🩺 *논문 파이프라인 watchdog* — {today.isoformat()}"]
    if report.actions:
        lines.append("*자동 조치*")
        lines += [f"• {a}" for a in report.actions]
    if report.problems:
        lines.append("*확인 필요*")
        lines += [f"• {p}" for p in report.problems]
    if report.notes:
        lines.append("_참고_")
        lines += [f"• {n}" for n in report.notes]
    if not report.needs_attention and not report.notes:
        lines.append("이상 없음.")
    return "\n".join(lines)
