"""Delivery is tiered by score; nothing is dropped by the tiering.

A full card costs about a third of a screen, so once the filter widens it is
the digest's LENGTH that makes it unreadable, not its relevance. Papers below
`card_min_score` therefore arrive as a compact list instead of a card.

That is a presentation change, so the tests here pin the two properties that
keep it from becoming a recall change: every paper is delivered somewhere, and
a failed message is attributed to exactly the papers it carried — attributing
it to the wrong ones would mark delivered-looking papers seen and lose them for
good.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot import slack_dispatch as sd  # noqa: E402
from synbee_bot.models import Paper, Verdict  # noqa: E402


def paper(pid: str) -> Paper:
    return Paper(id=pid, source="pubmed", title=f"Title {pid}", authors=["A"],
                 journal="Nature Communications", year=2026,
                 abstract="abstract " * 40, doi=None,
                 url=f"https://example.org/{pid}", published="2026-10-18")


def verdict(score: int) -> Verdict:
    return Verdict(verdict="YES", mission=1, score=score,
                   one_liner="한국어 한 문장 요약", one_liner_en="one line")


class FakeClient:
    """Records calls; `fail_on` makes the n-th brief message raise."""

    def __init__(self, fail_brief_index: int | None = None) -> None:
        self.cards: list[str] = []
        self.briefs: list[list[dict]] = []
        self.summaries = 0
        self._fail_brief_index = fail_brief_index

    def chat_postMessage(self, *, channel, blocks=None, text=None, **kw):
        header = blocks[0] if blocks else {}
        if header.get("type") == "header":
            self.cards.append(header["text"]["text"])
        elif header.get("type") == "context":
            idx = len(self.briefs)
            self.briefs.append(blocks)
            if self._fail_brief_index == idx:
                raise RuntimeError("slack is down")
        else:
            self.summaries += 1
        return {"ok": True}


@pytest.fixture
def client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(sd, "make_slack_client", lambda token: c)
    return c


def test_high_scores_get_cards_and_the_rest_get_one_list(client):
    items = [(paper("hi"), verdict(8)), (paper("lo1"), verdict(7)),
             (paper("lo2"), verdict(6))]

    posted, failed = sd.post_papers("t", "C1", items, card_min_score=8)

    assert len(client.cards) == 1
    assert len(client.briefs) == 1
    assert posted == 3, "every paper is delivered, card or line"
    assert failed == []


def test_without_a_threshold_every_paper_still_gets_a_card(client):
    items = [(paper("a"), verdict(8)), (paper("b"), verdict(6))]

    posted, failed = sd.post_papers("t", "C1", items, card_min_score=None)

    assert len(client.cards) == 2
    assert client.briefs == []
    assert (posted, failed) == (2, [])


def test_the_list_carries_title_journal_score_and_the_contribution(client):
    sd.post_papers("t", "C1", [(paper("x"), verdict(7))], card_min_score=8)

    body = client.briefs[0][1]["text"]["text"]
    assert "Title x" in body
    assert "https://example.org/x" in body
    assert "Nature Communications" in body
    assert "*7*" in body
    # The list line says what the paper contributed, not why the bot passed it:
    # `one_liner` is the judgment trace, `one_liner_en` is the contribution.
    assert "one line" in body
    assert "한국어 한 문장 요약" not in body


def test_the_list_falls_back_to_the_korean_line_when_there_is_no_english_one(client):
    """A paper is never listed bare — an empty `one_liner_en` must not blank it."""
    v = Verdict(verdict="YES", mission=1, score=7,
                one_liner="한국어 한 문장 요약", one_liner_en="")

    sd.post_papers("t", "C1", [(paper("x"), v)], card_min_score=8)

    assert "한국어 한 문장 요약" in client.briefs[0][1]["text"]["text"]


def test_a_failed_list_message_returns_exactly_the_papers_it_carried(monkeypatch):
    """The whole point: a paper in a failed message must not be marked seen."""
    c = FakeClient(fail_brief_index=0)
    monkeypatch.setattr(sd, "make_slack_client", lambda token: c)
    # Long summaries force more than one brief message.
    items = []
    for i in range(60):
        p, v = paper(f"p{i:02d}"), verdict(7)
        v = Verdict(verdict="YES", mission=1, score=7,
                    one_liner="요약 " * 40, one_liner_en="en")
        items.append((p, v))

    posted, failed = sd.post_papers("t", "C1", items, card_min_score=8)

    assert len(c.briefs) > 1, "this case needs a multi-message list"
    first_chunk = len(c.briefs[0][1]["text"]["text"].split(sd.BRIEF_SEP))
    assert len(failed) == first_chunk
    assert [p.id for p, _ in failed] == [p.id for p, _ in items[:first_chunk]]
    assert posted == len(items) - first_chunk
    assert posted + len(failed) == len(items), "no paper is unaccounted for"


def test_every_paper_appears_in_exactly_one_message(client):
    items = [(paper(f"p{i:03d}"), verdict(6 + i % 2)) for i in range(120)]

    posted, failed = sd.post_papers("t", "C1", items, card_min_score=8)

    delivered = sum(len(b[1]["text"]["text"].split(sd.BRIEF_SEP))
                    for b in client.briefs)
    assert delivered == len(items), "the list holds all of them"
    assert (posted, failed) == (len(items), [])
