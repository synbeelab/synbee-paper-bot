"""Cross-source dedup: one paper, many ids.

The same paper reaches the bot under a different id per route — `pubmed:12345`,
`biorxiv:10.64898/...`, `rss:https://www.biorxiv.org/...`, `doi:10.1038/...` —
so dedup by id alone posts it once per route.

The DOI is the shared key. A few routes carry no DOI at all (ScienceDirect RSS
gives only a PII link), and for those — and only those — the normalized title
stands in. Two records that both have DOIs are never merged on title: a
preprint and its journal version share a title but are different records, and
merging them would hide the publication.

Dropping an exact repeat of a paper that is kept (or was delivered) under
another id cannot cost recall.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping

from .models import Paper

_TAGS = re.compile(r"<[^>]+>")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

#: Shorter titles ("Editorial", "Correction") collide across unrelated papers.
MIN_TITLE_KEY = 30


def title_key(title: str | None) -> str | None:
    """Lower-case alphanumerics, tags stripped; None when too short to trust."""
    key = _NON_ALNUM.sub("", _TAGS.sub("", title or "").lower())
    return key if len(key) >= MIN_TITLE_KEY else None


#: bioRxiv/medRxiv DOI prefixes (old and current). A preprint shares its title
#: with the later journal version, so it must never take part in a title match.
PREPRINT_DOI_PREFIXES = ("10.1101/", "10.64898/")

NONE, JOURNAL, PREPRINT = "none", "journal", "preprint"


def doi_kind(doi: str | None) -> str:
    d = (doi or "").lower()
    if not d:
        return NONE
    return PREPRINT if d.startswith(PREPRINT_DOI_PREFIXES) else JOURNAL


def titles_may_match(a: str, b: str) -> bool:
    """Whether two records of these DOI kinds may be merged on title alone.

    Only when one side has no DOI and the other is not a preprint: the DOI-less
    ScienceDirect RSS item and the PubMed record of the same article. Two DOI'd
    records are decided by DOI; a preprint never matches its journal version.
    """
    return NONE in (a, b) and PREPRINT not in (a, b)


def _doi(p: Paper) -> str:
    return (p.doi or "").lower()


def merge_by_doi(papers: Iterable[Paper], rank: Callable[[Paper], int]) -> list[Paper]:
    """Drop repeats by id, then by DOI (or title, for DOI-less records).

    Keeps the lowest-`rank` copy of each paper — the one the LLM judges best (a
    real abstract beats a bare title). Survivors are ordered by `rank`.
    """
    by_id: dict[str, Paper] = {}
    for p in papers:
        by_id.setdefault(p.id, p)
    ordered = sorted(by_id.values(), key=rank)

    merged: list[Paper] = []
    dois: set[str] = set()
    title_kinds: dict[str, set[str]] = {}   # title_key → DOI kinds kept so far
    for p in ordered:
        doi, key, kind = _doi(p), title_key(p.title), doi_kind(p.doi)
        if doi and doi in dois:
            continue
        if key and any(titles_may_match(kind, k) for k in title_kinds.get(key, ())):
            continue
        if doi:
            dois.add(doi)
        if key:
            title_kinds.setdefault(key, set()).add(kind)
        merged.append(p)
    return merged


def drop_known_dois(papers: list[Paper], known: set[str]) -> list[Paper]:
    """Papers whose DOI is not already in `known` (lower-cased DOIs)."""
    return [p for p in papers if not (p.doi and p.doi.lower() in known)]


def drop_known_titles(papers: list[Paper],
                      seen_titles: Mapping[str, Collection[str]]) -> list[Paper]:
    """Drop papers already delivered under a DOI-less twin (see titles_may_match).

    `seen_titles` maps title_key → the DOI kinds of seen records with that title.
    """
    out = []
    for p in papers:
        key, kind = title_key(p.title), doi_kind(p.doi)
        if key and any(titles_may_match(kind, k) for k in seen_titles.get(key, ())):
            continue
        out.append(p)
    return out
