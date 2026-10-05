"""Paper sources: PubMed (E-utilities), bioRxiv API, RSS feeds."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import Paper
from .prefilter import is_non_article

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from build_query import (  # noqa: E402
    _load_yaml, build_pubmed_query, build_pubmed_journal_query, build_biorxiv_query,
    collect_journals, collect_keywords,
)

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TOOL = "synbee-paper-bot"


class SourceFetchError(RuntimeError):
    """A source could not be fetched completely.

    Raised instead of returning a short list. An empty or truncated result is
    indistinguishable from "nothing was published", so a source that degrades
    quietly drops papers that no later run ever looks for again. The caller
    treats this as a failed source: the run continues on the other sources, and
    this source's watermark is not advanced, so the next run refetches the
    window.
    """


class PartialSourceError(SourceFetchError):
    """Some of a source's sub-feeds failed, but the rest delivered.

    Carries the papers that *did* arrive. Throwing them away with the broken
    feed is what a plain SourceFetchError would do — and RSS feeds keep only
    their latest entries, so papers dropped today may have rolled off the feed
    before the next run looks again. The source still counts as failed (alert,
    watermark held), so the window widens until every feed is healthy.
    """

    def __init__(self, message: str, papers: list[Paper]):
        super().__init__(message)
        self.papers = papers


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------
def _ncbi_post(url: str, params: dict, timeout: int = 60,
               attempts: int = 3, backoff: float = 2.0) -> str:
    """POST to an NCBI E-utility, retrying transient failures.

    E-utilities return 502/503 under load often enough that a single attempt
    costs a whole day of PubMed. Exhausting the retries raises SourceFetchError
    so the caller treats PubMed as failed rather than as empty.
    """
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    params["tool"] = TOOL
    params["email"] = os.environ.get("NCBI_EMAIL", "dosoyang@korea.ac.kr")
    data = urllib.parse.urlencode(params, doseq=True).encode("utf-8")

    last_error = "unknown"
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}"
            if 400 <= e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = str(e) or type(e).__name__
        if attempt < attempts:
            sys.stderr.write(f"  retry {attempt}/{attempts - 1} for NCBI: {last_error}\n")
            time.sleep(backoff * attempt)
    raise SourceFetchError(f"NCBI {url}: {last_error}")


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
            if not paper:
                continue
            # PubMed labels errata and retractions outright, and its erratum
            # records often carry the ORIGINAL paper's title — so the title
            # patterns in prefilter.py cannot catch them. Drop them here, loudly.
            pub_types = {(el.text or "").strip()
                         for el in art.findall(".//PublicationTypeList/PublicationType")}
            if is_non_article(paper.title, pub_types):
                sys.stderr.write(f"  · non-article dropped: {paper.title[:100]}\n")
                continue
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


# Gap keywords the DAILY list misses — added ONLY to the weekly sweep so it
# surfaces relevant papers whose titles/abstracts don't hit the narrower daily net.
WEEKLY_EXTRA_KEYWORDS = [
    "biocatalysis", "biocatalyst", "whole-cell biocatalysis", "enzyme cascade",
    "cell factory", "cell factories", "chassis", "strain engineering",
    "protein evolution", "enzyme evolution", "de novo protein", "protein design",
    "cofactor engineering", "peroxygenase", "halogenase", "cytochrome P450",
    "glycosyltransferase", "methyltransferase", "aminotransferase", "transaminase",
    "decarboxylase", "heterologous expression", "biosynthetic pathway", "bioconversion",
    "genetic code expansion", "noncanonical amino acid", "orthogonal translation",
    "cell-free", "in vitro translation", "RiPP", "lasso peptide", "lanthipeptide",
    "thiopeptide", "siderophore", "adaptive laboratory evolution",
    "DNAzyme", "genetic circuit", "gene circuit", "phage engineering",
    "engineered bacteriophage",
]


def fetch_from_pubmed_weekly(since_days: int, retmax: int = 1500) -> list[Paper]:
    """Weekly sweep: broadened keyword net (daily keywords + WEEKLY_EXTRA_KEYWORDS)
    AND the journal whitelist. Bounded volume; catches relevant papers whose
    titles miss the narrower daily keyword list. Delta vs seen.db removes
    everything the daily bot already collected."""
    journals_yaml = _load_yaml(ROOT / "config" / "journals.yml")
    keywords_yaml = _load_yaml(ROOT / "config" / "keywords.yml")
    constraints = keywords_yaml.get("constraints", {}) or {}
    keywords = collect_keywords(keywords_yaml, include_aux=True)
    keywords["mission"] = sorted(set(keywords["mission"]) | set(WEEKLY_EXTRA_KEYWORDS))
    journals = collect_journals(journals_yaml)
    query = build_pubmed_query(
        journals, keywords,
        search_field=constraints.get("search_field", "tiab"),
        journal_filter=True,
        language_filter=bool(constraints.get("language_filter", True)),
    )
    pmids = pubmed_search_pmids(query, since_days=since_days, retmax=retmax)
    return pubmed_fetch_papers(pmids)


def fetch_from_pubmed_journals_only(since_days: int, retmax: int = 2000) -> list[Paper]:
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
def _fetch_json(url: str, *, timeout: int = 30, attempts: int = 3,
                backoff: float = 2.0) -> dict:
    """GET a JSON document, retrying transient failures.

    Guards every way this call has been seen to fail, not just HTTPError:
    an empty body served with HTTP 200 (the 2026-08-15 bioRxiv outage), an HTML
    holding page, a timeout, or a DNS/connection error.
    """
    last_error = "unknown"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace").strip()
            if not raw:
                raise ValueError("empty response body (HTTP 200)")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}"
            # 4xx other than rate-limiting will not fix itself on retry.
            if 400 <= e.code < 500 and e.code != 429:
                break
        except json.JSONDecodeError as e:
            last_error = f"malformed JSON ({e})"
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            last_error = str(e) or type(e).__name__
        if attempt < attempts:
            sys.stderr.write(f"  retry {attempt}/{attempts - 1} for {url}: {last_error}\n")
            time.sleep(backoff * attempt)
    raise SourceFetchError(f"{url}: {last_error}")


def _reported_total(data: dict) -> int | None:
    """The interval's paper count as the server reports it, if it does.

    bioRxiv sends it as a string (``"total": "338"``), and older payloads omit
    it entirely, so anything unparseable means "the server did not say".
    """
    messages = data.get("messages") or [{}]
    try:
        return int(messages[0].get("total"))
    except (TypeError, ValueError, IndexError, KeyError, AttributeError):
        return None


#: Page budget, sized against the worst case rather than the usual one. A
#: watermark that has fallen MAX_SINCE_DAYS behind asks for a month of
#: preprints — over 4,000 of them — and at 30 per page that is well past a
#: hundred requests. Running out of pages mid-window is treated as a failure,
#: not as the end of the results, so this only has to be generous enough that
#: the guard never fires on a real window.
BIORXIV_MAX_PAGES = 300
EUROPEPMC_MAX_PAGES = 200


def biorxiv_recent(server: str, since_days: int,
                   max_pages: int = BIORXIV_MAX_PAGES) -> list[Paper]:
    """Fetch all bioRxiv/medRxiv papers in date range, paginated.

    Raises SourceFetchError if any page cannot be read. Returning the pages
    collected so far would silently drop everything on the pages behind the
    failure.

    Pagination follows what the server actually served. The page size is the
    server's to choose and it has changed — the API documentation says 30 per
    call where this code once assumed 100 — and `count < 100 → last page` turns
    such a change into a silent truncation at the first page: the run keeps the
    window's first 30 preprints, discards the rest, and looks entirely normal
    doing it.
    """
    end = dt.date.today()
    start = end - dt.timedelta(days=since_days)
    interval = f"{start.isoformat()}/{end.isoformat()}"

    if max_pages <= 0:
        raise SourceFetchError(f"{server}: no page budget to fetch with")

    out: list[Paper] = []
    cursor = 0
    page_size = 0
    for _ in range(max_pages):
        url = f"https://api.biorxiv.org/details/{server}/{interval}/{cursor}/json"
        try:
            data = _fetch_json(url)
        except SourceFetchError as e:
            raise SourceFetchError(
                f"{server} unreachable at cursor {cursor}: {e}") from e
        items = data.get("collection") or []
        for item in items:
            paper = _parse_biorxiv_item(item, server)
            if paper:
                out.append(paper)
        if not items:
            # An empty page is only the end when the server agrees the window
            # is finished. Paginating `/pubs/` end to end on 2026-09-26 (the
            # sibling endpoint that survived the outage, sharing this
            # pagination) returned exactly `total` items with no empty page
            # before it, so `total` is exact and a short stop is a fault, not a
            # quiet day. Treating it as the end is how a half-served window
            # becomes a digest that looks complete.
            total = _reported_total(data)
            if total is not None and cursor < total:
                raise SourceFetchError(
                    f"{server}: empty page at cursor {cursor} of {total} — "
                    "the server stopped serving a window it says has more")
            break
        cursor += len(items)
        total = _reported_total(data)
        if total is not None:
            if cursor >= total:
                break
        elif page_size and len(items) < page_size:
            # No total to go by: a page shorter than the first one is the last.
            break
        page_size = page_size or len(items)
        time.sleep(0.4)
    else:
        # Fell out of the loop with pages still to read. Returning what we have
        # is the silent truncation this function exists to prevent.
        total = _reported_total(data)
        if total is None or cursor < total:
            raise SourceFetchError(
                f"{server}: ran out of pages at cursor {cursor} of "
                f"{total if total is not None else 'unknown'} — window too wide")
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


# ---------------------------------------------------------------------------
# Europe PMC — the stand-in for a dead api.biorxiv.org
#
# 2026-09-24 → 2026-09-26: the `/details/` endpoint answered HTTP 200 with a
# zero-length body for every server, interval, DOI, cursor and format —
# including bioRxiv's own documented example URLs. `format=html` came back
# truncated mid-document with no <body>, i.e. a PHP fatal error during render,
# so with `format=json` nothing had been flushed before the crash. Sibling
# endpoints on the same host (`/pubs/`, `/sum/`) kept working, so the host was
# up and one endpoint was broken — nothing a retry or a different window could
# route around.
#
# `collect_all` isolation kept the other sources alive and the watermark held
# the window open, but bioRxiv returned nothing for as long as the endpoint
# stayed down, and that window is capped at MAX_SINCE_DAYS. Europe PMC indexes
# the same preprints with full abstracts, so it stands in rather than letting a
# long outage run the window out.
# ---------------------------------------------------------------------------
EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

#: Europe PMC's FIRST_PDATE is its own stamp, not bioRxiv's posting date, and
#: the two are much further apart than they look. Measured over 2,254 bioRxiv
#: preprints with FIRST_PDATE in 2026-09-05..09-20, against the posting date
#: bioRxiv encodes in the DOI (``10.64898/YYYY.MM.DD.NNNNNN``): median 4 days,
#: only 78% within 5, 99.6% within 7, with a tail reaching 14.
#:
#: So the fallback must reach back well past the native window it stands in
#: for. A preprint that has not been indexed by the time its window passes is
#: not late, it is lost — no later run looks there again. Three weeks clears
#: the entire observed distribution. The overlap costs pages and nothing else:
#: seen.db dedups before the LLM stage, and this path only runs at all while
#: the native API is down.
EUROPEPMC_LAG_DAYS = 21

_EUROPEPMC_PUBLISHER = {"biorxiv": "bioRxiv", "medrxiv": "medRxiv"}


def _parse_europepmc_item(item: dict, server: str) -> Paper | None:
    doi = (item.get("doi") or "").strip()
    if not doi:
        return None
    authors = [a.get("fullName", "").strip()
               for a in ((item.get("authorList") or {}).get("author") or [])
               if (a.get("fullName") or "").strip()]
    if not authors:
        authors = [a.strip() for a in (item.get("authorString") or "").rstrip(".").split(",")
                   if a.strip()]
    published = (item.get("firstPublicationDate") or "").strip()
    try:
        year = int(item.get("pubYear"))
    except (TypeError, ValueError):
        year = int(published[:4]) if published[:4].isdigit() else None
    # Europe PMC does not carry the preprint's version number, and a hardcoded
    # `v1` would point readers at a superseded draft. The DOI always resolves
    # to the current version.
    return Paper(
        id=f"{server}:{doi}", source=server,
        title=(item.get("title") or "").strip(),
        authors=authors,
        journal=server, year=year,
        abstract=(item.get("abstractText") or "").strip(),
        doi=doi, url=f"https://doi.org/{doi}",
        published=published or None,
    )


def europepmc_preprints(server: str, since_days: int, *, page_size: int = 100,
                        max_pages: int = EUROPEPMC_MAX_PAGES) -> list[Paper]:
    """Fetch a server's preprints from Europe PMC, paginated by cursorMark.

    Raises SourceFetchError if any page cannot be read, for the same reason
    `biorxiv_recent` does: a short list is indistinguishable from a quiet week.
    """
    publisher = _EUROPEPMC_PUBLISHER.get(server, server)
    end = dt.date.today()
    start = end - dt.timedelta(days=since_days + EUROPEPMC_LAG_DAYS)
    query = (f'(SRC:"PPR") AND (PUBLISHER:"{publisher}") AND '
             f'(FIRST_PDATE:[{start.isoformat()} TO {end.isoformat()}])')

    if max_pages <= 0:
        raise SourceFetchError(f"Europe PMC {server}: no page budget to fetch with")

    out: list[Paper] = []
    fetched = 0
    cursor = "*"
    used: set[str] = set()
    for _ in range(max_pages):
        if cursor in used:
            # Europe PMC repeats the cursor instead of clearing it on the last
            # page; without this the loop would refetch it until max_pages.
            break
        used.add(cursor)
        url = EUROPEPMC_SEARCH + "?" + urllib.parse.urlencode({
            "query": query, "format": "json", "resultType": "core",
            "pageSize": page_size, "cursorMark": cursor,
        })
        data = _fetch_json(url)
        results = (data.get("resultList") or {}).get("result") or []
        fetched += len(results)
        for item in results:
            paper = _parse_europepmc_item(item, server)
            if paper:
                out.append(paper)
        if not results:
            # Same rule as the native path: stopping short of the hit count the
            # search itself reported is a fault, not the end of the window.
            # `fetched` counts what the server sent, not what parsed, so a
            # record we skip cannot masquerade as a missing one.
            try:
                hits = int(data.get("hitCount"))
            except (TypeError, ValueError):
                hits = None
            if hits is not None and fetched < hits:
                raise SourceFetchError(
                    f"Europe PMC {server}: empty page at {fetched} of {hits}")
            break
        cursor = (data.get("nextCursorMark") or "").strip()
        if not cursor:
            break
        time.sleep(0.2)
    else:
        raise SourceFetchError(
            f"Europe PMC {server}: ran out of pages at {len(out)} of "
            f"{data.get('hitCount', 'unknown')} — window too wide")
    return out


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
    try:
        raw = biorxiv_recent("biorxiv", since_days)
        if not raw:
            # bioRxiv posts 60+ preprints on a weekend day and ~300 on a
            # weekday, and every window spans at least two calendar days, so a
            # well-formed "no posts found" is the API, not a quiet window
            # (2026-10-03 →: read as quiet, it advanced the watermark over days
            # nobody had seen and never reached the fallback).
            raise SourceFetchError(
                f"bioRxiv API returned no preprints for the last {since_days + 1} "
                "calendar days — an outage, not a quiet window")
    except SourceFetchError as primary:
        # Europe PMC lags bioRxiv by a day or two, so it is a stand-in and not
        # a replacement: only reach for it once the native API has given up.
        sys.stderr.write(f"  ! bioRxiv API unusable ({primary})\n"
                         "    falling back to Europe PMC for this window\n")
        try:
            raw = europepmc_preprints("biorxiv", since_days)
        except SourceFetchError as backup:
            raise SourceFetchError(
                f"bioRxiv API: {primary} || Europe PMC fallback: {backup}"
            ) from primary
        sys.stderr.write(f"  ↪ Europe PMC served {len(raw)} preprints\n")
        # Deliver the stand-in's papers but hold the watermark: Europe PMC
        # indexes days late, and the window the next run reopens from the last
        # native success is what recovers whatever it had not indexed yet.
        raise PartialSourceError(
            f"bioRxiv API unusable ({primary}); Europe PMC stood in with "
            f"{len(raw)} preprints — watermark held until the API recovers",
            filter_biorxiv_by_keywords(raw, keywords["mission"])) from primary
    return filter_biorxiv_by_keywords(raw, keywords["mission"])


# ---------------------------------------------------------------------------
# RSS — feedparser optional. Returns Paper objects with empty abstracts.
# ---------------------------------------------------------------------------
def fetch_from_rss(since_days: int) -> list[Paper]:
    try:
        import feedparser
    except ImportError as e:
        # Returning [] here reads downstream as "no papers today" and would
        # advance the RSS watermark past days nobody ever looked at.
        raise SourceFetchError("feedparser not installed — RSS not collected") from e
    journals_yaml = _load_yaml(ROOT / "config" / "journals.yml")
    feeds = journals_yaml.get("rss_feeds", []) or []
    cutoff = dt.datetime.now() - dt.timedelta(days=since_days)
    out: list[Paper] = []
    broken: list[str] = []
    for feed in feeds:
        try:
            parsed = feedparser.parse(_download_feed(feed["url"]))
        except Exception as e:
            # Skipping the feed keeps the other feeds flowing, but the run must
            # still be told, or this feed's papers vanish one day at a time.
            sys.stderr.write(f"  RSS error {feed['name']}: {e}\n")
            broken.append(f"{feed.get('name', feed.get('url', '?'))}: {e}")
            continue
        problem = _dead_feed_reason(parsed)
        if problem:
            # feedparser does not raise on HTTP errors: a 403 HTML page parses
            # as a feed with zero entries, which reads as "nothing published".
            # Metabolic Engineering's old ScienceDirect URL did exactly that.
            sys.stderr.write(f"  RSS dead feed {feed['name']}: {problem}\n")
            broken.append(f"{feed.get('name', feed.get('url', '?'))}: {problem}")
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
                doi=_rss_entry_doi(entry), url=link,
                published=pub_dt.isoformat() if pub_dt else None,
            ))
    if broken:
        raise PartialSourceError("; ".join(broken), out)
    return out


_FEED_UA = "synbee-paper-bot/1.0 (+https://github.com/synbeelab/synbee-paper-bot)"


def _download_feed(url: str, *, timeout: int = 30) -> bytes:
    """Fetch a feed body through a cookie-keeping session.

    nature.com intermittently bounces feed requests through an idp.nature.com
    cookie handshake (303 → 302 → 302 → feed). feedparser's own fetcher drops
    the cookie and ends on an HTML page that parses as a zero-entry feed, so the
    four Nature-journal feeds silently delivered nothing on those days.
    """
    import requests

    with requests.Session() as session:
        response = session.get(url, timeout=timeout, headers={"User-Agent": _FEED_UA})
    response.raise_for_status()
    return response.content


_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#]+")


def _rss_entry_doi(entry) -> str | None:
    """The entry's DOI, so cross-source dedup can see the paper behind the URL.

    Without it the same bioRxiv preprint arrived as `biorxiv:<doi>` from the API
    and as `rss:<link>` from the subject feed, and was posted twice (HERO,
    PRIME, … on 2026-09-29 – 10-03). Nature/Science put it in `prism:doi`,
    bioRxiv and Cell in `dc:identifier` (with or without a `doi:` prefix).
    """
    for key in ("prism_doi", "dc_identifier"):
        value = entry.get(key)
        if value:
            m = _DOI_RE.search(str(value))
            if m:
                return m.group(0).lower()
    return None


def _dead_feed_reason(parsed) -> str | None:
    """Why a parsed feed should be treated as broken, or None if it is fine.

    HTTP errors are raised by _download_feed; this catches the 200-with-HTML
    case (an error or login page that parses as a zero-entry feed).
    """
    if not parsed.entries and parsed.get("bozo"):
        return f"unparseable feed ({parsed.get('bozo_exception')})"
    return None


# ---------------------------------------------------------------------------
# Orchestrator helper
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CollectResult:
    """What each enabled source returned, and which ones failed.

    `failures` is not decoration: a source missing from `papers` means "we do
    not know what it had", which is a different thing from "it had nothing",
    and only the former must block that source's watermark.
    """
    papers: dict[str, list[Paper]]
    failures: dict[str, str]
    succeeded: set[str]

    @property
    def all_papers(self) -> list[Paper]:
        return [p for group in self.papers.values() for p in group]


def collect_all(since_days_pubmed: int, since_days_biorxiv: int, since_days_rss: int,
                pubmed: bool = True, biorxiv: bool = True,
                rss: bool = True) -> CollectResult:
    """Collect from every enabled source, isolating each one.

    A source that raises is recorded as failed and the rest still run. Before
    this isolation existed, a bioRxiv outage aborted the whole process and threw
    away the PubMed papers already fetched in the same call (run 31849376195).
    """
    papers: dict[str, list[Paper]] = {}
    failures: dict[str, str] = {}
    succeeded: set[str] = set()

    enabled = [
        ("pubmed", pubmed, lambda: fetch_from_pubmed(since_days_pubmed)),
        ("biorxiv", biorxiv, lambda: fetch_from_biorxiv(since_days_biorxiv)),
        ("rss", rss, lambda: fetch_from_rss(since_days_rss)),
    ]
    for name, is_enabled, fetch in enabled:
        if not is_enabled:
            continue
        try:
            papers[name] = fetch()
            succeeded.add(name)
        except PartialSourceError as e:
            # Keep what arrived; still a failure for alerting and watermarks.
            papers[name] = e.papers
            failures[name] = f"{type(e).__name__}: {e}"
            sys.stderr.write(f"  ✗ source '{name}' partially failed: {failures[name]}\n")
        except Exception as e:
            failures[name] = f"{type(e).__name__}: {e}"
            sys.stderr.write(f"  ✗ source '{name}' failed: {failures[name]}\n")

    return CollectResult(papers=papers, failures=failures, succeeded=succeeded)
