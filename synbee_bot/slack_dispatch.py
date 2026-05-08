"""Slack Block Kit message builder + dispatcher."""
from __future__ import annotations

import sys
from typing import Iterable

from .models import Paper, Verdict


MISSION_NAMES = {
    1: "🧬 Mission 1 — 천연물·효소",
    2: "⚙️ Mission 2 — Genome/RNA tools",
    3: "💊 Mission 3 — Probiotic/Microbiome",
    None: "🔬 General",
}


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def build_paper_blocks(paper: Paper, verdict: Verdict) -> list[dict]:
    """Return a Block Kit block array for one paper."""
    mission_label = MISSION_NAMES.get(verdict.mission, "🔬 General")
    score_emoji = "⭐" * min(5, max(1, (verdict.score + 1) // 2))

    title_block = {
        "type": "header",
        "text": {"type": "plain_text",
                 "text": _truncate(paper.title, 145), "emoji": True},
    }

    meta = f"*{paper.journal}* · {paper.year or ''} · {paper.authors_short(3)}"
    meta_block = {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": meta}],
    }

    score_block = {
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"{mission_label}  {score_emoji} *score {verdict.score}/10*"},
    }

    # Bilingual one-liner summaries
    summary_lines: list[str] = []
    if verdict.one_liner:
        summary_lines.append(f"🇰🇷 _{verdict.one_liner}_")
    if verdict.one_liner_en:
        summary_lines.append(f"🇬🇧 _{verdict.one_liner_en}_")
    summary_block = None
    if summary_lines:
        summary_block = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(summary_lines)},
        }

    abstract_block = None
    if paper.abstract:
        abstract_block = {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "*Abstract*\n> " + _truncate(paper.abstract.replace("\n", " "), 600)},
        }

    actions = {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📄 논문 열기"},
                "url": paper.url,
                "action_id": f"open_{paper.id}",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📥 위키 후보 저장"},
                "value": paper.id,
                "action_id": "queue_for_wiki",
                "style": "primary",
            },
        ],
    }
    if paper.doi:
        actions["elements"].append({
            "type": "button",
            "text": {"type": "plain_text", "text": "🔗 DOI"},
            "url": f"https://doi.org/{paper.doi}",
            "action_id": f"doi_{paper.id}",
        })

    blocks = [title_block, meta_block, score_block]
    if summary_block:
        blocks.append(summary_block)
    if abstract_block:
        blocks.append(abstract_block)
    blocks.append(actions)
    blocks.append({"type": "divider"})
    return blocks


def build_summary_blocks(stats: dict) -> list[dict]:
    """Top-of-digest summary."""
    text = (
        f"*🐝 SynBEE 논문 알림 — {stats.get('date', 'today')}*\n"
        f"수집 {stats.get('collected', 0)}편 → "
        f"중복 제거 후 {stats.get('new', 0)}편 → "
        f"LLM 통과 {stats.get('passed', 0)}편 → "
        f"푸시 {stats.get('posted', 0)}편"
    )
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": text}},
        {"type": "divider"},
    ]


def post_paper(client, channel: str, paper: Paper, verdict: Verdict) -> dict:
    blocks = build_paper_blocks(paper, verdict)
    fallback = f"{paper.title} ({paper.journal})"
    return client.chat_postMessage(
        channel=channel, blocks=blocks, text=fallback,
        unfurl_links=False, unfurl_media=False,
    )


def post_summary(client, channel: str, stats: dict) -> dict:
    blocks = build_summary_blocks(stats)
    return client.chat_postMessage(
        channel=channel, blocks=blocks,
        text=f"SynBEE digest: {stats.get('posted', 0)} papers",
    )


def make_slack_client(token: str):
    try:
        from slack_sdk import WebClient
    except ImportError:
        sys.stderr.write("slack_sdk not installed.\n")
        raise
    return WebClient(token=token)


def post_papers(token: str, channel: str, items: Iterable[tuple[Paper, Verdict]],
                summary: dict | None = None) -> int:
    client = make_slack_client(token)
    posted = 0
    if summary:
        try:
            post_summary(client, channel, summary)
        except Exception as e:
            sys.stderr.write(f"summary post failed: {e}\n")
    for paper, verdict in items:
        try:
            post_paper(client, channel, paper, verdict)
            posted += 1
        except Exception as e:
            sys.stderr.write(f"  ! post failed for {paper.id}: {e}\n")
    return posted
