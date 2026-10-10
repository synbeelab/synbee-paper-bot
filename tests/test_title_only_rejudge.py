"""A paper rejected on its title alone must get a second, abstract-backed look.

Until 2026-10-10 a title-only NO was persisted like any other verdict, and the
DOI dedup then threw away every later copy of the paper — including the PubMed
copy that carried the abstract. 28~43% of each weekly sweep was judged this way.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot import rejudge  # noqa: E402
from synbee_bot.models import Paper, Verdict  # noqa: E402
from synbee_bot.sources import CollectResult  # noqa: E402
from synbee_bot.storage import SeenDB  # noqa: E402

REAL = "We engineered a polyketide synthase in Streptomyces to make a new macrolide."
TODAY = dt.date(2026, 10, 10)


def paper(pid: str, doi: str | None, abstract: str = "", source: str = "rss") -> Paper:
    return Paper(id=pid, source=source, title=f"T-{pid}", authors=["A"],
                 journal="Cell", year=2026, abstract=abstract, doi=doi,
                 url=f"https://example.org/{pid}", published="2026-10-01")


NO = Verdict("NO", None, 3, "irrelevant")
YES = Verdict("YES", 1, 8, "relevant")
ERR = Verdict("NO", None, 0, "", is_error=True)


def no_lookup(papers, *, timeout):
    return list(papers)


# --- what gets queued ---------------------------------------------------------

def test_only_title_only_rejects_with_a_doi_are_queued(tmp_path):
    db = SeenDB(tmp_path / "seen.db")
    persisted = [
        (paper("a", "10.1/a"), NO),               # queued
        (paper("b", "10.1/b", REAL), NO),         # had its abstract: final
        (paper("c", None), NO),                   # nothing to look up by
        (paper("d", "10.1/d"), YES),              # posted: nothing lost
        (paper("e", "10.1/e"), ERR),              # already retried elsewhere
        (paper("f", "10.1/f"), Verdict("YES", 1, 5, "weak")),  # below threshold
    ]
    assert rejudge.queue_title_only_rejects(db, persisted, min_score=6, today=TODAY) == 2
    assert sorted(p.id for p, _ in db.list_title_only()) == ["a", "f"]


def test_requeueing_keeps_the_original_date(tmp_path):
    db = SeenDB(tmp_path / "seen.db")
    db.queue_title_only(paper("a", "10.1/a"), TODAY - dt.timedelta(days=20))
    db.queue_title_only(paper("a", "10.1/a"), TODAY)
    [(_, first)] = db.list_title_only()
    assert first == TODAY - dt.timedelta(days=20)


# --- recovery -----------------------------------------------------------------

def test_abstract_from_this_runs_sources_wins_over_a_lookup(tmp_path):
    """The PubMed copy that DOI dedup is about to discard IS the abstract."""
    db = SeenDB(tmp_path / "seen.db")
    db.queue_title_only(paper("doi:10.1/A", "10.1/A"), TODAY)

    def must_not_look_up(papers, *, timeout):
        assert papers == [], "already recovered from the run's own sources"
        return []

    pubmed_copy = paper("pubmed:1", "10.1/a", REAL, source="pubmed")
    out = rejudge.recover_title_only(db, collected=[pubmed_copy], today=TODAY,
                                     lookup=must_not_look_up)
    assert [(p.id, p.abstract) for p in out] == [("doi:10.1/A", REAL)]


def test_europe_pmc_recovery_and_misses(tmp_path):
    db = SeenDB(tmp_path / "seen.db")
    db.queue_title_only(paper("a", "10.1/a"), TODAY)
    db.queue_title_only(paper("b", "10.1/b"), TODAY)

    def lookup(papers, *, timeout):
        return [Paper(**{**p.to_dict(), "abstract": REAL}) if p.id == "a" else p
                for p in papers]

    out = rejudge.recover_title_only(db, today=TODAY, lookup=lookup)
    assert [p.id for p in out] == ["a"]
    # Nothing is dropped from the queue until the second verdict is persisted.
    assert sorted(p.id for p, _ in db.list_title_only()) == ["a", "b"]


def test_old_entries_age_out_but_not_on_a_dry_run(tmp_path):
    db = SeenDB(tmp_path / "seen.db")
    db.queue_title_only(paper("old", "10.1/old"), TODAY - dt.timedelta(days=31))
    db.queue_title_only(paper("new", "10.1/new"), TODAY - dt.timedelta(days=30))

    rejudge.recover_title_only(db, today=TODAY, lookup=no_lookup, expire=False)
    assert len(db.list_title_only()) == 2

    rejudge.recover_title_only(db, today=TODAY, lookup=no_lookup)
    assert [p.id for p, _ in db.list_title_only()] == ["new"]


def test_settle_retires_only_what_was_persisted(tmp_path):
    db = SeenDB(tmp_path / "seen.db")
    for pid in ("a", "b"):
        db.queue_title_only(paper(pid, f"10.1/{pid}"), TODAY)
    recovered = [paper("a", "10.1/a", REAL), paper("b", "10.1/b", REAL)]
    rejudge.settle_title_only(db, recovered, [(recovered[0], NO)])
    assert [p.id for p, _ in db.list_title_only()] == ["b"]


def test_an_empty_queue_costs_no_lookup(tmp_path):
    db = SeenDB(tmp_path / "seen.db")

    def boom(papers, *, timeout):
        raise AssertionError("must not be called")

    assert rejudge.recover_title_only(db, today=TODAY, lookup=boom) == []


# --- end to end through run_daily.py -------------------------------------------

def _load_run_daily():
    spec = importlib.util.spec_from_file_location(
        "run_daily", ROOT / "scripts" / "run_daily.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_config(rd, monkeypatch, tmp_path):
    class Full:
        slack_enabled = True
        slack_bot_token = "xoxb-test"
        slack_max_posts = None
        pubmed_enabled = True
        biorxiv_enabled = False
        rss_enabled = False
        pubmed_since_days = 1
        biorxiv_since_days = 1
        rss_since_days = 1
        max_since_days = 30
        llm_enabled = False
        llm_min_score = 6
        prefilter_non_articles = True
        abstract_backfill_enabled = True
        abstract_backfill_timeout = 5
        abstract_rejudge_days = 30
        seen_db_path = tmp_path / "seen.db"

        def target_channel(self, score: int) -> str:
            return "C0B2QJ179K6"

    monkeypatch.setattr(rd, "load_config", lambda: Full())


def test_a_title_only_reject_is_rejudged_and_posted_when_pubmed_brings_the_abstract(
        monkeypatch, tmp_path):
    """The exact hole: day 1 judges the title, day 2 PubMed brings the abstract."""
    rd = _load_run_daily()
    _stub_config(rd, monkeypatch, tmp_path)
    monkeypatch.setattr(rd, "backfill_abstracts", lambda papers, **kw: list(papers))
    monkeypatch.setattr(rejudge, "backfill_abstracts", lambda papers, **kw: list(papers))
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rd, "post_summary", lambda *a, **kw: None)

    # Day 1: title only, judged NO, persisted AND queued.
    crossref = paper("doi:10.1/x", "10.1/X")
    monkeypatch.setattr(rd, "collect_all", lambda **kw: CollectResult(
        papers={"pubmed": [crossref]}, failures={}, succeeded={"pubmed"}))
    monkeypatch.setattr(rd, "post_papers", lambda *a, **kw: (0, []))
    monkeypatch.setattr(sys, "argv", ["run_daily.py", "--no-llm"])   # score 5 → NO
    assert rd.main() == 0
    db = SeenDB(tmp_path / "seen.db")
    assert [p.id for p, _ in db.list_title_only()] == ["doi:10.1/x"]
    db.close()

    # Day 2: PubMed sends the same DOI with its abstract. DOI dedup drops the
    # PubMed copy, but the queued paper comes back carrying that abstract.
    pubmed = paper("pubmed:9", "10.1/x", REAL, source="pubmed")
    monkeypatch.setattr(rd, "collect_all", lambda **kw: CollectResult(
        papers={"pubmed": [pubmed]}, failures={}, succeeded={"pubmed"}))
    posted: list[list] = []
    monkeypatch.setattr(rd, "post_papers",
                        lambda tok, ch, passing, **kw: posted.append(passing) or
                        (len(passing), []))
    monkeypatch.setattr(sys, "argv", ["run_daily.py", "--no-llm", "--min-score", "0"])
    assert rd.main() == 0

    assert len(posted) == 1
    [(p, _)] = posted[0]
    assert (p.id, p.abstract) == ("doi:10.1/x", REAL)
    db = SeenDB(tmp_path / "seen.db")
    assert db.list_title_only() == [], "done after its second verdict"
    db.close()
