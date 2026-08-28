"""Pins the catch-up guard: one delivery per KST day, and never a silent block.

2026-08-26 every scheduled workflow in this repo fired 5 h late; 2026-08-27 the
23:00 UTC cycle produced no run at all, so the 08:00 KST digest never came.
Nothing was wrong with the bot — GitHub simply did not deliver the `schedule`
event. Catch-up crons now cover that, and this guard is what stops them from
posting a second copy on the days the primary did arrive.

The two failure modes worth pinning are opposite: a guard that never skips
duplicates the digest, and a guard that skips too eagerly loses the day's papers
entirely. The second is the expensive one, so the guard fails OPEN.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot.catchup import (  # noqa: E402
    KST,
    already_delivered_today,
    decide,
    kst_day,
    parse_github_ts,
    runs_url,
)

UTC = timezone.utc


def run(created_at: str, *, conclusion: str = "success", run_id: int = 1) -> dict:
    """A GitHub workflow-run record, trimmed to the fields the guard reads."""
    return {"id": run_id, "conclusion": conclusion, "created_at": created_at}


# --- timestamp handling ------------------------------------------------------


def test_parse_github_ts_accepts_the_zulu_suffix():
    # Arrange / Act
    parsed = parse_github_ts("2026-08-27T04:06:53Z")

    # Assert
    assert parsed == datetime(2026, 8, 27, 4, 6, 53, tzinfo=UTC)


def test_parse_github_ts_accepts_an_explicit_offset():
    assert parse_github_ts("2026-08-27T04:06:53+00:00") == datetime(
        2026, 8, 27, 4, 6, 53, tzinfo=UTC
    )


def test_evening_utc_cron_belongs_to_the_next_kst_day():
    """The whole scheme rests on this: 22:47 UTC is 07:47 KST tomorrow.

    That is what makes the primary and its next-morning catch-up share a
    grouping key.
    """
    # Arrange
    primary = parse_github_ts("2026-08-28T22:47:00Z")
    catchup = parse_github_ts("2026-08-29T01:23:00Z")

    # Act / Assert
    assert kst_day(primary) == kst_day(catchup)
    assert kst_day(primary).isoformat() == "2026-08-29"


def test_kst_day_of_a_saturday_weekly_run():
    assert kst_day(parse_github_ts("2026-08-29T00:30:00Z")).isoformat() == "2026-08-29"


# --- already_delivered_today -------------------------------------------------


def test_no_history_means_nothing_was_delivered():
    assert not already_delivered_today([], now=parse_github_ts("2026-08-29T01:23:00Z"))


def test_a_success_earlier_the_same_kst_day_counts_as_delivered():
    # Arrange — the primary landed at 07:47 KST on 2026-08-29
    runs = [run("2026-08-28T22:47:00Z", run_id=100)]

    # Act / Assert — the 10:23 KST catch-up must see it
    assert already_delivered_today(runs, now=parse_github_ts("2026-08-29T01:23:00Z"))


def test_a_failed_run_today_does_not_count_as_delivered():
    runs = [run("2026-08-28T22:47:00Z", conclusion="failure", run_id=100)]

    assert not already_delivered_today(
        runs, now=parse_github_ts("2026-08-29T01:23:00Z")
    )


def test_an_in_progress_run_today_does_not_count_as_delivered():
    runs = [run("2026-08-28T22:47:00Z", conclusion=None, run_id=100)]

    assert not already_delivered_today(
        runs, now=parse_github_ts("2026-08-29T01:23:00Z")
    )


def test_yesterdays_success_does_not_block_today():
    # Arrange — delivered on 2026-08-28, nothing yet for 2026-08-29
    runs = [run("2026-08-27T22:47:00Z", run_id=99)]

    assert not already_delivered_today(
        runs, now=parse_github_ts("2026-08-29T01:23:00Z")
    )


def test_the_current_run_never_blocks_itself():
    """The API lists the running run too; counting it would skip every run."""
    runs = [run("2026-08-29T01:23:00Z", run_id=555)]

    assert not already_delivered_today(
        runs, now=parse_github_ts("2026-08-29T01:23:00Z"), current_run_id=555
    )


def test_a_catchup_delivery_blocks_the_later_catchup():
    # Arrange — 01:23 UTC catch-up delivered because the primary was dropped
    runs = [run("2026-08-29T01:23:00Z", run_id=555)]

    # Act / Assert — the 04:23 UTC catch-up must stand down
    assert already_delivered_today(
        runs, now=parse_github_ts("2026-08-29T04:23:00Z"), current_run_id=777
    )


def test_a_very_late_primary_stands_down_after_a_catchup_delivered():
    """2026-08-26's primary arrived 5 h late. By then a catch-up would have run."""
    runs = [run("2026-08-29T01:23:00Z", run_id=555)]

    assert already_delivered_today(
        runs, now=parse_github_ts("2026-08-29T03:47:00Z"), current_run_id=778
    )


def test_a_malformed_timestamp_is_ignored_not_fatal():
    runs = [run("not-a-timestamp", run_id=100), run("2026-08-28T22:47:00Z", run_id=101)]

    assert already_delivered_today(
        runs, now=parse_github_ts("2026-08-29T01:23:00Z")
    )


# --- decide ------------------------------------------------------------------


def test_manual_dispatch_always_runs():
    """A human asking for a run must never be second-guessed by the guard."""
    runs = [run("2026-08-28T22:47:00Z", run_id=100)]

    assert decide(
        lambda: runs,
        now=parse_github_ts("2026-08-29T01:23:00Z"),
        event_name="workflow_dispatch",
    )


def test_scheduled_catchup_skips_when_already_delivered():
    runs = [run("2026-08-28T22:47:00Z", run_id=100)]

    assert not decide(
        lambda: runs,
        now=parse_github_ts("2026-08-29T01:23:00Z"),
        event_name="schedule",
    )


def test_scheduled_catchup_runs_when_the_primary_was_dropped():
    assert decide(
        lambda: [],
        now=parse_github_ts("2026-08-29T01:23:00Z"),
        event_name="schedule",
    )


def test_the_guard_fails_open_when_the_api_call_raises():
    """A broken guard must cost a duplicate post, never a missed digest."""

    def boom():
        raise RuntimeError("502 Bad Gateway")

    assert decide(
        boom,
        now=parse_github_ts("2026-08-29T01:23:00Z"),
        event_name="schedule",
    )


def test_the_guard_fails_open_on_a_garbage_payload():
    assert decide(
        lambda: "not a list of runs",
        now=parse_github_ts("2026-08-29T01:23:00Z"),
        event_name="schedule",
    )


def test_decide_defaults_now_to_the_current_moment():
    """Callers may omit `now`; the guard must not require it."""
    assert decide(lambda: [], event_name="schedule")


def test_kst_is_plus_nine():
    assert KST.utcoffset(None) == timedelta(hours=9)


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch", "push"])
def test_decide_returns_a_bool_for_every_event(event):
    assert isinstance(
        decide(lambda: [], now=parse_github_ts("2026-08-29T01:23:00Z"), event_name=event),
        bool,
    )


# --- API surface -------------------------------------------------------------


def test_runs_url_filters_to_successes_server_side():
    """A long tail of failures must not push today's delivery off the page."""
    url = runs_url("synbeelab/synbee-paper-bot", "daily.yml", limit=30)

    assert url == (
        "https://api.github.com/repos/synbeelab/synbee-paper-bot"
        "/actions/workflows/daily.yml/runs?status=success&per_page=30"
    )


# --- workflow-side CLI -------------------------------------------------------


def _load_guard_cli():
    spec = importlib.util.spec_from_file_location(
        "catchup_guard", ROOT / "scripts" / "catchup_guard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_writes_true_when_nothing_was_delivered(tmp_path, monkeypatch, capsys):
    # Arrange
    out = tmp_path / "gh_output"
    monkeypatch.setenv("WORKFLOW_FILE", "daily.yml")
    monkeypatch.setenv("GITHUB_REPOSITORY", "synbeelab/synbee-paper-bot")
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    cli = _load_guard_cli()
    monkeypatch.setattr(cli, "fetch_successful_runs", lambda *a, **k: [])

    # Act
    assert cli.main() == 0

    # Assert
    assert out.read_text(encoding="utf-8").strip() == "should_run=true"


def test_cli_writes_false_when_today_already_delivered(tmp_path, monkeypatch):
    # Arrange
    out = tmp_path / "gh_output"
    for key, value in {
        "WORKFLOW_FILE": "daily.yml",
        "GITHUB_REPOSITORY": "synbeelab/synbee-paper-bot",
        "GITHUB_TOKEN": "t0ken",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_RUN_ID": "777",
        "GITHUB_OUTPUT": str(out),
    }.items():
        monkeypatch.setenv(key, value)
    cli = _load_guard_cli()
    delivered = [
        {
            "id": 100,
            "conclusion": "success",
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    ]
    monkeypatch.setattr(cli, "fetch_successful_runs", lambda *a, **k: delivered)

    # Act
    assert cli.main() == 0

    # Assert
    assert out.read_text(encoding="utf-8").strip() == "should_run=false"


def test_cli_runs_anyway_when_the_env_is_misconfigured(tmp_path, monkeypatch):
    """No token, no workflow name — still must not block the delivery."""
    out = tmp_path / "gh_output"
    monkeypatch.delenv("WORKFLOW_FILE", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "synbeelab/synbee-paper-bot")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    cli = _load_guard_cli()

    assert cli.main() == 0
    assert out.read_text(encoding="utf-8").strip() == "should_run=true"


def test_cli_runs_anyway_when_the_api_is_down(tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    for key, value in {
        "WORKFLOW_FILE": "weekly.yml",
        "GITHUB_REPOSITORY": "synbeelab/synbee-paper-bot",
        "GITHUB_TOKEN": "t0ken",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_RUN_ID": "1",
        "GITHUB_OUTPUT": str(out),
    }.items():
        monkeypatch.setenv(key, value)
    cli = _load_guard_cli()

    def boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(cli, "fetch_successful_runs", boom)

    assert cli.main() == 0
    assert out.read_text(encoding="utf-8").strip() == "should_run=true"


def test_cli_survives_a_missing_github_output(monkeypatch):
    """Running the guard locally must not crash for lack of Actions plumbing."""
    monkeypatch.setenv("WORKFLOW_FILE", "daily.yml")
    monkeypatch.setenv("GITHUB_REPOSITORY", "synbeelab/synbee-paper-bot")
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    cli = _load_guard_cli()

    assert cli.main() == 0


# --- the guard must never be the reason a run is skipped ---------------------


def test_cli_writes_the_output_before_it_prints(tmp_path, monkeypatch):
    """Printing is the last thing that happens, and it cannot fail the step.

    Found the hard way: the first version printed a ✓/↷ banner *before* writing
    $GITHUB_OUTPUT, and on a cp949 console the print raised UnicodeEncodeError.
    The output file stayed empty, so `needs.guard.outputs.should_run` was empty,
    the gate read it as "not true" — and the digest would have been skipped by
    the very guard that promises to fail open.
    """
    # Arrange
    out = tmp_path / "gh_output"
    for key, value in {
        "WORKFLOW_FILE": "daily.yml",
        "GITHUB_REPOSITORY": "synbeelab/synbee-paper-bot",
        "GITHUB_TOKEN": "t0ken",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_RUN_ID": "555",
        "GITHUB_OUTPUT": str(out),
    }.items():
        monkeypatch.setenv(key, value)
    cli = _load_guard_cli()
    monkeypatch.setattr(cli, "fetch_successful_runs", lambda *a, **k: [])

    def unprintable(*_a, **_k):
        raise UnicodeEncodeError("cp949", "↷", 0, 1, "illegal multibyte sequence")

    monkeypatch.setattr("builtins.print", unprintable)

    # Act
    assert cli.main() == 0

    # Assert
    assert out.read_text(encoding="utf-8").strip() == "should_run=true"


def test_everything_the_guard_prints_is_ascii():
    """Runners are UTF-8, but a guard that can crash on output is not a guard.

    Checks only what reaches a console, so Korean comments and docstrings stay
    allowed here as everywhere else in the repo.
    """
    tree = ast.parse((ROOT / "scripts" / "catchup_guard.py").read_text(encoding="utf-8"))

    printed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id in {"print", "_say"}):
            continue
        for piece in ast.walk(node):
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                printed.append(piece.value)

    assert printed, "expected the guard to log something"
    offenders = [text for text in printed if not text.isascii()]
    assert not offenders, f"keep the guard's own output plain ASCII: {offenders}"


def test_cli_runs_anyway_when_the_decision_itself_explodes(tmp_path, monkeypatch):
    """Last line of defence: nothing escaping `decide` may skip the delivery."""
    out = tmp_path / "gh_output"
    for key, value in {
        "WORKFLOW_FILE": "daily.yml",
        "GITHUB_REPOSITORY": "synbeelab/synbee-paper-bot",
        "GITHUB_TOKEN": "t0ken",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_RUN_ID": "555",
        "GITHUB_OUTPUT": str(out),
    }.items():
        monkeypatch.setenv(key, value)
    cli = _load_guard_cli()

    def explode(*_a, **_k):
        raise KeyboardInterrupt("runner went away")

    monkeypatch.setattr(cli, "decide", explode)

    assert cli.main() == 0
    assert out.read_text(encoding="utf-8").strip() == "should_run=true"
