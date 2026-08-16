"""Guards against a single source outage wiping out a whole run.

Two rules are pinned here:

  1. **A source never fails silently.** Anything short of a complete, parseable
     fetch raises `SourceFetchError`. Returning a short list looks identical to
     "nothing was published today", which is how papers disappear without a
     trace.
  2. **One dead source does not take the others down.** `collect_all` isolates
     each source, so a bioRxiv outage still lets the PubMed papers through, and
     reports which source failed instead of swallowing it.

Regression origin: 2026-08-15, run 31849376195. api.biorxiv.org answered
`HTTP 200` with a zero-length body; `json.loads("")` raised `JSONDecodeError`
out of `biorxiv_recent`, through `collect_all`, out of `main()` — killing the
run and discarding the PubMed papers already collected in the same call.
"""
from __future__ import annotations

import builtins
import datetime as dt
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot import sources  # noqa: E402
from synbee_bot import slack_dispatch as sources_slack  # noqa: E402
from synbee_bot.slack_dispatch import (  # noqa: E402
    build_source_alert_blocks, post_source_alert,
)
from synbee_bot.sources import SourceFetchError, biorxiv_recent, collect_all  # noqa: E402
from synbee_bot.storage import SeenDB, effective_since_days  # noqa: E402


# --- helpers ----------------------------------------------------------------

class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object urlopen yields as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def page(count: int, dois: list[str]) -> bytes:
    return json.dumps({
        "messages": [{"count": count}],
        "collection": [
            {"doi": d, "title": f"T-{d}", "abstract": "synthetic biology abstract",
             "authors": "Kim A; Lee B", "date": "2026-08-14", "version": 1}
            for d in dois
        ],
    }).encode("utf-8")


def fake_urlopen(*responses):
    """Return a urlopen replacement that yields `responses` in order.

    A bytes entry becomes a body; an Exception entry is raised instead.
    """
    calls = {"n": 0}

    def _open(url, timeout=None):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        item = responses[i]
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    _open.calls = calls
    return _open


# --- rule 1: a source never fails silently ----------------------------------

def test_empty_body_raises_source_fetch_error(monkeypatch):
    """HTTP 200 + zero-length body — the exact 2026-08-15 outage.

    It must surface as SourceFetchError, not a raw JSONDecodeError escaping to
    the top of the process.
    """
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        fake_urlopen(b"", b"", b"", b"", b""))

    with pytest.raises(SourceFetchError) as excinfo:
        biorxiv_recent("biorxiv", since_days=1)

    assert "biorxiv" in str(excinfo.value).lower()


def test_html_error_page_raises_source_fetch_error(monkeypatch):
    """Some outages serve an HTML holding page with a 200."""
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        fake_urlopen(b"<html><body>502 Bad Gateway</body></html>"))

    with pytest.raises(SourceFetchError):
        biorxiv_recent("biorxiv", since_days=1)


def test_transient_empty_body_is_retried_then_succeeds(monkeypatch):
    """A blip must not cost us the day's preprints."""
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        fake_urlopen(b"", page(2, ["10.1101/aaa", "10.1101/bbb"])))

    papers = biorxiv_recent("biorxiv", since_days=1)

    assert [p.doi for p in papers] == ["10.1101/aaa", "10.1101/bbb"]


def test_http_error_raises_instead_of_truncating(monkeypatch):
    """A mid-pagination HTTP error used to `break` and return the pages read so
    far. A short list is indistinguishable from a quiet day, so the papers on
    the unread pages vanished. Fail loudly and let the next run refetch."""
    err = urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        fake_urlopen(page(100, [f"10.1101/{i}" for i in range(100)]), err))

    with pytest.raises(SourceFetchError):
        biorxiv_recent("biorxiv", since_days=1)


def test_timeout_raises_source_fetch_error(monkeypatch):
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        fake_urlopen(TimeoutError("timed out")))

    with pytest.raises(SourceFetchError):
        biorxiv_recent("biorxiv", since_days=1)


def test_pubmed_retries_a_transient_5xx(monkeypatch):
    """NCBI answers 502 under load. One blip must not cost a day of PubMed."""
    err = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        fake_urlopen(err, b"<eSearchResult/>"))

    assert sources._ncbi_post(sources.ESEARCH, {"db": "pubmed"}) == "<eSearchResult/>"


def test_pubmed_gives_up_as_a_source_failure_not_a_raw_http_error(monkeypatch):
    err = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
    monkeypatch.setattr(sources.urllib.request, "urlopen", fake_urlopen(err))

    with pytest.raises(SourceFetchError):
        sources._ncbi_post(sources.ESEARCH, {"db": "pubmed"})


def test_pubmed_does_not_retry_a_bad_request(monkeypatch):
    """A malformed query is not transient — retrying just wastes the window."""
    err = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
    opener = fake_urlopen(err)
    monkeypatch.setattr(sources.urllib.request, "urlopen", opener)

    with pytest.raises(SourceFetchError):
        sources._ncbi_post(sources.ESEARCH, {"db": "pubmed"})

    assert opener.calls["n"] == 1


def test_missing_feedparser_is_a_failure_not_an_empty_result(monkeypatch):
    """`feedparser not installed → return []` reads downstream as "no papers
    today" and advances the RSS watermark past days nobody ever looked at."""
    real_import = builtins.__import__

    def no_feedparser(name, *a, **kw):
        if name == "feedparser":
            raise ImportError("no feedparser")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_feedparser)

    with pytest.raises(SourceFetchError):
        sources.fetch_from_rss(1)


# --- rule 2: one dead source does not take the others down ------------------

def test_collect_all_keeps_working_sources_when_one_fails(monkeypatch):
    """The 2026-08-15 loss: PubMed had already been fetched when bioRxiv blew
    up, and every one of those papers was thrown away with the process."""
    pubmed_papers = [sources.Paper(
        id="pubmed:1", source="pubmed", title="T", authors=["A"], journal="J",
        year=2026, abstract="a", doi=None, url="https://e.org/1",
        published="2026-08-14")]
    monkeypatch.setattr(sources, "fetch_from_pubmed", lambda d: pubmed_papers)
    monkeypatch.setattr(sources, "fetch_from_biorxiv",
                        lambda d: (_ for _ in ()).throw(SourceFetchError("biorxiv down")))
    monkeypatch.setattr(sources, "fetch_from_rss", lambda d: [])

    result = collect_all(since_days_pubmed=1, since_days_biorxiv=1, since_days_rss=1)

    assert [p.id for p in result.papers["pubmed"]] == ["pubmed:1"]
    assert "biorxiv" in result.failures
    assert "biorxiv" not in result.papers
    assert result.succeeded == {"pubmed", "rss"}


def test_collect_all_reports_no_failures_when_all_sources_work(monkeypatch):
    monkeypatch.setattr(sources, "fetch_from_pubmed", lambda d: [])
    monkeypatch.setattr(sources, "fetch_from_biorxiv", lambda d: [])
    monkeypatch.setattr(sources, "fetch_from_rss", lambda d: [])

    result = collect_all(since_days_pubmed=1, since_days_biorxiv=1, since_days_rss=1)

    assert result.failures == {}
    assert result.succeeded == {"pubmed", "biorxiv", "rss"}


def test_collect_all_only_reports_enabled_sources(monkeypatch):
    monkeypatch.setattr(sources, "fetch_from_pubmed", lambda d: [])

    result = collect_all(since_days_pubmed=1, since_days_biorxiv=1, since_days_rss=1,
                         biorxiv=False, rss=False)

    assert result.succeeded == {"pubmed"}
    assert result.failures == {}


# --- window recovery: a missed run must widen the next one ------------------

TODAY = dt.date(2026, 8, 15)


def test_window_is_the_configured_one_when_there_is_no_history():
    assert effective_since_days(1, None, today=TODAY) == 1


def test_window_widens_to_cover_a_missed_run():
    """Yesterday's run died, so today must reach back past it.

    Without this, `reldate=1` skips straight over the crashed day and those
    papers are never fetched again by any run.
    """
    last_success = dt.date(2026, 8, 12)  # 3 days ago

    assert effective_since_days(1, last_success, today=TODAY) == 4


def test_window_keeps_one_day_of_overlap_on_a_healthy_streak():
    """Papers indexed after yesterday's run but before midnight sit in the seam
    between two 1-day windows. Overlap is free — seen.db dedups it before the
    LLM stage — so always reach back one extra day."""
    assert effective_since_days(1, dt.date(2026, 8, 14), today=TODAY) == 2


def test_window_never_shrinks_below_the_configured_value():
    assert effective_since_days(7, dt.date(2026, 8, 14), today=TODAY) == 7


def test_window_is_capped_so_an_old_watermark_cannot_run_away():
    assert effective_since_days(1, dt.date(2025, 1, 1), today=TODAY, max_days=30) == 30


def test_future_watermark_does_not_produce_a_negative_window():
    assert effective_since_days(1, dt.date(2026, 8, 20), today=TODAY) == 1


# --- the failure has to be visible where people actually look ---------------

def test_source_alert_names_the_source_and_the_reason():
    blocks = build_source_alert_blocks(
        {"biorxiv": "SourceFetchError: empty response body (HTTP 200)"},
        "2026-08-15")

    text = blocks[0]["text"]["text"]
    assert "biorxiv" in text
    assert "empty response body" in text
    assert "2026-08-15" in text


def test_source_alert_is_a_standalone_message_not_a_digest_footer():
    """On a day when nothing passes the filter there is no digest to attach a
    warning to, so the outage would otherwise be completely silent."""
    blocks = build_source_alert_blocks({"biorxiv": "down"}, "2026-08-15")

    assert blocks  # a message exists on its own, independent of any paper posts


def test_no_alert_when_every_source_worked():
    assert post_source_alert("token", "C123", {}, "2026-08-15") is False


def test_alert_reaches_slack_as_a_well_formed_message(monkeypatch):
    """Block Kit shape is only validated by Slack at post time, and
    post_source_alert swallows its own exceptions — so a malformed block would
    fail silently in production and pass every content-only test."""
    sent = {}

    class FakeClient:
        def chat_postMessage(self, **kw):
            sent.update(kw)
            return {"ok": True}

    monkeypatch.setattr(sources_slack, "make_slack_client", lambda token: FakeClient())

    ok = post_source_alert("xoxb-test", "C123", {"biorxiv": "empty body"}, "2026-08-15")

    assert ok is True
    assert sent["channel"] == "C123"
    assert sent["text"], "a fallback text is required for notifications"
    for block in sent["blocks"]:
        assert block["type"] in {"section", "divider"}
        if block["type"] == "section":
            assert block["text"]["type"] == "mrkdwn"
            assert len(block["text"]["text"]) <= 3000, "Slack rejects >3000 chars"


def test_alert_survives_a_reason_too_long_for_a_slack_block():
    """Fetch errors carry full URLs, and several can fail at once. Slack rejects
    a section over 3000 characters outright — which would drop the one message
    telling the reader the digest is incomplete."""
    blocks = build_source_alert_blocks(
        {f"src{i}": "x" * 4000 for i in range(4)}, "2026-08-15")

    for block in blocks:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= 3000


def test_alert_still_names_every_failed_source_after_truncation():
    """Truncation must not cost the reader a source name."""
    blocks = build_source_alert_blocks(
        {"pubmed": "y" * 4000, "biorxiv": "z" * 4000}, "2026-08-15")

    text = blocks[0]["text"]["text"]
    assert "pubmed" in text
    assert "biorxiv" in text


# --- watermark persistence --------------------------------------------------

def test_watermark_round_trips(tmp_path):
    db = SeenDB(tmp_path / "seen.db")

    assert db.get_source_watermark("pubmed") is None
    db.mark_source_success("pubmed", TODAY)

    assert db.get_source_watermark("pubmed") == TODAY
    db.close()


def test_watermark_survives_reopen(tmp_path):
    """It lives in seen.db, which the workflow caches with `if: always()` — so
    the recovery window is still correct after a crashed run."""
    path = tmp_path / "seen.db"
    db = SeenDB(path)
    db.mark_source_success("biorxiv", dt.date(2026, 8, 10))
    db.close()

    reopened = SeenDB(path)

    assert reopened.get_source_watermark("biorxiv") == dt.date(2026, 8, 10)
    reopened.close()


def test_a_failed_source_keeps_its_old_watermark(tmp_path):
    """Only advance on success — that is the whole recovery mechanism."""
    db = SeenDB(tmp_path / "seen.db")
    db.mark_source_success("biorxiv", dt.date(2026, 8, 13))

    # biorxiv fails today; pubmed succeeds. Only pubmed advances.
    db.mark_source_success("pubmed", TODAY)

    assert db.get_source_watermark("biorxiv") == dt.date(2026, 8, 13)
    assert effective_since_days(1, db.get_source_watermark("biorxiv"), today=TODAY) == 3
    db.close()
