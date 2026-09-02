"""The spam sweep must not inherit the digest's once-a-day guard.

2026-09-02: a prospective graduate student's inquiry and an undergraduate's
enrolment request both landed in the spam folder at 09:00 and 11:21 KST — after
that morning's sweep (08:34 KST) had already run. Both catch-up crons then
logged "already delivered today" and skipped, so neither mail was ever judged;
the user found them by hand at 01:30 KST the next day.

The guard is right for ``daily.yml``: a second digest that day would be a
duplicate e-mail. It is wrong here. A sweep is idempotent — the
``SpamRescueChecked`` label excludes mail already judged, so a re-run with no
new spam costs one Gmail list call and zero model calls. Skipping it buys
nothing and can leave real correspondence in spam for the best part of a day.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "spam-rescue.yml"

# Worst-case time a legitimate mail may sit in spam before it is judged,
# ignoring GitHub's own best-effort delay on top.
MAX_GAP_HOURS = 3


def _workflow() -> dict:
    # "on" is parsed as the boolean True by YAML 1.1; ask for both spellings.
    raw = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    raw["on"] = raw.get("on", raw.get(True))
    return raw


def _cron_hours() -> list[float]:
    """Scheduled sweep times, in hours past midnight UTC."""
    schedules = _workflow()["on"]["schedule"]
    hours = []
    for entry in schedules:
        minute, hour = entry["cron"].split()[:2]
        if hour == "*":
            return [h + int(minute) / 60 for h in range(24)]
        hours.extend(int(h) + int(minute) / 60 for h in hour.split(","))
    return sorted(hours)


def test_the_sweep_is_not_gated_by_the_once_a_day_guard():
    """The run job must not stand down because a sweep already ran today."""
    jobs = _workflow()["jobs"]
    condition = str(jobs["run"].get("if", ""))

    assert "should_run" not in condition, (
        "the spam sweep is gated on the once-a-day catch-up guard; mail that "
        "reaches spam after the day's first sweep would wait until tomorrow"
    )
    assert "guard" not in jobs, (
        "spam-rescue.yml still defines the guard job — a sweep is idempotent "
        "and must run at every scheduled time"
    )


def test_no_scheduled_gap_leaves_mail_in_spam_for_hours():
    hours = _cron_hours()
    assert hours, "spam-rescue.yml has no schedule at all"

    # Wrap around midnight: the gap from the last sweep to the first one
    # tomorrow counts too, which is exactly what the 2026-09-02 miss exposed.
    gaps = [b - a for a, b in zip(hours, hours[1:])]
    gaps.append(hours[0] + 24 - hours[-1])

    assert max(gaps) <= MAX_GAP_HOURS, (
        f"longest gap between sweeps is {max(gaps):.1f} h "
        f"(> {MAX_GAP_HOURS} h): legitimate mail can sit in spam that long"
    )


def test_the_keepalive_survives():
    """Removing the guard must not take the 60-day auto-disable guard with it."""
    assert "keepalive" in _workflow()["jobs"]
    assert re.search(r"liskin/gh-workflow-keepalive",
                     WORKFLOW.read_text(encoding="utf-8"))
