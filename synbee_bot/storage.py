"""SQLite-backed dedup + wiki queue."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Paper, Verdict


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
    pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_seen_pushed ON seen(pushed_at);
CREATE INDEX IF NOT EXISTS idx_seen_score ON seen(score);

CREATE TABLE IF NOT EXISTS wiki_queue (
    paper_id TEXT PRIMARY KEY,
    queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    FOREIGN KEY(paper_id) REFERENCES seen(id)
);
"""


class SeenDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
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

    def mark_seen(self, paper: Paper, verdict: Verdict | None = None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO seen
               (id, source, title, journal, year, doi, url, abstract,
                verdict, score, mission, one_liner)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                paper.id, paper.source, paper.title, paper.journal,
                paper.year, paper.doi, paper.url, paper.abstract,
                verdict.verdict if verdict else None,
                verdict.score if verdict else None,
                verdict.mission if verdict else None,
                verdict.one_liner if verdict else None,
            ),
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
