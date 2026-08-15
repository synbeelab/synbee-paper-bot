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


def build_summary_blocks(stats: dict, title: str = "🐝 SynBEE 논문 알림") -> list[dict]:
    """Top-of-digest summary."""
    lines = [
        f"*{title} — {stats.get('date', 'today')}*",
        (
            f"수집 {stats.get('collected', 0)}편 → "
            f"중복 제거 후 {stats.get('new', 0)}편 → "
            f"LLM 통과 {stats.get('passed', 0)}편 → "
            f"푸시 {stats.get('posted', 0)}편"
        ),
    ]
    failed = stats.get("failed", 0)
    if failed:
        lines.append(f"⚠️ Slack 전송 실패 {failed}편 (stderr 확인)")
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "divider"},
    ]


def build_source_alert_blocks(failures: dict[str, str], date: str) -> list[dict]:
    """Warning for sources that could not be collected.

    A dead source makes for a digest that looks perfectly normal and is quietly
    incomplete — and on a day when nothing passes the filter, there is no digest
    at all to attach the warning to. So this goes out as its own message.
    """
    detail = "\n".join(f"• *{name}* — {reason}" for name, reason in sorted(failures.items()))
    text = (
        f"🚨 *논문 수집 실패* — {date}\n"
        f"{detail}\n\n"
        f"_이 소스의 논문은 오늘 다이제스트에 포함되지 않았습니다. "
        f"해당 소스의 수집 기준일은 전진하지 않으므로, 다음 런이 빠진 기간을 다시 훑습니다._"
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "divider"},
    ]


def post_source_alert(token: str, channel: str, failures: dict[str, str],
                      date: str) -> bool:
    """Announce failed sources. Returns True if the alert reached Slack."""
    if not failures:
        return False
    try:
        client = make_slack_client(token)
        client.chat_postMessage(
            channel=channel,
            blocks=build_source_alert_blocks(failures, date),
            text=f"논문 수집 실패: {', '.join(sorted(failures))}",
        )
        return True
    except Exception as e:
        sys.stderr.write(f"source alert post failed: {e}\n")
        return False


def post_paper(client, channel: str, paper: Paper, verdict: Verdict) -> dict:
    blocks = build_paper_blocks(paper, verdict)
    fallback = f"{paper.title} ({paper.journal})"
    return client.chat_postMessage(
        channel=channel, blocks=blocks, text=fallback,
        unfurl_links=False, unfurl_media=False,
    )


def post_summary(client, channel: str, stats: dict, title: str = "🐝 SynBEE 논문 알림") -> dict:
    blocks = build_summary_blocks(stats, title=title)
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
    client = WebClient(token=token)
    # chat.postMessage is rate-limited at roughly 1 msg/sec per channel. Now that
    # the per-run post cap is gone, a busy day or a backfill can exceed that, and
    # a 429 would otherwise surface as a failed post — i.e. a dropped paper.
    try:
        from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
        client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
    except Exception as e:  # older slack_sdk — degrade, don't crash
        sys.stderr.write(f"(rate-limit retry handler unavailable: {e})\n")
    return client


def post_papers(token: str, channel: str, items: Iterable[tuple[Paper, Verdict]],
                summary: dict | None = None, title: str = "🐝 SynBEE 논문 알림",
                ) -> tuple[int, list[tuple[Paper, Verdict]]]:
    """Post per-paper messages, then a final digest summary reflecting the
    actual success/failure counts.

    Returns (posted_count, failed_items). The caller needs the actual failed
    items, not just a count, so it can avoid marking them seen — a paper marked
    seen is never evaluated again, so a failed post would silently lose it.
    """
    client = make_slack_client(token)
    items_list = list(items)
    posted = 0
    failed_items: list[tuple[Paper, Verdict]] = []
    for paper, verdict in items_list:
        try:
            post_paper(client, channel, paper, verdict)
            posted += 1
        except Exception as e:
            failed_items.append((paper, verdict))
            sys.stderr.write(f"  ! post failed for {paper.id}: {e}\n")
    if summary is not None:
        final = dict(summary)
        final["posted"] = posted
        final["failed"] = len(failed_items)
        try:
            post_summary(client, channel, final, title=title)
        except Exception as e:
            sys.stderr.write(f"summary post failed: {e}\n")
    return posted, failed_items
