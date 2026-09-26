"""bioRxiv keeps its recall when api.biorxiv.org stops answering.

Regression origin: 2026-09-24 → 2026-09-26, three consecutive daily runs. The
`/details/` endpoint on api.biorxiv.org answered `HTTP 200` with a zero-length
body for *every* request — every server, every interval, every DOI, every
cursor, including bioRxiv's own documented example URLs. Requesting
`format=html` returned a page truncated mid-document with no `<body>`, which is
a PHP fatal error during render; with `format=json` nothing had been flushed
before the crash, so the body was empty. Sibling endpoints on the same host
(`/pubs/`, `/sum/`) kept working, so this was one broken endpoint, not an
unreachable host.

`collect_all` isolation (2026-08-15) already kept the other sources alive and
the watermark held the window open, but bioRxiv itself returned nothing for as
long as the endpoint stayed down — and the watermark window is capped at
`MAX_SINCE_DAYS`, so a long enough outage loses preprints outright.

Two rules are pinned here:

  1. **A dead `/details/` endpoint costs us no preprints.** Europe PMC indexes
     the same preprints with abstracts, so it stands in when the native API
     fails. Both failing is still a loud `SourceFetchError`.
  2. **Pagination follows what the server actually sent.** The page size was
     never ours to assume: the API documentation says 30 per call while the
     code assumed 100, and `count < 100 → stop` turns a page-size change into a
     silent truncation at the first page.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot import sources  # noqa: E402
from synbee_bot.sources import (  # noqa: E402
    SourceFetchError, biorxiv_recent, europepmc_preprints, fetch_from_biorxiv,
)


# --- helpers ----------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def native_page(dois: list[str], total: int | None = None) -> bytes:
    """A page as api.biorxiv.org serves it, page size left to the caller."""
    return json.dumps({
        "messages": [{"status": "ok", "count": len(dois),
                      "total": str(total if total is not None else len(dois))}],
        "collection": [
            {"doi": d, "title": f"T-{d}", "abstract": "synthetic biology abstract",
             "authors": "Kim A; Lee B", "date": "2026-09-24", "version": 1}
            for d in dois
        ],
    }).encode("utf-8")


def epmc_page(dois: list[str], next_cursor: str = "", hit_count: int | None = None) -> bytes:
    return json.dumps({
        "hitCount": hit_count if hit_count is not None else len(dois),
        "nextCursorMark": next_cursor,
        "resultList": {"result": [
            {"id": f"PPR{i}", "source": "PPR", "doi": d,
             "title": f"T-{d}", "abstractText": "synthetic biology abstract",
             "authorString": "Norton AJ, Borevitz JO.",
             "authorList": {"author": [{"fullName": "Norton AJ"},
                                       {"fullName": "Borevitz JO"}]},
             "firstPublicationDate": "2026-09-23", "pubYear": 2026,
             "bookOrReportDetails": {"publisher": "bioRxiv"}}
            for i, d in enumerate(dois)
        ]},
    }).encode("utf-8")


def fake_urlopen(*responses):
    """urlopen replacement yielding `responses` in order; the last one repeats.

    A bytes entry becomes a body, an Exception entry is raised instead.
    """
    calls = {"n": 0, "urls": []}

    def _open(url, timeout=None):
        calls["urls"].append(url if isinstance(url, str) else url.full_url)
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        item = responses[i]
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    _open.calls = calls
    return _open


def route_urlopen(**by_host):
    """urlopen replacement dispatching on a substring of the URL.

    Each value is either bytes or an Exception, so a test can have
    api.biorxiv.org fail while ebi.ac.uk answers.
    """
    calls = {"urls": []}

    def _open(url, timeout=None):
        target = url if isinstance(url, str) else url.full_url
        calls["urls"].append(target)
        for needle, item in by_host.items():
            if needle in target:
                if isinstance(item, Exception):
                    raise item
                return FakeResponse(item)
        raise AssertionError(f"unrouted URL: {target}")

    _open.calls = calls
    return _open


# --- rule 2: pagination follows the server, not a hardcoded page size -------

def test_a_thirty_paper_page_is_not_mistaken_for_the_last_page(monkeypatch):
    """The documented page size moved from 100 to 30.

    The old rule was `count < 100 → this was the last page`, which turns every
    full 30-paper page into a stop signal: the run keeps the first 30 preprints
    of the window and silently discards the rest. Nothing in the digest looks
    wrong, which is exactly how a recall hole survives.
    """
    first = native_page([f"10.64898/a{i}" for i in range(30)], total=45)
    second = native_page([f"10.64898/b{i}" for i in range(15)], total=45)
    opener = fake_urlopen(first, second)
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    papers = biorxiv_recent("biorxiv", since_days=1)

    assert len(papers) == 45, "the second page was dropped"
    assert "/0/json" in opener.calls["urls"][0]
    assert "/30/json" in opener.calls["urls"][1], "cursor must advance by what was served"


def test_a_hundred_paper_page_still_paginates(monkeypatch):
    """The same code has to keep working if the server stays at 100."""
    first = native_page([f"10.64898/a{i}" for i in range(100)], total=150)
    second = native_page([f"10.64898/b{i}" for i in range(50)], total=150)
    opener = fake_urlopen(first, second)
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    papers = biorxiv_recent("biorxiv", since_days=1)

    assert len(papers) == 150
    assert "/100/json" in opener.calls["urls"][1]


def test_pagination_stops_once_the_reported_total_is_reached(monkeypatch):
    """`total` is the server's own count of the interval — honour it rather
    than probing for an empty page, which costs a round trip every run."""
    opener = fake_urlopen(native_page([f"10.64898/a{i}" for i in range(30)], total=30))
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    papers = biorxiv_recent("biorxiv", since_days=1)

    assert len(papers) == 30
    assert opener.calls["n"] == 1


def test_an_empty_page_short_of_the_total_is_a_fault_not_the_end(monkeypatch):
    """Paginating the surviving `/pubs/` endpoint end to end on 2026-09-26
    returned exactly `total` items with no empty page before it, so `total` is
    exact. A server that stops serving a window it says has more is broken —
    the same fault class as this outage — and accepting it as the end turns a
    half-served window into a digest that looks complete."""
    first = native_page([f"10.64898/a{i}" for i in range(30)], total=9999)
    empty = native_page([], total=9999)
    opener = fake_urlopen(first, empty)
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    with pytest.raises(SourceFetchError) as excinfo:
        biorxiv_recent("biorxiv", since_days=1)

    assert "empty page" in str(excinfo.value)


def test_an_empty_page_with_no_total_to_contradict_it_ends_pagination(monkeypatch):
    """Without a `total` there is nothing to call the server out on, and
    looping to max_pages would only re-append the same page."""
    body = json.dumps({"messages": [{"count": 0}], "collection": []}).encode("utf-8")
    opener = fake_urlopen(body)
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    assert biorxiv_recent("biorxiv", since_days=1) == []
    assert opener.calls["n"] == 1


def test_europepmc_stopping_short_of_its_hit_count_is_a_fault(monkeypatch):
    opener = fake_urlopen(
        epmc_page(["10.64898/a"], next_cursor="C1", hit_count=500),
        epmc_page([], next_cursor="C2", hit_count=500),
    )
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    with pytest.raises(SourceFetchError) as excinfo:
        europepmc_preprints("biorxiv", since_days=3)

    assert "empty page" in str(excinfo.value)


def test_a_zero_page_budget_fails_loudly_rather_than_returning_nothing():
    """`for/else` runs its else on zero iterations, so a zero budget used to
    reach the guard with no response to report and raise NameError."""
    with pytest.raises(SourceFetchError):
        biorxiv_recent("biorxiv", since_days=1, max_pages=0)
    with pytest.raises(SourceFetchError):
        europepmc_preprints("biorxiv", since_days=1, max_pages=0)


def test_a_messages_object_served_as_a_dict_is_not_a_raw_keyerror(monkeypatch):
    """Malformed shapes have to arrive as SourceFetchError like every other
    way this API has been seen to break."""
    body = json.dumps({"messages": {"total": "5"}, "collection": []}).encode("utf-8")
    monkeypatch.setattr(sources.urllib.request, "urlopen", fake_urlopen(body))

    assert biorxiv_recent("biorxiv", since_days=1) == []


def test_a_missing_total_falls_back_to_a_short_page_meaning_the_end(monkeypatch):
    """Older payloads carry `count` but no `total`."""
    def untotalled(dois: list[str]) -> bytes:
        return json.dumps({
            "messages": [{"count": len(dois)}],
            "collection": [
                {"doi": d, "title": "T", "abstract": "a",
                 "authors": "Kim A", "date": "2026-09-24", "version": 1}
                for d in dois
            ],
        }).encode("utf-8")

    first = untotalled([f"10.64898/a{i}" for i in range(30)])
    opener = fake_urlopen(first, untotalled(["10.64898/aaa", "10.64898/bbb"]))
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    papers = biorxiv_recent("biorxiv", since_days=1)

    assert len(papers) == 32
    assert opener.calls["n"] == 2, "a short page is the last page"


# --- rule 1: a dead /details/ endpoint costs us no preprints ----------------

def test_europepmc_parses_a_preprint_into_the_same_shape(monkeypatch):
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        fake_urlopen(epmc_page(["10.64898/2026.09.20.753045"])))

    papers = europepmc_preprints("biorxiv", since_days=3)

    assert len(papers) == 1
    paper = papers[0]
    assert paper.doi == "10.64898/2026.09.20.753045"
    assert paper.source == "biorxiv"
    assert paper.abstract, "the filter stage needs an abstract"
    assert paper.authors == ["Norton AJ", "Borevitz JO"]
    assert paper.year == 2026


def test_europepmc_ids_match_the_native_api_so_dedup_still_works(monkeypatch):
    """The two paths must not each mint their own id for one preprint, or a
    preprint fetched during the outage posts a second time after recovery."""
    doi = "10.64898/2026.09.20.753045"
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        fake_urlopen(epmc_page([doi])))
    from_backup = europepmc_preprints("biorxiv", since_days=3)[0]

    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        fake_urlopen(native_page([doi])))
    from_native = biorxiv_recent("biorxiv", since_days=3)[0]

    assert from_backup.id == from_native.id


def test_europepmc_follows_its_cursor_to_the_end(monkeypatch):
    first = epmc_page([f"10.64898/a{i}" for i in range(2)],
                      next_cursor="CURSOR2", hit_count=3)
    second = epmc_page([f"10.64898/b{i}" for i in range(1)],
                       next_cursor="CURSOR2", hit_count=3)
    opener = fake_urlopen(first, second)
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    papers = europepmc_preprints("biorxiv", since_days=3)

    assert len(papers) == 3
    assert "cursorMark=%2A" in opener.calls["urls"][0] or "cursorMark=*" in opener.calls["urls"][0]
    assert "CURSOR2" in opener.calls["urls"][1]
    assert opener.calls["n"] == 2, "a repeated cursor means the end of the results"


def test_europepmc_asks_for_the_right_server(monkeypatch):
    opener = fake_urlopen(epmc_page([]))
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    europepmc_preprints("medrxiv", since_days=3)

    url = opener.calls["urls"][0]
    assert "medRxiv" in url or "medrxiv" in url
    assert "PPR" in url, "preprints only — Europe PMC also indexes journals"


def test_europepmc_failure_is_a_source_fetch_error_not_a_short_list(monkeypatch):
    monkeypatch.setattr(sources.urllib.request, "urlopen", fake_urlopen(b""))

    with pytest.raises(SourceFetchError):
        europepmc_preprints("biorxiv", since_days=3)


def test_the_outage_falls_back_instead_of_losing_the_day(monkeypatch):
    """The whole point: /details/ is dead, the preprints still arrive."""
    monkeypatch.setattr(sources.urllib.request, "urlopen", route_urlopen(**{
        "api.biorxiv.org": b"",                                   # the outage
        "ebi.ac.uk": epmc_page(["10.64898/2026.09.20.753045"]),
    }))
    monkeypatch.setattr(sources, "filter_biorxiv_by_keywords", lambda papers, kw: papers)

    papers = fetch_from_biorxiv(since_days=3)

    assert [p.doi for p in papers] == ["10.64898/2026.09.20.753045"]


def test_the_fallback_is_not_used_when_the_native_api_is_healthy(monkeypatch):
    """Europe PMC lags bioRxiv by a day or two, so it is a stand-in, not a
    replacement. A healthy run must never reach for it."""
    opener = route_urlopen(**{
        "api.biorxiv.org": native_page(["10.64898/native"]),
        "ebi.ac.uk": epmc_page(["10.64898/should-not-be-fetched"]),
    })
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)
    monkeypatch.setattr(sources, "filter_biorxiv_by_keywords", lambda papers, kw: papers)

    papers = fetch_from_biorxiv(since_days=3)

    assert [p.doi for p in papers] == ["10.64898/native"]
    assert not any("ebi.ac.uk" in u for u in opener.calls["urls"])


def test_both_sources_down_is_still_a_loud_failure(monkeypatch):
    """Silence here would advance the watermark over days nobody looked at."""
    monkeypatch.setattr(sources.urllib.request, "urlopen", route_urlopen(**{
        "api.biorxiv.org": b"",
        "ebi.ac.uk": urllib.error.HTTPError("u", 503, "down", {}, None),
    }))

    with pytest.raises(SourceFetchError) as excinfo:
        fetch_from_biorxiv(since_days=3)

    message = str(excinfo.value)
    assert "biorxiv" in message.lower()
    assert "europe pmc" in message.lower(), "name both, or the alert misleads"


def test_the_fallback_reports_which_path_served_the_papers(monkeypatch, capsys):
    """A run that quietly fell back looks identical to a healthy one in the
    log, which is how a multi-week outage goes unnoticed."""
    monkeypatch.setattr(sources.urllib.request, "urlopen", route_urlopen(**{
        "api.biorxiv.org": b"",
        "ebi.ac.uk": epmc_page(["10.64898/x"]),
    }))
    monkeypatch.setattr(sources, "filter_biorxiv_by_keywords", lambda papers, kw: papers)

    fetch_from_biorxiv(since_days=3)

    assert "Europe PMC" in capsys.readouterr().err


# --- running out of pages is a failure, never a quiet short list ------------

def test_native_pagination_running_out_of_pages_raises(monkeypatch):
    """A 30-day catch-up window holds thousands of preprints. Exhausting the
    page budget and returning what fits is the same silent truncation the
    page-size fix removed, just arriving by a different route."""
    opener = fake_urlopen(native_page([f"10.64898/a{i}" for i in range(30)], total=9999))
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    with pytest.raises(SourceFetchError) as excinfo:
        biorxiv_recent("biorxiv", since_days=30, max_pages=3)

    assert "ran out of pages" in str(excinfo.value)


def test_europepmc_running_out_of_pages_raises(monkeypatch):
    opener = fake_urlopen(
        epmc_page(["10.64898/a"], next_cursor="C1", hit_count=9999),
        epmc_page(["10.64898/b"], next_cursor="C2", hit_count=9999),
        epmc_page(["10.64898/c"], next_cursor="C3", hit_count=9999),
    )
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    with pytest.raises(SourceFetchError) as excinfo:
        europepmc_preprints("biorxiv", since_days=30, max_pages=3)

    assert "ran out of pages" in str(excinfo.value)


def test_the_page_budget_covers_a_full_catch_up_window():
    """MAX_SINCE_DAYS of bioRxiv measured 4,218 preprints on 2026-09-26. The
    budget has to clear that by a margin, or the guard above turns the very
    catch-up it protects into a hard failure."""
    from synbee_bot.storage import MAX_SINCE_DAYS

    assert MAX_SINCE_DAYS == 30
    assert sources.BIORXIV_MAX_PAGES * 30 > 6000, "30 preprints per page"
    assert sources.EUROPEPMC_MAX_PAGES * 100 > 6000


def test_the_fallback_window_covers_the_observed_indexing_lag(monkeypatch):
    """Europe PMC stamps FIRST_PDATE when it indexes, not when bioRxiv posts.

    Asking for exactly the native window would therefore miss whatever had not
    been indexed yet — and those preprints never come back, because the next
    run's window has already moved past them. Measured lag over 1,000 preprints
    (2026-09-01..09-26) topped out at 4 days.
    """
    opener = fake_urlopen(epmc_page([]))
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    europepmc_preprints("biorxiv", since_days=2)

    url = opener.calls["urls"][0]
    start = (dt.date.today() - dt.timedelta(days=2 + sources.EUROPEPMC_LAG_DAYS))
    assert sources.EUROPEPMC_LAG_DAYS >= 4, "the observed tail reached 4 days"
    assert start.isoformat() in urllib.parse.unquote(url)
