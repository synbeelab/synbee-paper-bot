"""Decide whether a catch-up run of a scheduled workflow should proceed.

GitHub delivers `schedule` events on a best-effort basis: they arrive late under
load and are sometimes dropped outright. On 2026-08-26 every scheduled workflow
in this repo fired 5 h late (the digest landed at 13:11 KST instead of 08:00),
and on 2026-08-27 the 23:00 UTC cycle produced no run at all — no failure, no
queued run, no event. The 08:00 KST digest simply never came.

A cron alone therefore cannot guarantee delivery, so every scheduled workflow
also fires catch-up crons a few hours later. Those must be no-ops on the days
the primary did arrive, or the channel fills with duplicates (and the weekly
digest would post a second zero report). The rule is **one delivery per KST
day, per workflow**.

KST date is the right grouping key even though every cron is UTC: the daily
primary fires at 22:47 UTC, which is 07:47 KST *the next day* — the same KST day
as its 01:23 UTC catch-up. Grouping by UTC date would split that pair.

The guard fails OPEN. A guard that wrongly allows a run costs one duplicate
Slack post; a guard that wrongly blocks one costs the day's papers, which is the
failure this bot exists to prevent.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))

#: How far back to look. The guard only cares about today, and a workflow runs
#: at most a handful of times a day, so a short page is plenty.
RUNS_PAGE_SIZE = 30

_API_ROOT = "https://api.github.com"

#: Only a completed, successful run counts as a delivery. A failure or an
#: in-progress run must leave the door open for the next catch-up.
_DELIVERED = "success"


def parse_github_ts(value: str) -> datetime:
    """Parse a GitHub API timestamp (``2026-08-27T04:06:53Z``) as aware UTC."""
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def kst_day(moment: datetime) -> date:
    """The KST calendar day a moment falls on — the guard's grouping key."""
    return moment.astimezone(KST).date()


def runs_url(repo: str, workflow_file: str, *, limit: int = RUNS_PAGE_SIZE) -> str:
    """The API URL listing a workflow's successful runs, newest first.

    ``status=success`` is applied server-side so a long tail of failed or
    cancelled runs cannot push today's delivery off the page.
    """
    return (
        f"{_API_ROOT}/repos/{repo}/actions/workflows/{workflow_file}/runs"
        f"?status=success&per_page={limit}"
    )


def fetch_successful_runs(
    repo: str, workflow_file: str, *, token: str, limit: int = RUNS_PAGE_SIZE
) -> list[dict[str, Any]]:
    """Ask GitHub which runs of this workflow have already succeeded.

    Raises on any transport or payload problem — `decide` turns that into
    "run anyway".
    """
    request = urllib.request.Request(
        runs_url(repo, workflow_file, limit=limit),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "synbee-paper-bot-catchup-guard",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["workflow_runs"]


def already_delivered_today(
    runs: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    current_run_id: int | None = None,
) -> bool:
    """True when a *different* run already succeeded on this KST day.

    ``runs`` are GitHub workflow-run records; only ``id``, ``conclusion`` and
    ``created_at`` are read, so the caller can pass the API payload untouched.
    A record with an unparseable timestamp is skipped rather than raised on: one
    bad row must not decide the whole run.
    """
    today = kst_day(now)

    for record in runs:
        if record.get("conclusion") != _DELIVERED:
            continue
        if current_run_id is not None and record.get("id") == current_run_id:
            continue
        raw = record.get("created_at") or ""
        try:
            when = parse_github_ts(raw)
        except (TypeError, ValueError):
            continue
        if kst_day(when) == today:
            return True

    return False


def decide(
    fetch_runs: Callable[[], Iterable[Mapping[str, Any]]],
    *,
    event_name: str,
    now: datetime | None = None,
    current_run_id: int | None = None,
) -> bool:
    """Whether this run should do its work.

    A manual ``workflow_dispatch`` always runs: a human asking for a digest must
    never be second-guessed. A scheduled run stands down only when the day's
    delivery is already on the record. Any error while checking that resolves to
    "run" — see the fail-open note in the module docstring.
    """
    if event_name != "schedule":
        return True

    try:
        runs = fetch_runs()
        return not already_delivered_today(
            runs, now=now or datetime.now(timezone.utc), current_run_id=current_run_id
        )
    except Exception as exc:  # noqa: BLE001 — fail open, loudly
        sys.stderr.write(
            f"  ! catch-up guard could not check today's runs ({exc}); "
            "running anyway rather than risk a missed delivery\n"
        )
        return True
