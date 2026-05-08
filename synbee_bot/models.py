"""Domain models."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Paper:
    id: str                          # canonical: "pubmed:12345" / "biorxiv:10.1101/..."
    source: str                      # "pubmed" | "biorxiv" | "rss"
    title: str
    authors: list[str]
    journal: str
    year: int | None
    abstract: str
    doi: str | None
    url: str
    published: str | None            # ISO date

    def authors_short(self, n: int = 3) -> str:
        if not self.authors:
            return ""
        if len(self.authors) <= n:
            return ", ".join(self.authors)
        return ", ".join(self.authors[:n]) + f" et al."

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verdict:
    verdict: str                     # "YES" | "NO"
    mission: int | None              # 1, 2, 3, or None
    score: int                       # 1-10
    one_liner: str                   # 한국어 한 줄 요약
    one_liner_en: str = ""           # English one-line summary
    raw_response: str = ""
    is_error: bool = False           # True if this verdict came from an SDK/parse failure
    model_used: str = ""             # which model produced this verdict

    @property
    def is_yes(self) -> bool:
        return self.verdict.upper() == "YES"
