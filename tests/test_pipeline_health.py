"""Pins the 2026-10-03 pipeline audit fixes and the watchdog's decisions.

What the audit found, all of it while every workflow run was green:
  * the same preprint posted twice — `biorxiv:<doi>` from the API and
    `rss:<link>` from the bioRxiv subject feed (HERO, PRIME, …), because RSS
    papers carried no DOI and daily deduped by id only;
  * Metabolic Engineering's RSS URL answered HTTP 403 with an HTML page, which
    feedparser read as an empty feed — "nothing published", every day;
  * nature.com bounced feed requests through a cookie handshake that
    feedparser's own fetcher cannot follow — same silent zero;
  * a weekly sweep failed (NCBI 500) inside a "successful" run, so the catch-up
    guard counted the day as delivered and the hole waited a week.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot import sources  # noqa: E402
from synbee_bot.dedup import drop_known_titles, merge_by_doi, title_key  # noqa: E402
from synbee_bot.models import Paper  # noqa: E402
from synbee_bot.sources import CollectResult, PartialSourceError, collect_all  # noqa: E402
from synbee_bot.storage import SeenDB  # noqa: E402
from synbee_bot.watchdog import (  # noqa: E402
    PIPELINES, Report, delivery_verdict, find_duplicate_posts, render,
    silent_feeds, volume_drops,
)

LONG_TITLE = "Heterologous engineering of receptors using OrthoRep for directed evolution"


def paper(pid: str, source: str, *, doi: str | None = None,
          title: str = LONG_TITLE) -> Paper:
    return Paper(id=pid, source=source, title=title, authors=["A"], journal=source,
                 year=2026, abstract="abs", doi=doi, url=f"https://x/{pid}",
                 published="2026-10-02")


# --- RSS: DOI, dead feeds, per-feed isolation ---------------------------------

@pytest.mark.parametrize("entry, expected", [
    ({"prism_doi": "10.1038/s41587-026-03307-w"}, "10.1038/s41587-026-03307-w"),
    ({"dc_identifier": "doi:10.64898/2026.09.30.755785"}, "10.64898/2026.09.30.755785"),
    ({"dc_identifier": "10.1016/j.cell.2026.09.002"}, "10.1016/j.cell.2026.09.002"),
    ({"dc_identifier": "S0092-8674(26)01071-8"}, None),
    ({}, None),
])
def test_rss_entry_doi_reads_every_publisher_layout(entry, expected):
    assert sources._rss_entry_doi(entry) == expected


class _Parsed(dict):
    def __init__(self, entries, **kw):
        super().__init__(kw)
        self.entries = entries


def test_an_html_page_that_parses_as_an_empty_feed_is_a_dead_feed():
    assert sources._dead_feed_reason(_Parsed([], bozo=1, bozo_exception="mismatched tag"))
    assert sources._dead_feed_reason(_Parsed([{"title": "x"}], bozo=1)) is None
    assert sources._dead_feed_reason(_Parsed([], bozo=0)) is None  # a genuinely empty feed


def _feeds(monkeypatch, bodies: dict[str, bytes | Exception]):
    feedparser = pytest.importorskip("feedparser")  # noqa: F841
    monkeypatch.setattr(sources, "_load_yaml", lambda p: {"rss_feeds": [
        {"name": name, "url": f"https://feed/{name}"} for name in bodies]})

    def download(url, **kw):
        body = bodies[url.rsplit("/", 1)[-1]]
        if isinstance(body, Exception):
            raise body
        return body
    monkeypatch.setattr(sources, "_download_feed", download)


RSS_OK = b"""<?xml version="1.0"?><rss version="2.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>t</title>
<item><title>A paper</title><link>https://www.biorxiv.org/content/10.64898/2026.10.01.1v1</link>
<dc:identifier>doi:10.64898/2026.10.01.1</dc:identifier></item></channel></rss>"""


def test_one_dead_feed_does_not_throw_away_the_healthy_feeds(monkeypatch):
    _feeds(monkeypatch, {"good": RSS_OK, "dead": b"<html><body>403</body></html"})
    with pytest.raises(PartialSourceError) as exc:
        sources.fetch_from_rss(3)
    assert "dead" in str(exc.value)
    assert [p.doi for p in exc.value.papers] == ["10.64898/2026.10.01.1"]


def test_a_feed_download_error_is_reported_not_swallowed(monkeypatch):
    _feeds(monkeypatch, {"good": RSS_OK, "down": OSError("timeout")})
    with pytest.raises(PartialSourceError, match="down: timeout"):
        sources.fetch_from_rss(3)


def test_collect_all_keeps_partial_papers_but_holds_the_watermark(monkeypatch):
    kept = [paper("rss:x", "rss")]
    monkeypatch.setattr(sources, "fetch_from_pubmed", lambda d: [])
    monkeypatch.setattr(sources, "fetch_from_biorxiv", lambda d: [])

    def partial(d):
        raise PartialSourceError("Metabolic Engineering: HTTP 403", kept)
    monkeypatch.setattr(sources, "fetch_from_rss", partial)

    result = collect_all(since_days_pubmed=1, since_days_biorxiv=1, since_days_rss=1)
    assert result.papers["rss"] == kept
    assert "rss" in result.failures          # alerted
    assert "rss" not in result.succeeded     # watermark held → window widens


# --- cross-source dedup ---------------------------------------------------------

RANK = {"pubmed": 0, "biorxiv": 1, "rss": 2}


def _rank(p: Paper) -> int:
    return RANK[p.source]


def test_the_same_preprint_from_api_and_rss_survives_once_as_the_api_copy():
    doi = "10.64898/2026.09.30.755785"
    rss = paper("rss:https://www.biorxiv.org/content/x?rss=1", "rss", doi=doi)
    api = paper(f"biorxiv:{doi}", "biorxiv", doi=doi.upper())
    assert merge_by_doi([rss, api], rank=_rank) == [api]


def test_a_doi_less_feed_entry_merges_with_its_doi_twin_on_title():
    pub = paper("pubmed:1", "pubmed", doi="10.1016/j.ymben.2026.01.001")
    sd = paper("rss:S1096717626001473", "rss")              # ScienceDirect: no DOI
    assert merge_by_doi([sd, pub], rank=_rank) == [pub]


def test_preprint_and_journal_version_are_not_merged_on_title():
    preprint = paper("biorxiv:10.64898/a", "biorxiv", doi="10.64898/a")
    journal = paper("pubmed:2", "pubmed", doi="10.1038/b")
    assert len(merge_by_doi([preprint, journal], rank=_rank)) == 2


def test_a_doi_less_journal_item_is_not_mistaken_for_its_preprint():
    """Review finding: the preprint was posted months ago; the ScienceDirect RSS
    item of the published version (no DOI) must still come through."""
    preprint = paper("biorxiv:10.1101/p", "biorxiv", doi="10.1101/p")
    sd = paper("rss:S1096717626009999", "rss")
    assert len(merge_by_doi([preprint, sd], rank=_rank)) == 2
    assert drop_known_titles([sd], {title_key(LONG_TITLE): {"preprint"}}) == [sd]


def test_short_titles_never_merge():
    a, b = paper("rss:a", "rss", title="Editorial"), paper("rss:b", "rss", title="Editorial")
    assert title_key("Editorial") is None
    assert len(merge_by_doi([a, b], rank=_rank)) == 2


def test_title_index_and_drop_known_titles(tmp_path):
    db = SeenDB(tmp_path / "seen.db")
    db.mark_seen(paper("rss:sd", "rss", title=LONG_TITLE))                    # no DOI
    db.mark_seen(paper("pubmed:9", "pubmed", doi="10.1/x",
                       title="A completely different paper with a DOI on record"))
    index = db.title_index()
    db.close()

    later_pubmed = paper("pubmed:1", "pubmed", doi="10.1016/y")              # same title
    other_version = paper("biorxiv:z", "biorxiv", doi="10.64898/z",
                          title="A completely different paper with a DOI on record")
    kept = drop_known_titles([later_pubmed, other_version], index)
    assert kept == [other_version]


def test_daily_counts_the_api_and_rss_copies_as_one_new_paper(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("run_daily", ROOT / "scripts" / "run_daily.py")
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    class Cfg:
        slack_enabled, slack_bot_token = True, "xoxb-test"
        pubmed_enabled = biorxiv_enabled = rss_enabled = True
        pubmed_since_days = biorxiv_since_days = rss_since_days = 1
        max_since_days, llm_enabled, llm_min_score = 30, False, 6
        prefilter_non_articles, abstract_backfill_enabled = True, False
        abstract_backfill_timeout = 5
        slack_max_posts = None
        seen_db_path = tmp_path / "seen.db"

        def target_channel(self, score):
            return "C0B2QJ179K6"

    doi = "10.64898/2026.09.30.755785"
    monkeypatch.setattr(rd, "load_config", lambda: Cfg())
    monkeypatch.setattr(rd, "collect_all", lambda **kw: CollectResult(
        papers={"biorxiv": [paper(f"biorxiv:{doi}", "biorxiv", doi=doi)],
                "rss": [paper("rss:link", "rss", doi=doi)]},
        failures={}, succeeded={"biorxiv", "rss"}))
    sent: list[dict] = []
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rd, "post_summary",
                        lambda client, channel, stats, title="": sent.append(stats))
    monkeypatch.setattr(sys, "argv", ["run_daily.py", "--no-llm"])

    assert rd.main() == 0
    assert sent[0]["new"] == 1


# --- watchdog: delivery ---------------------------------------------------------

DAILY = next(p for p in PIPELINES if p.workflow_file == "daily.yml")
WEEKLY = next(p for p in PIPELINES if p.workflow_file == "weekly.yml")
NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)      # Sat 18:00 KST


def run(rid, created="2026-10-03T01:27:36Z", conclusion="success", status="completed"):
    return {"id": rid, "created_at": created, "conclusion": conclusion, "status": status}


ALL_FRESH = {s: date(2026, 10, 3) for s in DAILY.sources + WEEKLY.sources}


def test_a_complete_delivery_is_ok():
    v = delivery_verdict(DAILY, [run(1)], {1: True}, ALL_FRESH, now=NOW)
    assert v.status == "ok" and not v.should_dispatch


def test_a_run_whose_work_job_the_guard_skipped_is_not_a_delivery():
    v = delivery_verdict(DAILY, [run(1)], {1: False}, ALL_FRESH, now=NOW)
    assert v.status == "missing" and v.should_dispatch


def test_no_run_today_means_dispatch():
    v = delivery_verdict(DAILY, [run(1, created="2026-10-02T01:27:36Z")], {}, ALL_FRESH, now=NOW)
    assert v.status == "missing" and v.should_dispatch


def test_a_run_in_flight_is_left_alone():
    v = delivery_verdict(DAILY, [run(2, conclusion=None, status="in_progress"), run(1)],
                         {1: True}, ALL_FRESH, now=NOW)
    assert v.status == "in_progress" and not v.should_dispatch


def test_weekly_is_not_due_on_a_weekday():
    v = delivery_verdict(WEEKLY, [], {}, {}, now=datetime(2026, 10, 2, 9, tzinfo=timezone.utc))
    assert v.status == "not_due"


def test_the_2026_09_19_case_a_sweep_failed_inside_a_green_run():
    """weekly_pubmed stayed at last week's date: dispatch now, not next Saturday."""
    marks = dict(ALL_FRESH, weekly_pubmed=date(2026, 9, 26))
    v = delivery_verdict(WEEKLY, [run(1, created="2026-10-03T05:33:17Z")], {1: True},
                         marks, now=NOW)
    assert v.status == "stale_sources" and v.stale == ("weekly_pubmed",)
    assert v.should_dispatch


def test_a_long_outage_is_reported_but_not_rerun_again_and_again():
    marks = dict(ALL_FRESH, biorxiv=date(2026, 9, 23))
    v = delivery_verdict(DAILY, [run(1)], {1: True}, marks, now=NOW)
    assert v.status == "stale_sources" and not v.should_dispatch


def test_a_source_that_failed_today_after_a_good_yesterday_is_rerun():
    marks = dict(ALL_FRESH, rss=date(2026, 10, 2))
    v = delivery_verdict(DAILY, [run(1)], {1: True}, marks, now=NOW)
    assert v.should_dispatch and v.stale == ("rss",)


# --- watchdog: content ----------------------------------------------------------

def row(rid, doi=None, title=LONG_TITLE, d="2026-10-03"):
    return {"id": rid, "doi": doi, "title": title, "d": d}


def test_duplicate_posts_are_found_by_doi_or_by_title_when_a_doi_is_missing():
    groups = find_duplicate_posts([
        row("biorxiv:10.1/a", doi="10.1/a"), row("rss:l", doi="10.1/A"),
        row("rss:sd", title="Overcoming protocatechuate accumulation in muconic acid production"),
        row("pubmed:5", doi="10.1/m",
            title="Overcoming protocatechuate accumulation in muconic acid production"),
        row("biorxiv:p", doi="10.1/p", title="Same title for preprint and journal version ok"),
        row("pubmed:p", doi="10.2/p", title="Same title for preprint and journal version ok"),
        row("biorxiv:q", doi="10.64898/q", title="A preprint and its DOI-less journal RSS twin"),
        row("rss:q", title="A preprint and its DOI-less journal RSS twin"),
    ])
    found = sorted(sorted(m["id"] for m in g) for g in groups)
    assert found == [["biorxiv:10.1/a", "rss:l"], ["pubmed:5", "rss:sd"]]


def _series(today: date, history: int, today_n: int) -> dict[date, int]:
    by_day = {today - dt.timedelta(days=d): history for d in range(1, 15)}
    by_day[today] = today_n
    return by_day


def test_the_biorxiv_truncation_shape_is_a_volume_drop():
    today = date(2026, 10, 3)
    found = volume_drops({"biorxiv": _series(today, 45, 4)}, today=today)
    assert found and "biorxiv" in found[0]


def test_ordinary_day_to_day_noise_is_not_a_volume_drop():
    today = date(2026, 10, 3)
    assert volume_drops({"pubmed": _series(today, 20, 10)}, today=today) == []
    assert volume_drops({"new": {today: 0}}, today=today) == []          # no history


def test_a_feed_that_went_quiet_is_flagged():
    today = date(2026, 10, 3)
    feeds = {"Cell": {today - dt.timedelta(days=d): 2 for d in range(5, 15)},
             "Metabolic Engineering": {}}
    assert len(silent_feeds(feeds, today=today)) == 1


def test_render_says_nothing_is_wrong_only_when_nothing_is():
    assert "이상 없음" in render(Report(), today=date(2026, 10, 3))
    r = Report(actions=["daily.yml → 재실행"])
    assert r.needs_attention and "자동 조치" in render(r, today=date(2026, 10, 3))
