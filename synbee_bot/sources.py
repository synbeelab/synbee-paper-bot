"""Paper sources: PubMed (E-utilities), bioRxiv API, RSS feeds."""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

from .models import Paper

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from build_query import (  # noqa: E402
    _load_yaml, build_pubmed_query, build_pubmed_journal_query, build_biorxiv_query,
    collect_journals, collect_keywords,
)

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TOOL = "synbee-paper-bot"


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------
def _ncbi_post(url: str, params: dict, timeout: int = 60) -> str:
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    params["tool"] = TOOL
    params["email"] = os.environ.get("NCBI_EMAIL", "dosoyang@korea.ac.kr")
    data = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def pubmed_search_pmids(query: str, since_days: int, retmax: int = 500) -> list[str]:
    params = {
        "db": "pubmed", "term": query, "retmax": retmax,
        "retmode": "xml", "reldate": since_days,
        "datetype": "pdat", "sort": "pub_date",
    }
    text = _ncbi_post(ESEARCH, params)
    root = ET.fromstring(text)
    return [el.text for el in root.findall(".//IdList/Id") if el.text]


def pubmed_fetch_papers(pmids: list[str]) -> list[Paper]:
    """Batch-fetch full Medline records (handles up to ~200 at a time)."""
    if not pmids:
        return []
    out: list[Paper] = []
    for i in range(0, len(pmids), 200):
        batch = pmids[i:i+200]
        text = _ncbi_post(EFETCH, {
            "db": "pubmed", "id": ",".join(batch),
            "retmode": "xml", "rettype": "abstract",
        })
        root = ET.fromstring(text)
        for art in root.findall(".//PubmedArticle"):
            paper = _parse_pubmed_article(art)
            if paper:
                out.append(paper)
        time.sleep(0.35)
    return out


def _parse_pubmed_article(art: ET.Element) -> Paper | None:
    pmid_el = art.find(".//PMID")
    if pmid_el is None or not pmid_el.text:
        return None
    pmid = pmid_el.text.strip()

    title_el = art.find(".//ArticleTitle")
    title = "".join(title_el.itertext()).strip() if title_el is not None else ""

    # Abstract — concatenate all AbstractText (may be structured)
    abs_parts: list[str] = []
    for ab in art.findall(".//Abstract/AbstractText"):
        label = ab.attrib.get("Label")
        text = "".join(ab.itertext()).strip()
        if label:
            abs_parts.append(f"{label}: {text}")
        else:
            abs_parts.append(text)
    abstract = "\n".join(p for p in abs_parts if p)

    # Authors
    authors: list[str] = []
    for au in art.findall(".//AuthorList/Author"):
        last = au.findtext("LastName") or ""
        init = au.findtext("Initials") or ""
        col = au.findtext("CollectiveName") or ""
        if last or init:
            authors.append(f"{last} {init}".strip())
        elif col:
            authors.append(col)

    journal = (
        art.findtext(".//Journal/ISOAbbreviation")
        or art.findtext(".//Journal/Title")
        or ""
    )
    year_el = art.find(".//Journal/JournalIssue/PubDate/Year")
    year = int(year_el.text) if year_el is not None and year_el.text and year_el.text.isdigit() else None

    doi = None
    for aid in art.findall(".//ArticleId"):
        if aid.attrib.get("IdType") == "doi" and aid.text:
            doi = aid.text.strip()
            break

    # Published date — best effort
    pub_date_el = art.find(".//PubMedPubDate[@PubStatus='pubmed']")
    if pub_date_el is not None:
        y = pub_date_el.findtext("Year")
        m = pub_date_el.findtext("Month")
        d = pub_date_el.findtext("Day")
        published = "-".join(x for x in [y, m, d] if x and x.isdigit())
    else:
        published = str(year) if year else None

    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    return Paper(
        id=f"pubmed:{pmid}", source="pubmed", title=title, authors=authors,
        journal=journal, year=year, abstract=abstract, doi=doi,
        url=url, published=published,
    )


def fetch_from_pubmed(since_days: int) -> list[Paper]:
    """Top-level: read YAML config, build query, fetch papers."""
    journals_yaml = _load_yaml(ROOT / "config" / "journals.yml")
    keywords_yaml = _load_yaml(ROOT / "config" / "keywords.yml")
    constraints = keywords_yaml.get("constraints", {}) or {}
    journals = collect_journals(journals_yaml)
    keywords = collect_keywords(keywords_yaml, include_aux=True)
    query = build_pubmed_query(
        journals, keywords,
        search_field=constraints.get("search_field", "tiab"),
        journal_filter=True,
        language_filter=bool(constraints.get("language_filter", True)),
    )
    pmids = pubmed_search_pmids(query, since_days=since_days)
    return pubmed_fetch_papers(pmids)


def fetch_from_pubmed_journals_only(since_days: int, retmax: int = 1000) -> list[Paper]:
    """Weekly sweep: ALL papers in the whitelisted journals in the window,
    no keyword gate. LLM filter downstream decides relevance."""
    journals_yaml = _load_yaml(ROOT / "config" / "journals.yml")
    keywords_yaml = _load_yaml(ROOT / "config" / "keywords.yml")
    constraints = keywords_yaml.get("constraints", {}) or {}
    keywords = collect_keywords(keywords_yaml, include_aux=True)
    journals = collect_journals(journals_yaml)
    query = build_pubmed_journal_query(
        journals, exclude=keywords["exclude"],
        language_filter=bool(constraints.get("language_filter", True)),
    )
    pmids = pubmed_search_pmids(query, since_days=since_days, retmax=retmax)
    return pubmed_fetch_papers(pmids)


# ---------------------------------------------------------------------------
# bioRxiv  — JSON API
# https://api.biorxiv.org/details/biorxiv/{interval}/{cursor}/{format}
# ---------------------------------------------------------------------------
import json
import urllib.error


def biorxiv_recent(server: str, since_days: int, max_pages: int = 20) -> list[Paper]:
    """Fetch all bioRxiv/medRxiv papers in date range, paginated."""
    end = dt.date.today()
    start = end - dt.timedelta(days=since_days)
    interval = f"{start.isoformat()}/{end.isoformat()}"

    out: list[Paper] = []
    cursor = 0
    for _ in range(max_pages):
        url = f"https://api.biorxiv.org/details/{server}/{interval}/{cursor}/json"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"  bioRxiv HTTP {e.code} at cursor {cursor}\n")
            break
        for item in data.get("collection", []):
            paper = _parse_biorxiv_item(item, server)
            if paper:
                out.append(paper)
        if data.get("messages", [{}])[0].get("count", 0) < 100:
            break
        cursor += 100
        time.sleep(0.4)
    return out


def _parse_biorxiv_item(item: dict, server: str) -> Paper | None:
    doi = item.get("doi")
    if not doi:
        return None
    title = (item.get("title") or "").strip()
    abstract = (item.get("abstract") or "").strip()
    authors_raw = item.get("authors") or ""
    authors = [a.strip() for a in re.split(r"[;,]", authors_raw) if a.strip()]
    published = item.get("date")
    year = None
    if published and len(published) >= 4 and published[:4].isdigit():
        year = int(published[:4])
    url = f"https://www.biorxiv.org/content/{doi}v{item.get('version', 1)}"
    if server == "medrxiv":
        url = f"https://www.medrxiv.org/content/{doi}v{item.get('version', 1)}"
    return Paper(
        id=f"{server}:{doi}", source=server,
        title=title, authors=authors,
        journal=server, year=year, abstract=abstract,
        doi=doi, url=url, published=published,
    )


def filter_biorxiv_by_keywords(papers: list[Paper], keywords: Iterable[str]) -> list[Paper]:
    """Client-side keyword filter — bioRxiv API doesn't support boolean queries."""
    pats = [re.compile(re.escape(k), re.I) for k in keywords]
    out: list[Paper] = []
    for p in papers:
        text = f"{p.title}\n{p.abstract}"
        if any(pat.search(text) for pat in pats):
            out.append(p)
    return out


def fetch_from_biorxiv(since_days: int) -> list[Paper]:
    keywords_yaml = _load_yaml(ROOT / "config" / "keywords.yml")
    keywords = collect_keywords(keywords_yaml, include_aux=False)
    raw = biorxiv_recent("biorxiv", since_days)
    return filter_biorxiv_by_keywords(raw, keywords["mission"])


# ---------------------------------------------------------------------------
# RSS — feedparser optional. Returns Paper objects with empty abstracts.
# ---------------------------------------------------------------------------
def fetch_from_rss(since_days: int) -> list[Paper]:
    try:
        import feedparser
    except ImportError:
        sys.stderr.write("feedparser not installed — skipping RSS\n")
        return []
    journals_yaml = _load_yaml(ROOT / "config" / "journals.yml")
    feeds = journals_yaml.get("rss_feeds", []) or []
    cutoff = dt.datetime.now() - dt.timedelta(days=since_days)
    out: list[Paper] = []
    for feed in feeds:
        try:
            parsed = feedparser.parse(feed["url"])
        except Exception as e:
            sys.stderr.write(f"  RSS error {feed['name']}: {e}\n")
            continue
        for entry in parsed.entries:
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub:
                pub_dt = dt.datetime(*pub[:6])
                if pub_dt < cutoff:
                    continue
            else:
                pub_dt = None
            link = entry.get("link", "")
            entry_id = entry.get("id") or link
            if not entry_id:
                continue
            out.append(Paper(
                id=f"rss:{entry_id}", source="rss",
                title=entry.get("title", ""),
                authors=[a.get("name", "") for a in entry.get("authors", []) if a.get("name")],
                journal=feed.get("name", ""),
                year=pub_dt.year if pub_dt else None,
                abstract=entry.get("summary", "") or entry.get("description", ""),
                doi=None, url=link,
                published=pub_dt.isoformat() if pub_dt else None,
            ))
    return out


# ---------------------------------------------------------------------------
# Orchestrator helper
# ---------------------------------------------------------------------------
def collect_all(since_days_pubmed: int, since_days_biorxiv: int, since_days_rss: int,
                pubmed: bool = True, biorxiv: bool = True, rss: bool = True) -> dict[str, list[Paper]]:
    out: dict[str, list[Paper]] = {}
    if pubmed:
        out["pubmed"] = fetch_from_pubmed(since_days_pubmed)
    if biorxiv:
        out["biorxiv"] = fetch_from_biorxiv(since_days_biorxiv)
    if rss:
        out["rss"] = fetch_from_rss(since_days_rss)
    return out
