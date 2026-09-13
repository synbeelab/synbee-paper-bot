"""Backfill may only ever ADD information — never drop, never overwrite, never raise."""
from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synbee_bot import abstracts  # noqa: E402
from synbee_bot.models import Paper  # noqa: E402

REAL = "A" * 200


def paper(pid: str, doi: str | None, abstract: str = "") -> Paper:
    return Paper(id=pid, source="crossref_toc", title=f"T-{pid}", authors=["A B"],
                 journal="Cell Reports", year=2026, abstract=abstract,
                 doi=doi, url="https://e.org", published="2026-08-30")


def test_only_missing_abstracts_are_looked_up(monkeypatch):
    seen: list[list[str]] = []

    def fake_lookup(dois, *, timeout):
        seen.append(list(dois))
        return {d: REAL for d in dois}

    monkeypatch.setattr(abstracts, "_lookup", fake_lookup)
    papers = [paper("a", "10.1/a"), paper("b", "10.1/b", REAL), paper("c", "10.1/c")]
    out = abstracts.backfill_abstracts(papers, log=lambda m: None)

    assert seen == [["10.1/a", "10.1/c"]]
    assert out[0].abstract == REAL
    assert out[1].abstract == REAL      # untouched
    assert out[2].abstract == REAL


def test_papers_without_a_doi_are_skipped(monkeypatch):
    monkeypatch.setattr(abstracts, "_lookup",
                        lambda dois, *, timeout: {d: REAL for d in dois})
    out = abstracts.backfill_abstracts([paper("a", None)], log=lambda m: None)
    assert out[0].abstract == ""


def test_inputs_are_not_mutated(monkeypatch):
    monkeypatch.setattr(abstracts, "_lookup",
                        lambda dois, *, timeout: {d: REAL for d in dois})
    original = paper("a", "10.1/a")
    out = abstracts.backfill_abstracts([original], log=lambda m: None)
    assert original.abstract == ""      # the caller's object is untouched
    assert out[0] is not original
    assert out[0].abstract == REAL


def test_a_paper_europepmc_does_not_know_keeps_its_empty_abstract(monkeypatch):
    monkeypatch.setattr(abstracts, "_lookup", lambda dois, *, timeout: {})
    papers = [paper("a", "10.1/a")]
    out = abstracts.backfill_abstracts(papers, log=lambda m: None)
    assert len(out) == 1 and out[0].abstract == ""


def test_a_dead_batch_costs_nobody_their_paper(monkeypatch):
    calls = {"n": 0}

    def flaky(dois, *, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("europepmc down")
        return {d: REAL for d in dois}

    monkeypatch.setattr(abstracts, "_lookup", flaky)
    monkeypatch.setattr(abstracts, "SLEEP_BETWEEN_BATCHES", 0)
    papers = [paper(f"p{i}", f"10.1/{i}") for i in range(4)]
    out = abstracts.backfill_abstracts(papers, batch_size=2, log=lambda m: None)

    assert len(out) == len(papers)                 # nothing dropped
    assert [p.abstract for p in out[:2]] == ["", ""]   # the dead batch
    assert [p.abstract for p in out[2:]] == [REAL, REAL]


def test_lookups_are_chunked(monkeypatch):
    sizes: list[int] = []
    monkeypatch.setattr(abstracts, "_lookup",
                        lambda dois, *, timeout: sizes.append(len(dois)) or {})
    monkeypatch.setattr(abstracts, "SLEEP_BETWEEN_BATCHES", 0)
    papers = [paper(f"p{i}", f"10.1/{i}") for i in range(7)]
    abstracts.backfill_abstracts(papers, batch_size=3, log=lambda m: None)
    assert sizes == [3, 3, 1]


def test_no_work_means_no_network(monkeypatch):
    def boom(dois, *, timeout):
        raise AssertionError("must not be called")

    monkeypatch.setattr(abstracts, "_lookup", boom)
    papers = [paper("a", "10.1/a", REAL)]
    assert abstracts.backfill_abstracts(papers, log=lambda m: None) == papers


def test_doi_case_does_not_matter(monkeypatch):
    monkeypatch.setattr(abstracts, "_lookup",
                        lambda dois, *, timeout: {d.lower(): REAL for d in dois})
    out = abstracts.backfill_abstracts([paper("a", "10.1/ABC")], log=lambda m: None)
    assert out[0].abstract == REAL


def test_markup_is_stripped():
    assert abstracts._clean("<p>Hello   <i>world</i></p>") == "Hello world"


def test_a_stub_abstract_still_counts_as_missing():
    assert abstracts.needs_abstract(paper("a", "10.1/a", "Abstract"))
    assert not abstracts.needs_abstract(paper("a", "10.1/a", REAL))
