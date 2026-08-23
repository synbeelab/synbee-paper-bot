"""SQLite-backed dedup + wiki queue + per-source collection watermarks."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Paper, Verdict


# A watermark that has fallen far behind must not trigger an unbounded backfill.
MAX_SINCE_DAYS = 30


SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT,
    journal TEXT,
    year INTEGER,
    doi TEXT,
    url TEXT,
    abstract TEXT,
    verdict TEXT,
    score INTEGER,
    mission INTEGER,
    one_liner TEXT,
    one_liner_en TEXT,
    pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_seen_pushed ON seen(pushed_at);
CREATE INDEX IF NOT EXISTS idx_seen_score ON seen(score);
-- 같은 논문이 소스마다 다른 id로 들어온다(pubmed:12345 vs doi:10.1038/...).
-- id만 비교하면 PubMed로 이미 밀어놓은 논문을 Crossref 스윕이 다시 밀어버린다.
CREATE INDEX IF NOT EXISTS idx_seen_doi ON seen(doi);

CREATE TABLE IF NOT EXISTS wiki_queue (
    paper_id TEXT PRIMARY KEY,
    queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    FOREIGN KEY(paper_id) REFERENCES seen(id)
);

-- Last date each source was collected AND delivered end-to-end. The next run
-- widens its window to cover everything since, so a crashed or skipped run
-- heals itself instead of leaving a permanent hole in the record.
CREATE TABLE IF NOT EXISTS source_watermark (
    source TEXT PRIMARY KEY,
    last_success TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for older DBs."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen)").fetchall()}
    if "one_liner_en" not in cols:
        conn.execute("ALTER TABLE seen ADD COLUMN one_liner_en TEXT")
    conn.commit()


class SeenDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        _migrate(self.conn)
        self.conn.commit()

    def has_seen(self, paper_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE id = ?", (paper_id,)
        ).fetchone()
        return row is not None

    def filter_unseen(self, paper_ids: Iterable[str]) -> set[str]:
        ids = list(paper_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT id FROM seen WHERE id IN ({placeholders})", ids
        ).fetchall()
        seen = {r["id"] for r in rows}
        return set(ids) - seen

    def seen_dois(self, dois: Iterable[str]) -> set[str]:
        """Which of these DOIs are already recorded, under any source id.

        Cross-source dedup: the same paper arrives as `pubmed:12345` from the
        E-utilities sweep and as `doi:10.1038/...` from the Crossref ToC sweep.
        Comparing ids alone would post it twice.
        """
        ids = [d.lower() for d in dois if d]
        if not ids:
            return set()
        out: set[str] = set()
        for chunk_start in range(0, len(ids), 400):   # SQLite parameter limit
            chunk = ids[chunk_start:chunk_start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT LOWER(doi) AS d FROM seen WHERE LOWER(doi) IN ({placeholders})",
                chunk,
            ).fetchall()
            out |= {r["d"] for r in rows if r["d"]}
        return out

    def mark_seen(self, paper: Paper, verdict: Verdict | None = None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO seen
               (id, source, title, journal, year, doi, url, abstract,
                verdict, score, mission, one_liner, one_liner_en)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                paper.id, paper.source, paper.title, paper.journal,
                paper.year, paper.doi, paper.url, paper.abstract,
                verdict.verdict if verdict else None,
                verdict.score if verdict else None,
                verdict.mission if verdict else None,
                verdict.one_liner if verdict else None,
                verdict.one_liner_en if verdict else None,
            ),
        )
        self.conn.commit()

    # --- per-source collection watermarks ---------------------------------
    def get_source_watermark(self, source: str) -> dt.date | None:
        """Last date this source was collected and delivered, or None."""
        row = self.conn.execute(
            "SELECT last_success FROM source_watermark WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            return None
        try:
            return dt.date.fromisoformat(row["last_success"])
        except ValueError:
            # Unparseable watermark: treat as no history, which widens the
            # window rather than narrowing it.
            return None

    def mark_source_success(self, source: str, day: dt.date | None = None) -> None:
        """Advance a source's watermark. Call only once the run has actually
        delivered — advancing after collection alone would skip past papers a
        later stage dropped."""
        day = day or dt.date.today()
        self.conn.execute(
            """INSERT INTO source_watermark(source, last_success, updated_at)
               VALUES(?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(source) DO UPDATE SET
                 last_success = excluded.last_success,
                 updated_at = CURRENT_TIMESTAMP""",
            (source, day.isoformat()),
        )
        self.conn.commit()

    def queue_for_wiki(self, paper_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO wiki_queue(paper_id) VALUES(?)", (paper_id,)
        )
        self.conn.commit()

    def list_wiki_queue(self, only_unprocessed: bool = True) -> list[sqlite3.Row]:
        clause = "WHERE processed_at IS NULL" if only_unprocessed else ""
        return list(self.conn.execute(
            f"""SELECT s.* FROM wiki_queue q
                JOIN seen s ON s.id = q.paper_id
                {clause}
                ORDER BY q.queued_at DESC"""
        ).fetchall())

    def mark_wiki_processed(self, paper_id: str) -> None:
        self.conn.execute(
            "UPDATE wiki_queue SET processed_at = CURRENT_TIMESTAMP WHERE paper_id = ?",
            (paper_id,),
        )
        self.conn.commit()

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN verdict='YES' THEN 1 ELSE 0 END) AS yes_n FROM seen"
        ).fetchone()
        return {"total_seen": row["n"], "total_yes": row["yes_n"] or 0}

    def close(self) -> None:
        self.conn.close()


def effective_since_days(
    configured: int,
    last_success: dt.date | None,
    *,
    today: dt.date | None = None,
    max_days: int = MAX_SINCE_DAYS,
) -> int:
    """How far back this source must reach on this run.

    The configured `since_days` assumes every previous run succeeded. When one
    did not — it crashed, the workflow was disabled, the source was down — a
    fixed 1-day window steps straight over the gap and those papers are never
    fetched by any run again. So the window is stretched to cover everything
    since the last confirmed delivery.

    One extra day is always added: papers indexed after yesterday's run but
    before midnight fall in the seam between two adjacent windows. The overlap
    costs nothing — seen.db removes the duplicates before the LLM stage.
    """
    today = today or dt.date.today()
    if last_success is None:
        return configured
    gap = (today - last_success).days
    if gap < 0:  # clock skew / hand-edited DB — never shrink the window
        return configured
    return min(max(configured, gap + 1), max_days)


def split_persist_vs_retry(
    results: Iterable[tuple[Paper, Verdict]],
    *,
    held_back: Iterable[tuple[Paper, Verdict]] = (),
    post_failures: Iterable[tuple[Paper, Verdict]] = (),
) -> tuple[list[tuple[Paper, Verdict]], list[tuple[Paper, Verdict]]]:
    """Split a run's results into (persist, retry).

    Marking a paper seen is permanent — it is excluded from every future run — so
    only papers we are genuinely finished with may be recorded. Three cases must
    be retried instead, because recording them would silently lose the paper:

      * `verdict.is_error` — the LLM never actually judged it (503, parse error,
        whole fallback chain exhausted). It is stored as NO/score=0, which is not
        a real decision.
      * `held_back` — passed the filter but was cut by a post cap.
      * `post_failures` — passed the filter but never reached Slack.

    Rejected papers (a real NO, or a score below the threshold) DO get persisted:
    they were judged, and the decision stands.
    """
    retry_ids = ({p.id for p, _ in held_back}
                 | {p.id for p, _ in post_failures})
    persist: list[tuple[Paper, Verdict]] = []
    retry: list[tuple[Paper, Verdict]] = []
    for paper, verdict in results:
        if verdict.is_error or paper.id in retry_ids:
            retry.append((paper, verdict))
        else:
            persist.append((paper, verdict))
    return persist, retry
