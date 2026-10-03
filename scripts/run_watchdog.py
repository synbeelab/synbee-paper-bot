#!/usr/bin/env python3
"""Daily health check of the paper pipelines — see synbee_bot/watchdog.py.

Env:
  GITHUB_TOKEN, GITHUB_REPOSITORY   GitHub API (actions: write to repair)
  SLACK_BOT_TOKEN, WATCHDOG_CHANNEL Slack report (only when something is wrong)
  SEEN_DB                           path to the restored seen.db (optional)
  MIN_SCORE                         post threshold, to tell posted rows (default 6)
  WATCHDOG_DRY_RUN=true             report only: no repair, no Slack

Exit code is 0 even when problems were found — the report is the signal, and
a red watchdog run would only add a GitHub e-mail on top of the Slack message.
It is non-zero only when the watchdog itself could not do its job.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot.catchup import kst_day  # noqa: E402
from synbee_bot.watchdog import (  # noqa: E402
    AUTO_DISABLED, PIPELINES, Report, delivery_verdict, find_duplicate_posts,
    render, silent_feeds, volume_drops,
)

API = "https://api.github.com"
DAILY_SOURCES = ("pubmed", "biorxiv", "rss")


def _gh(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "synbee-paper-bot-watchdog"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _work_job_ran(repo: str, token: str, run_id: int, job_name: str) -> bool:
    jobs = _gh("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs", token)["jobs"]
    return any(j["name"] == job_name and j.get("conclusion") == "success" for j in jobs)


def check_workflows(repo: str, token: str, report: Report, *, dry_run: bool) -> None:
    for wf in _gh("GET", f"/repos/{repo}/actions/workflows?per_page=100", token)["workflows"]:
        if wf["state"] != AUTO_DISABLED:
            continue
        name = wf["path"].rsplit("/", 1)[-1]
        if dry_run:
            report.problems.append(f"{name}: GitHub이 60일 무활동으로 비활성화함 (dry-run, 재활성화 안 함)")
            continue
        _gh("PUT", f"/repos/{repo}/actions/workflows/{wf['id']}/enable", token)
        report.actions.append(f"{name}: 60일 무활동 자동 비활성화 → 재활성화함")


def check_deliveries(repo: str, token: str, db: sqlite3.Connection | None,
                     report: Report, *, now: datetime, dry_run: bool) -> None:
    watermarks = _watermarks(db)
    for pipe in PIPELINES:
        runs = _gh("GET", f"/repos/{repo}/actions/workflows/{pipe.workflow_file}/runs"
                          "?per_page=20", token)["workflow_runs"]
        today = kst_day(now)
        ran = {r["id"]: _work_job_ran(repo, token, r["id"], pipe.work_job)
               for r in runs
               if r.get("conclusion") == "success" and kst_day(_ts(r["created_at"])) == today}
        verdict = delivery_verdict(pipe, runs, ran, watermarks, now=now)
        if verdict.status in ("ok", "not_due"):
            continue
        if verdict.status == "in_progress":
            report.notes.append(f"{pipe.workflow_file}: 아직 실행 중 — 다음 점검에서 확인")
            continue
        what = ("오늘 배달 없음" if verdict.status == "missing"
                else "일부 소스 미수집")
        if verdict.should_dispatch and not dry_run:
            _gh("POST", f"/repos/{repo}/actions/workflows/{pipe.workflow_file}/dispatches",
                token, {"ref": os.environ.get("GITHUB_REF_NAME") or "main"})
            report.actions.append(
                f"{pipe.workflow_file}: {what} ({verdict.detail}) → 재실행 dispatch함 "
                "(seen.db dedup으로 빠진 것만 게시됨)")
        elif verdict.should_dispatch:
            report.problems.append(f"{pipe.workflow_file}: {what} ({verdict.detail}) — dry-run")
        else:
            report.notes.append(
                f"{pipe.workflow_file}: 장기 장애 지속 중 ({verdict.detail}) — "
                "재실행해도 소용없어 생략. 워터마크가 30일까지 창을 넓혀 회수함")


def check_content(db: sqlite3.Connection | None, report: Report, *,
                  today_utc: date, min_score: int) -> None:
    if db is None:
        report.problems.append("seen.db 캐시를 복원하지 못해 내용 점검(중복·수집량)을 건너뜀")
        return
    since = (today_utc - timedelta(days=20)).isoformat()
    rows = [dict(r) for r in db.execute(
        "SELECT id, source, title, journal, doi, verdict, score, date(pushed_at) AS d "
        "FROM seen WHERE date(pushed_at) >= ?", (since,))]

    posted = [r for r in rows
              if (r["verdict"] or "").upper() == "YES" and (r["score"] or 0) >= min_score]
    recent = (today_utc - timedelta(days=1)).isoformat()
    for group in find_duplicate_posts(posted):
        if max(m["d"] for m in group) < recent:
            continue  # reported on an earlier day
        sample = (group[0]["title"] or "")[:70]
        ids = ", ".join(sorted(m["id"].split(":", 1)[0] for m in group))
        report.problems.append(f"중복 게시: '{sample}…' ({ids})")

    by_source: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    by_feed: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        d = date.fromisoformat(r["d"])
        if r["source"] in DAILY_SOURCES:
            by_source[r["source"]][d] += 1
        if r["source"] == "rss":
            by_feed[r["journal"] or "?"][d] += 1
    report.problems += [f"수집량 급감 — {f}" for f in volume_drops(by_source, today=today_utc)]
    report.problems += silent_feeds(by_feed, today=today_utc)


def _watermarks(db: sqlite3.Connection | None) -> dict[str, date | None]:
    if db is None:
        return {}
    out: dict[str, date | None] = {}
    for row in db.execute("SELECT source, last_success FROM source_watermark"):
        try:
            out[row["source"]] = date.fromisoformat(row["last_success"])
        except ValueError:
            out[row["source"]] = None
    return out


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _open_db(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _post_slack(text: str) -> None:
    token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("WATCHDOG_CHANNEL")
    if not (token and channel):
        print("  ! Slack not configured; report printed only")
        return
    from synbee_bot.slack_dispatch import make_slack_client
    make_slack_client(token).chat_postMessage(channel=channel, text=text)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not (token and repo):
        print("GITHUB_TOKEN / GITHUB_REPOSITORY missing", file=sys.stderr)
        return 2
    dry_run = os.environ.get("WATCHDOG_DRY_RUN", "").lower() == "true"
    min_score = int(os.environ.get("MIN_SCORE") or 6)
    now = datetime.now(timezone.utc)
    db = _open_db(Path(os.environ.get("SEEN_DB") or ROOT / "data" / "seen.db"))

    report = Report()
    # Each check is isolated: one failing API call must not hide the others.
    for name, check in (
        ("workflows", lambda: check_workflows(repo, token, report, dry_run=dry_run)),
        ("deliveries", lambda: check_deliveries(repo, token, db, report,
                                                now=now, dry_run=dry_run)),
        ("content", lambda: check_content(db, report, today_utc=now.date(),
                                          min_score=min_score)),
    ):
        try:
            check()
        except Exception as e:  # noqa: BLE001 — report and keep checking
            report.problems.append(f"watchdog '{name}' 점검 자체가 실패: {type(e).__name__}: {e}")

    text = render(report, today=kst_day(now))
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(text + "\n", encoding="utf-8")
    if report.needs_attention and not dry_run:
        _post_slack(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
