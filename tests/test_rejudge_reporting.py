"""The re-judge tool's two counting helpers.

Both run against the same connection the sampler uses, and that sampler sets
`row_factory = sqlite3.Row`. A 2026-10-18 run died on exactly that — rows came
back as Row objects, which are not orderable — so every test here opens the
connection the way the script really does.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "rejudge_compare", ROOT / "scripts" / "rejudge_compare.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

SINCE = "2026-09-06"
ROWS = [
    # id, verdict, score, pushed_at
    ("y8a", "YES", 8, "2026-09-10"),
    ("y8b", "YES", 8, "2026-09-12"),
    ("y7", "YES", 7, "2026-09-11"),
    ("y6", "YES", 6, "2026-09-11"),
    ("y5", "YES", 5, "2026-09-11"),      # under the gate
    ("yold", "YES", 9, "2026-08-01"),    # before the window
    ("n4", "NO", 4, "2026-09-10"),
    ("n3a", "NO", 3, "2026-09-10"),
    ("n3b", "NO", 3, "2026-09-13"),
    ("n2", "NO", 2, "2026-09-13"),
    ("nold", "NO", 3, "2026-08-02"),     # before the window
]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE seen (id TEXT, source TEXT, title TEXT, abstract TEXT, "
        "journal TEXT, year INT, doi TEXT, url TEXT, "
        "verdict TEXT, score INT, pushed_at TEXT)")
    c.executemany(
        "INSERT INTO seen VALUES (?,'pubmed','T','abs','J',2026,NULL,'',?,?,?)", ROWS)
    # What the script does before either helper is called.
    c.row_factory = sqlite3.Row
    return c


def test_pass_distribution_survives_the_row_factory(conn):
    assert rc.pass_score_distribution(conn, SINCE, 6) == {6: 1, 7: 1, 8: 2}


def test_pass_distribution_excludes_rejects_low_scores_and_old_rows(conn):
    dist = rc.pass_score_distribution(conn, SINCE, 6)

    assert 5 not in dist, "below the gate"
    assert 9 not in dist, "outside the window"
    assert sum(dist.values()) == 4


def test_pass_distribution_is_ordered_by_score(conn):
    assert list(rc.pass_score_distribution(conn, SINCE, 6)) == [6, 7, 8]


def test_reject_population_counts_the_whole_window_not_the_sample(conn):
    pop = rc.reject_population(conn, SINCE, {4: 60, 3: 240, 2: 120})

    assert pop == {4: 1, 3: 2, 2: 1}, "quota is a sample cap, not a filter"


def test_reject_sample_by_ids_keeps_order_and_tolerates_missing(conn):
    got = rc.reject_sample_by_ids(conn, ["n3b", "gone", "n4"])

    assert [p.id for p in got] == ["n3b", "n4"]
