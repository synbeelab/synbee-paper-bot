"""Guards the DAILY digest against reporting NOTHING.

The weekly bot learned this on 2026-07-27 and grew ``_post_zero_summary``; the
daily bot never got the same treatment, and on 2026-09-06 it cost us a day of
doubt. That run was healthy — 20 collected, 4 new, ``Filter: 0 pass / 4 total``
(scores 1/2/3/3 on a thin Sunday) — but it posted nothing, so #papers-daily
looked exactly like a dead bot. A reviewer spent the week unable to tell an
empty day from a silent failure.

Daily has TWO silent exits, not one:
  * nothing new after dedup      (run_daily.py, the ``if not new_papers`` exit)
  * nothing passing after filter (the Slack push is gated on ``passing``)

Both must leave a card behind, and the numbers on that card must distinguish
them: "수집 20편 → 중복 제거 후 4편 → LLM 통과 0편" is a different day from
"수집 20편 → 중복 제거 후 0편".
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot.models import Paper  # noqa: E402
from synbee_bot.sources import CollectResult  # noqa: E402
from synbee_bot.storage import SeenDB  # noqa: E402


def _load_run_daily():
    """run_daily.py is a script, not a package module — load it by path."""
    spec = importlib.util.spec_from_file_location(
        "run_daily", ROOT / "scripts" / "run_daily.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def paper(pid: str) -> Paper:
    return Paper(id=pid, source="pubmed", title=f"T-{pid}", authors=["A"],
                 journal="J", year=2026, abstract="abs", doi=None,
                 url=f"https://example.org/{pid}", published="2026-09-06")


class Args:
    """Stand-in for argparse.Namespace."""

    def __init__(self, **kw):
        self.dry_run = kw.get("dry_run", False)
        self.no_slack = kw.get("no_slack", False)


class Cfg:
    """Minimal config stub for the zero-summary helper."""

    slack_enabled = True
    slack_bot_token = "xoxb-test"

    def target_channel(self, score: int) -> str:
        return "C0B2QJ179K6"


# --- the zero report itself ---------------------------------------------------

def test_zero_report_posts_exactly_one_card(monkeypatch):
    rd = _load_run_daily()
    sent: list[dict] = []
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rd, "post_summary",
                        lambda client, channel, stats, title="": sent.append(
                            {"channel": channel, "stats": stats, "title": title}))

    rd._post_zero_summary(Cfg(), Args(), collected=20, new=4, passed=0)

    assert len(sent) == 1, "a zero day must still produce exactly one card"
    assert sent[0]["channel"] == "C0B2QJ179K6"
    assert sent[0]["stats"]["collected"] == 20
    assert sent[0]["stats"]["new"] == 4
    assert sent[0]["stats"]["passed"] == 0
    assert sent[0]["stats"]["posted"] == 0


def test_zero_report_distinguishes_the_two_silent_exits():
    """The whole point of the card is telling these two days apart."""
    from synbee_bot.slack_dispatch import build_summary_blocks

    nothing_new = build_summary_blocks(
        {"date": "2026-09-06", "collected": 20, "new": 0, "passed": 0, "posted": 0})
    nothing_passed = build_summary_blocks(
        {"date": "2026-09-06", "collected": 20, "new": 4, "passed": 0, "posted": 0})

    a = nothing_new[0]["text"]["text"]
    b = nothing_passed[0]["text"]["text"]
    assert "중복 제거 후 0편" in a
    assert "중복 제거 후 4편" in b
    assert "푸시 0편" in a and "푸시 0편" in b
    assert a != b, "an empty day and a filtered-out day must not read identically"


@pytest.mark.parametrize("flag", ["dry_run", "no_slack"])
def test_zero_report_is_silent_under_dry_run_and_no_slack(monkeypatch, flag):
    rd = _load_run_daily()
    sent: list[dict] = []
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rd, "post_summary", lambda *a, **kw: sent.append({}))

    rd._post_zero_summary(Cfg(), Args(**{flag: True}), collected=5, new=0, passed=0)

    assert sent == [], f"--{flag} must not post to Slack"


def test_zero_report_is_silent_without_a_channel(monkeypatch):
    rd = _load_run_daily()
    sent: list[dict] = []
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rd, "post_summary", lambda *a, **kw: sent.append({}))

    class NoChannel(Cfg):
        def target_channel(self, score: int) -> str:
            return ""

    rd._post_zero_summary(NoChannel(), Args(), collected=5, new=0, passed=0)

    assert sent == [], "no channel configured must not raise, and must not post"


def test_a_failing_zero_report_never_kills_the_run(monkeypatch):
    """The report is a courtesy; it must not raise into the caller."""
    rd = _load_run_daily()
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())

    def boom(*a, **kw):
        raise RuntimeError("channel_not_found")

    monkeypatch.setattr(rd, "post_summary", boom)

    rd._post_zero_summary(Cfg(), Args(), collected=5, new=5, passed=0)  # no raise


# --- both silent exits must actually reach the helper -------------------------

def _stub_config(rd, monkeypatch, tmp_path):
    class Full:
        slack_enabled = True
        slack_bot_token = "xoxb-test"
        slack_channel_daily = "C0B2QJ179K6"
        slack_channel_priority = ""
        slack_channel_test = ""
        slack_use_test = False
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
        # PR #11이 들여온 키. 테스트는 네트워크를 타면 안 되므로 꺼 둔다.
        abstract_backfill_enabled = False
        abstract_backfill_timeout = 5
        seen_db_path = tmp_path / "seen.db"

        def target_channel(self, score: int) -> str:
            return self.slack_channel_daily

    monkeypatch.setattr(rd, "load_config", lambda: Full())
    return Full


def test_nothing_new_exit_posts_a_card(monkeypatch, tmp_path):
    """The ``if not new_papers`` exit used to return 0 in silence."""
    rd = _load_run_daily()
    _stub_config(rd, monkeypatch, tmp_path)

    # Everything collected is already in seen.db → new == 0.
    db = SeenDB(tmp_path / "seen.db")
    db.mark_seen(paper("p1"))
    db.close()

    monkeypatch.setattr(rd, "collect_all", lambda **kw: CollectResult(
        papers={"pubmed": [paper("p1")]}, failures={}, succeeded={"pubmed"}))

    sent: list[dict] = []
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rd, "post_summary",
                        lambda client, channel, stats, title="": sent.append(stats))
    monkeypatch.setattr(sys, "argv", ["run_daily.py"])

    assert rd.main() == 0
    assert len(sent) == 1, "an all-duplicates day must still report"
    assert sent[0]["new"] == 0
    assert sent[0]["collected"] == 1


def test_nothing_passing_exit_posts_a_card(monkeypatch, tmp_path):
    """2026-09-06's exact shape: new papers, all judged NO, nothing posted."""
    rd = _load_run_daily()
    _stub_config(rd, monkeypatch, tmp_path)

    papers = [paper("p1"), paper("p2")]
    monkeypatch.setattr(rd, "collect_all", lambda **kw: CollectResult(
        papers={"pubmed": list(papers)}, failures={}, succeeded={"pubmed"}))

    sent: list[dict] = []
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rd, "post_summary",
                        lambda client, channel, stats, title="": sent.append(stats))
    # post_papers must never be reached with an empty digest.
    monkeypatch.setattr(rd, "post_papers",
                        lambda *a, **kw: pytest.fail("posted an empty digest"))
    monkeypatch.setattr(sys, "argv", ["run_daily.py", "--no-llm"])

    assert rd.main() == 0
    assert len(sent) == 1, "a filtered-out day must still report"
    assert sent[0]["new"] == 2
    assert sent[0]["passed"] == 0

    # And the judged rejects must still be recorded, or every run re-judges them.
    db = SeenDB(tmp_path / "seen.db")
    assert db.filter_unseen(["p1", "p2"]) == set()
    db.close()


def test_a_normal_day_posts_no_zero_card(monkeypatch, tmp_path):
    """The card is for zero days only — it must not double up on a real digest."""
    rd = _load_run_daily()
    _stub_config(rd, monkeypatch, tmp_path)

    monkeypatch.setattr(rd, "collect_all", lambda **kw: CollectResult(
        papers={"pubmed": [paper("p1")]}, failures={}, succeeded={"pubmed"}))

    zero: list[dict] = []
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rd, "post_summary", lambda *a, **kw: zero.append({}))
    monkeypatch.setattr(rd, "post_papers", lambda *a, **kw: (1, []))
    # --no-llm scores 5; min_score 0 lets it through, so this is a "real" digest.
    monkeypatch.setattr(sys, "argv", ["run_daily.py", "--no-llm", "--min-score", "0"])

    assert rd.main() == 0
    assert zero == [], "a day that posted papers must not also post a zero card"


def test_zero_card_reports_the_dedup_count_not_the_post_prefilter_count(
        monkeypatch, tmp_path):
    """The card says "중복 제거 후 N편", so N must be the dedup count.

    `drop_non_articles` reassigns `new_papers`, so reading its length at the
    zero-card call site would quietly subtract the corrections the prefilter
    threw away — making a normal day look like a thinner one.
    """
    rd = _load_run_daily()
    Full = _stub_config(rd, monkeypatch, tmp_path)
    Full.prefilter_non_articles = True

    real = paper("p1")
    correction = paper("p2")
    correction = type(real)(**{**real.__dict__, "id": "p2",
                               "title": "Author Correction: something"})
    monkeypatch.setattr(rd, "collect_all", lambda **kw: CollectResult(
        papers={"pubmed": [real, correction]}, failures={}, succeeded={"pubmed"}))

    sent: list[dict] = []
    monkeypatch.setattr(rd, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rd, "post_summary",
                        lambda client, channel, stats, title="": sent.append(stats))
    monkeypatch.setattr(sys, "argv", ["run_daily.py", "--no-llm"])

    assert rd.main() == 0
    assert len(sent) == 1
    assert sent[0]["new"] == 2,         "the prefilter dropped one, but 2 were new after dedup"
