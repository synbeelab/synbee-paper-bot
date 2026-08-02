"""Guards against silently losing a paper.

Marking a paper seen is permanent — it is excluded from every future run — so the
rule is: never persist a paper unless it was genuinely judged AND delivered.
These tests pin that rule down, plus the uncapped-by-default post limit.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot.config import _optional_cap  # noqa: E402
from synbee_bot.models import Paper, Verdict  # noqa: E402
from synbee_bot.storage import split_persist_vs_retry  # noqa: E402


def paper(pid: str) -> Paper:
    return Paper(id=pid, source="pubmed", title=f"T-{pid}", authors=["A"],
                 journal="J", year=2026, abstract="abs", doi=None,
                 url=f"https://example.org/{pid}", published="2026-08-02")


def verdict(score: int = 8, *, yes: bool = True, error: bool = False) -> Verdict:
    return Verdict(verdict="YES" if yes else "NO", mission=1, score=score,
                   one_liner="kr", one_liner_en="en", is_error=error)


# --- the three loss channels ------------------------------------------------

def test_error_verdicts_are_retried_not_persisted():
    """An SDK/parse error is not a judgement — persisting it loses the paper."""
    results = [(paper("p1"), verdict(error=True))]

    persist, retry = split_persist_vs_retry(results)

    assert persist == []
    assert [p.id for p, _ in retry] == ["p1"]


def test_held_back_papers_are_retried_not_persisted():
    kept, cut = (paper("p1"), verdict()), (paper("p2"), verdict())

    persist, retry = split_persist_vs_retry([kept, cut], held_back=[cut])

    assert [p.id for p, _ in persist] == ["p1"]
    assert [p.id for p, _ in retry] == ["p2"]


def test_papers_that_failed_to_post_are_retried_not_persisted():
    ok, bad = (paper("p1"), verdict()), (paper("p2"), verdict())

    persist, retry = split_persist_vs_retry([ok, bad], post_failures=[bad])

    assert [p.id for p, _ in persist] == ["p1"]
    assert [p.id for p, _ in retry] == ["p2"]


# --- what SHOULD be persisted ----------------------------------------------

def test_rejected_papers_are_persisted():
    """A real NO is a decision; re-judging it every day would waste tokens."""
    results = [(paper("p1"), verdict(score=2, yes=False))]

    persist, retry = split_persist_vs_retry(results)

    assert [p.id for p, _ in persist] == ["p1"]
    assert retry == []


def test_low_scoring_yes_below_threshold_is_still_persisted():
    results = [(paper("p1"), verdict(score=3, yes=True))]

    persist, _ = split_persist_vs_retry(results)

    assert [p.id for p, _ in persist] == ["p1"]


def test_delivered_papers_are_persisted():
    results = [(paper("p1"), verdict()), (paper("p2"), verdict())]

    persist, retry = split_persist_vs_retry(results)

    assert {p.id for p, _ in persist} == {"p1", "p2"}
    assert retry == []


def test_every_paper_is_accounted_for_exactly_once():
    """No paper may vanish between the two buckets."""
    a, b, c, d = (paper("a"), verdict()), (paper("b"), verdict(error=True)), \
                 (paper("c"), verdict()), (paper("d"), verdict(score=1, yes=False))
    results = [a, b, c, d]

    persist, retry = split_persist_vs_retry(
        results, held_back=[c], post_failures=[])

    assert len(persist) + len(retry) == len(results)
    assert {p.id for p, _ in persist} | {p.id for p, _ in retry} == {"a", "b", "c", "d"}


def test_a_paper_in_both_held_back_and_post_failures_is_retried_once():
    x = (paper("x"), verdict())

    persist, retry = split_persist_vs_retry([x], held_back=[x], post_failures=[x])

    assert persist == []
    assert len(retry) == 1


def test_empty_results_is_not_an_error():
    assert split_persist_vs_retry([]) == ([], [])


# --- post cap: uncapped by default -----------------------------------------

@pytest.mark.parametrize("raw", [None, 0, -1, "", "abc", "0"])
def test_absent_zero_or_junk_means_no_cap(raw):
    assert _optional_cap(raw) is None


@pytest.mark.parametrize("raw,expected", [(15, 15), ("25", 25), (1, 1)])
def test_positive_values_are_honoured_as_caps(raw, expected):
    assert _optional_cap(raw) == expected


def test_shipped_config_template_has_no_post_caps():
    """Regression guard: the template must not reintroduce a silent truncation."""
    from synbee_bot.config import load_config

    cfg = load_config(ROOT / "config" / "config.yml.example")

    assert cfg.slack_max_posts is None
    assert cfg.weekly_max_posts is None


# --- post_papers must report WHICH papers failed, not just how many ----------

def test_post_papers_returns_the_failed_items(monkeypatch):
    """run_daily needs the failed items to keep them out of seen.db."""
    from synbee_bot import slack_dispatch as sd

    sent: list[str] = []

    class FakeClient:
        def chat_postMessage(self, **kw):
            text = kw.get("text", "")
            if "T-p2" in text:
                raise RuntimeError("ratelimited")
            sent.append(text)
            return {"ok": True}

    monkeypatch.setattr(sd, "make_slack_client", lambda token: FakeClient())

    items = [(paper("p1"), verdict()), (paper("p2"), verdict()),
             (paper("p3"), verdict())]

    posted, failed = sd.post_papers("tok", "C123", items)

    assert posted == 2
    assert [p.id for p, _ in failed] == ["p2"]


def test_post_papers_summary_failure_does_not_lose_successes(monkeypatch):
    from synbee_bot import slack_dispatch as sd

    class FakeClient:
        def chat_postMessage(self, **kw):
            if "SynBEE digest" in kw.get("text", ""):
                raise RuntimeError("summary boom")
            return {"ok": True}

    monkeypatch.setattr(sd, "make_slack_client", lambda token: FakeClient())

    posted, failed = sd.post_papers("tok", "C123", [(paper("p1"), verdict())],
                                    summary={"date": "2026-08-02"})

    assert posted == 1
    assert failed == []


# --- integration: a retried paper really does come back next run -------------

def test_retried_papers_are_picked_up_again_by_the_next_run(tmp_path):
    """The whole point: an undelivered paper must reappear, a delivered one must not."""
    from synbee_bot.storage import SeenDB

    db = SeenDB(tmp_path / "seen.db")

    delivered = (paper("ok"), verdict())
    errored = (paper("err"), verdict(error=True))
    unposted = (paper("nopost"), verdict())
    rejected = (paper("no"), verdict(score=1, yes=False))
    results = [delivered, errored, unposted, rejected]

    persist, retry = split_persist_vs_retry(results, post_failures=[unposted])
    for p, v in persist:
        db.mark_seen(p, v)

    # Next run sees the same four papers again from the sources.
    unseen = db.filter_unseen(p.id for p, _ in results)

    assert unseen == {"err", "nopost"}, "undelivered papers must be re-evaluated"
    assert "ok" not in unseen, "delivered paper must not be re-posted"
    assert "no" not in unseen, "judged rejection must not be re-judged"
    db.close()


def test_a_held_back_paper_survives_two_consecutive_capped_runs(tmp_path):
    """Cap of 1 over 3 papers: all 3 must eventually be delivered, none lost."""
    from synbee_bot.storage import SeenDB

    db = SeenDB(tmp_path / "seen.db")
    all_items = [(paper("a"), verdict(score=9)), (paper("b"), verdict(score=8)),
                 (paper("c"), verdict(score=7))]
    delivered: list[str] = []
    cap = 1

    for _ in range(3):
        unseen = db.filter_unseen(p.id for p, _ in all_items)
        pending = [(p, v) for p, v in all_items if p.id in unseen]
        if not pending:
            break
        posting, held = pending[:cap], pending[cap:]
        delivered.extend(p.id for p, _ in posting)
        persist, _ = split_persist_vs_retry(pending, held_back=held)
        for p, v in persist:
            db.mark_seen(p, v)

    assert sorted(delivered) == ["a", "b", "c"], "every paper delivered exactly once"
    assert len(delivered) == len(set(delivered)), "no duplicates"
    db.close()
