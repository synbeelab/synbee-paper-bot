"""Guards the weekly digest against reporting NOTHING.

Silence is not an acceptable output for an alert bot: the reader cannot tell
"no new papers this week" apart from "the workflow is dead". The 2026-07-27
scheduled weekly run succeeded, posted nothing (delta was 0 because a manual run
12 h earlier had already consumed those papers into seen.db), and looked exactly
like a broken bot for a week.

These tests pin the two zero paths, and the rule that the zero path must NOT
skip the seen.db bookkeeping.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot.models import Paper, Verdict  # noqa: E402
from synbee_bot.storage import SeenDB  # noqa: E402


def _load_run_weekly():
    """run_weekly.py is a script, not a package module — load it by path."""
    spec = importlib.util.spec_from_file_location(
        "run_weekly", ROOT / "scripts" / "run_weekly.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def paper(pid: str) -> Paper:
    return Paper(id=pid, source="pubmed", title=f"T-{pid}", authors=["A"],
                 journal="J", year=2026, abstract="abs", doi=None,
                 url=f"https://example.org/{pid}", published="2026-08-08")


class Args:
    """Stand-in for argparse.Namespace."""

    def __init__(self, **kw):
        self.dry_run = kw.get("dry_run", False)
        self.no_slack = kw.get("no_slack", False)
        self.no_llm = kw.get("no_llm", True)
        self.since_days = kw.get("since_days", 7)
        self.limit = kw.get("limit", None)
        self.min_score = kw.get("min_score", None)


class Cfg:
    """Minimal config stub for the zero-summary helper."""

    slack_enabled = True
    slack_bot_token = "xoxb-test"
    weekly_channel = "C0B2QJ179K6"


# --- the zero report itself ---------------------------------------------------

def test_zero_delta_still_posts_a_summary(monkeypatch):
    rw = _load_run_weekly()
    sent: list[dict] = []
    monkeypatch.setattr(rw, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rw, "post_summary",
                        lambda client, channel, stats, title="": sent.append(
                            {"channel": channel, "stats": stats, "title": title}))

    rw._post_zero_summary(Cfg(), Args(), collected=80, new=0, passed=0)

    assert len(sent) == 1, "a zero week must still produce exactly one card"
    assert sent[0]["channel"] == "C0B2QJ179K6"
    assert sent[0]["stats"]["collected"] == 80
    assert sent[0]["stats"]["new"] == 0
    assert sent[0]["stats"]["posted"] == 0
    assert "delta" in sent[0]["title"]


def test_zero_summary_renders_the_real_slack_blocks():
    """The card must actually say 0, not render an empty/None digest."""
    from synbee_bot.slack_dispatch import build_summary_blocks

    blocks = build_summary_blocks(
        {"date": "2026-08-08", "collected": 80, "new": 0, "passed": 0, "posted": 0},
        title="🐝 SynBEE 주간 논문 다이제스트 (delta)")

    text = blocks[0]["text"]["text"]
    assert "2026-08-08" in text
    assert "수집 80편" in text
    assert "푸시 0편" in text


@pytest.mark.parametrize("flag", ["dry_run", "no_slack"])
def test_zero_summary_is_silent_under_dry_run_and_no_slack(monkeypatch, flag):
    rw = _load_run_weekly()
    sent: list[dict] = []
    monkeypatch.setattr(rw, "make_slack_client", lambda token: object())
    monkeypatch.setattr(rw, "post_summary",
                        lambda *a, **kw: sent.append({}))

    rw._post_zero_summary(Cfg(), Args(**{flag: True}),
                          collected=5, new=0, passed=0)

    assert sent == [], f"--{flag} must not post to Slack"


def test_a_failing_zero_report_never_kills_the_run(monkeypatch):
    """The report is a courtesy; it must not raise into the caller."""
    rw = _load_run_weekly()
    monkeypatch.setattr(rw, "make_slack_client", lambda token: object())

    def boom(*a, **kw):
        raise RuntimeError("channel_not_found")

    monkeypatch.setattr(rw, "post_summary", boom)

    rw._post_zero_summary(Cfg(), Args(), collected=5, new=5, passed=0)  # no raise


# --- the zero path must not skip seen.db bookkeeping -------------------------

def test_nothing_passing_still_persists_the_judged_rejects(tmp_path, monkeypatch):
    """If the "nothing passed" branch returned early, every run would re-send the
    same rejected papers to the LLM forever. Rejects were judged — record them.
    """
    rw = _load_run_weekly()

    papers = [paper("p1"), paper("p2")]
    db_path = tmp_path / "seen.db"

    monkeypatch.setattr(rw, "fetch_from_pubmed_weekly",
                        lambda since_days: list(papers))
    monkeypatch.setattr(rw, "SeenDB", lambda _p: SeenDB(db_path))

    class Full(Cfg):
        weekly_enabled = True
        weekly_since_days = 7
        weekly_min_score = 6      # --no-llm gives score 5 → nothing passes
        weekly_max_posts = None
        llm_enabled = False
        seen_db_path = db_path

    monkeypatch.setattr(rw, "load_config", lambda: Full())
    monkeypatch.setattr(sys, "argv", ["run_weekly.py", "--no-llm", "--no-slack"])

    assert rw.main() == 0

    db = SeenDB(db_path)
    assert db.filter_unseen(["p1", "p2"]) == set(), \
        "judged-NO papers must be marked seen, so the branch cannot return early"
    db.close()


# --- channel consolidation guard ---------------------------------------------

PAPERS_DAILY = "C0B2QJ179K6"   # #papers-daily


def test_weekly_posts_to_papers_daily():
    """2026-08-04: two alert channels meant the reader had to remember which one
    they skipped, and that is how a paper gets missed. Keep them unified.

    Note the asymmetry: the DAILY channel is injected from the
    SLACK_DAILY_CHANNEL secret (blank in the template), while the weekly channel
    is hardcoded here — so this can only pin the weekly side. That the secret
    resolves to the same #papers-daily was confirmed by reading the channel.
    """
    from synbee_bot.config import load_config

    cfg = load_config(ROOT / "config" / "config.yml.example")

    assert cfg.weekly_channel == PAPERS_DAILY
    assert cfg.weekly_channel != "C0BKDNLRFAT", "#논문-알림 is retired"
