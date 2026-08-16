"""Guards the weekly delta sweep against losing a whole week.

The weekly run has the same shape of hole the daily run had (fixed in #5), but a
worse blast radius. Its window is 7 days wide and it fires once a week, so a
single failed run means the next one starts *after* the week it missed — and no
run ever looks at those days again.

It matters more here than on the daily path: the weekly sweep is journal-only,
with no keyword gate, and exists precisely to catch the papers the daily
keyword query does *not* match. A paper lost from the weekly delta had no other
route to the digest.

So the weekly gets the same watermark: advanced only after a run has actually
delivered, and read back to widen the following window.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot.models import Paper  # noqa: E402
from synbee_bot.sources import SourceFetchError  # noqa: E402
from synbee_bot.storage import SeenDB  # noqa: E402

WEEKLY_SOURCE = "weekly_pubmed"


def _load_run_weekly():
    spec = importlib.util.spec_from_file_location(
        "run_weekly", ROOT / "scripts" / "run_weekly.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def paper(pid: str) -> Paper:
    return Paper(id=pid, source="pubmed", title=f"T-{pid}", authors=["A"],
                 journal="J", year=2026, abstract="abs", doi=None,
                 url=f"https://example.org/{pid}", published="2026-08-08")


def _wire(rw, monkeypatch, db_path, *, fetch, argv=("--no-llm", "--no-slack")):
    """Point run_weekly at a temp DB and a stub source."""
    class Full:
        slack_enabled = True
        slack_bot_token = "xoxb-test"
        weekly_channel = "C0B2QJ179K6"
        weekly_enabled = True
        weekly_since_days = 7
        weekly_min_score = 6
        weekly_max_posts = None
        llm_enabled = False
        max_since_days = 30
        seen_db_path = db_path

    monkeypatch.setattr(rw, "fetch_from_pubmed_weekly", fetch)
    monkeypatch.setattr(rw, "SeenDB", lambda _p: SeenDB(db_path))
    monkeypatch.setattr(rw, "load_config", lambda: Full())
    monkeypatch.setattr(sys, "argv", ["run_weekly.py", *argv])
    return Full


# --- the window ---------------------------------------------------------------

def test_uses_the_configured_window_when_there_is_no_history(tmp_path, monkeypatch):
    rw = _load_run_weekly()
    asked: list[int] = []
    _wire(rw, monkeypatch, tmp_path / "seen.db",
          fetch=lambda since_days: asked.append(since_days) or [])

    rw.main()

    assert asked == [7]


def test_window_widens_to_cover_a_missed_week(tmp_path, monkeypatch):
    """A weekly run that dies leaves a 7-day hole, not a 1-day one."""
    db_path = tmp_path / "seen.db"
    db = SeenDB(db_path)
    db.mark_source_success(WEEKLY_SOURCE, dt.date.today() - dt.timedelta(days=14))
    db.close()

    rw = _load_run_weekly()
    asked: list[int] = []
    _wire(rw, monkeypatch, db_path,
          fetch=lambda since_days: asked.append(since_days) or [])

    rw.main()

    assert asked == [15], "14 days since the last delivery, plus the seam day"


def test_window_is_capped(tmp_path, monkeypatch):
    db_path = tmp_path / "seen.db"
    db = SeenDB(db_path)
    db.mark_source_success(WEEKLY_SOURCE, dt.date(2024, 1, 1))
    db.close()

    rw = _load_run_weekly()
    asked: list[int] = []
    _wire(rw, monkeypatch, db_path,
          fetch=lambda since_days: asked.append(since_days) or [])

    rw.main()

    assert asked == [30]


def test_an_explicit_since_days_still_wins(tmp_path, monkeypatch):
    db_path = tmp_path / "seen.db"
    db = SeenDB(db_path)
    db.mark_source_success(WEEKLY_SOURCE, dt.date.today() - dt.timedelta(days=14))
    db.close()

    rw = _load_run_weekly()
    asked: list[int] = []
    _wire(rw, monkeypatch, db_path,
          fetch=lambda since_days: asked.append(since_days) or [],
          argv=("--no-llm", "--no-slack", "--since-days", "3"))

    rw.main()

    assert asked == [3]


# --- when the watermark may advance -------------------------------------------

def test_watermark_advances_after_a_delivered_run(tmp_path, monkeypatch):
    db_path = tmp_path / "seen.db"
    rw = _load_run_weekly()
    _wire(rw, monkeypatch, db_path, fetch=lambda since_days: [paper("p1")])

    assert rw.main() == 0

    db = SeenDB(db_path)
    assert db.get_source_watermark(WEEKLY_SOURCE) == dt.date.today()
    db.close()


def test_a_zero_delta_week_still_advances_the_watermark(tmp_path, monkeypatch):
    """Zero delta is a real answer — the sweep ran and found nothing new. Not
    advancing here would widen the window forever on a quiet stretch."""
    db_path = tmp_path / "seen.db"
    rw = _load_run_weekly()
    _wire(rw, monkeypatch, db_path, fetch=lambda since_days: [])

    assert rw.main() == 0

    db = SeenDB(db_path)
    assert db.get_source_watermark(WEEKLY_SOURCE) == dt.date.today()
    db.close()


def test_a_fetch_failure_leaves_the_watermark_alone(tmp_path, monkeypatch):
    """The whole recovery mechanism is this: no delivery, no advance."""
    db_path = tmp_path / "seen.db"
    db = SeenDB(db_path)
    db.mark_source_success(WEEKLY_SOURCE, dt.date(2026, 8, 9))
    db.close()

    rw = _load_run_weekly()

    def boom(since_days):
        raise SourceFetchError("NCBI: HTTP 502")

    _wire(rw, monkeypatch, db_path, fetch=boom)

    assert rw.main() == 1, "a failed sweep must exit non-zero"

    db = SeenDB(db_path)
    assert db.get_source_watermark(WEEKLY_SOURCE) == dt.date(2026, 8, 9)
    db.close()


def test_a_dry_run_never_advances_the_watermark(tmp_path, monkeypatch):
    db_path = tmp_path / "seen.db"
    rw = _load_run_weekly()
    _wire(rw, monkeypatch, db_path, fetch=lambda since_days: [paper("p1")],
          argv=("--no-llm", "--dry-run"))

    rw.main()

    db = SeenDB(db_path)
    assert db.get_source_watermark(WEEKLY_SOURCE) is None
    db.close()


# --- the failure has to be visible --------------------------------------------

def test_a_fetch_failure_is_announced_in_slack(tmp_path, monkeypatch):
    """A weekly sweep that dies is otherwise indistinguishable from a quiet week
    — and the zero report the reader is used to seeing never arrives."""
    db_path = tmp_path / "seen.db"
    rw = _load_run_weekly()
    alerts: list[dict] = []
    monkeypatch.setattr(rw, "post_source_alert",
                        lambda token, channel, failures, date: alerts.append(
                            {"channel": channel, "failures": failures}) or True)

    def boom(since_days):
        raise SourceFetchError("NCBI: HTTP 502")

    _wire(rw, monkeypatch, db_path, fetch=boom, argv=("--no-llm",))

    rw.main()

    assert len(alerts) == 1
    assert alerts[0]["channel"] == "C0B2QJ179K6"
    assert "502" in str(alerts[0]["failures"])
