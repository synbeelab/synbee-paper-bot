"""Crossref ToC sweep — full-issue coverage for journals whose e-mail alerts truncate.

Why this exists
---------------
Two independent holes let papers through:

  * The journal's ToC e-mail is a *partial* list. Measured 2026-08-22:
    Nature Communications shows 12 of 214-296 per week, PNAS shows front matter
    only (15-18 of 84-106 per issue). Reading the mail is not reading the journal.
  * The weekly PubMed sweep is journal AND keyword gated, so a relevant paper
    whose title/abstract misses the keyword list is never fetched at all.

Crossref closes both: it lists every article a journal published in a window,
with no keyword gate. The publisher sites cannot be used for this — pnas.org
answers HTTP 403 to non-browser clients and nature.com bounces through an
idp.nature.com cookie handshake (its RSS too).

Volume is real (Nature Communications alone is ~250/week), so the LLM filter
downstream is what makes the output readable. That is deliberate: the filter is
cheap, a missed paper is not.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .models import Paper
from .sources import SourceFetchError, _load_yaml

ROOT = Path(__file__).resolve().parent.parent
CROSSREF = "https://api.crossref.org/journals/{issn}/works"
MAILTO = "dosoyang@korea.ac.kr"          # Crossref polite pool
ROWS = 1000                              # Crossref hard maximum per page
SOURCE_NAME = "crossref_toc"
SELECT = ",".join([
    "DOI", "title", "author", "container-title", "type", "abstract",
    "published-online", "published-print", "volume", "issue",
])

# JATS markup leaks into Crossref abstracts; the LLM does not need the tags.
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _get(url: str, *, timeout: int = 90, attempts: int = 3) -> dict:
    """GET a Crossref JSON page, retrying transient failures.

    A partial fetch is worse than a failed one: it looks like a quiet week and
    the papers it dropped are never looked for again. So every failure path
    either retries or raises.
    """
    last = "unknown"
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": f"synbee-paper-bot (mailto:{MAILTO})"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace").strip()
            if not raw:
                raise ValueError("empty response body (HTTP 200)")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if 400 <= e.code < 500 and e.code != 429:
                break          # a bad ISSN or filter will not fix itself
        except Exception as e:   # timeout, DNS, connection reset, bad JSON
            last = f"{type(e).__name__}: {e}"
        if attempt < attempts:
            time.sleep(2.0 * attempt)
    raise SourceFetchError(f"crossref fetch failed ({last}): {url}")


def _clean(text: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", text or "")).strip()


def _pub_date(item: dict) -> str | None:
    for key in ("published-online", "published-print"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            y = parts[0] + [1, 1]           # Crossref may give year only
            try:
                return dt.date(int(y[0]), int(y[1]), int(y[2])).isoformat()
            except (ValueError, TypeError):
                return str(y[0])
    return None


def _authors(item: dict) -> list[str]:
    out = []
    for a in item.get("author") or []:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
        if name:
            out.append(name)
    return out


def _to_paper(item: dict, journal_name: str) -> Paper | None:
    doi = (item.get("DOI") or "").lower()
    title = _clean((item.get("title") or [""])[0])
    if not doi or not title:
        return None
    published = _pub_date(item)
    year = None
    if published and len(published) >= 4 and published[:4].isdigit():
        year = int(published[:4])
    return Paper(
        id=f"doi:{doi}",
        source=SOURCE_NAME,
        title=title,
        authors=_authors(item),
        journal=(item.get("container-title") or [journal_name])[0] or journal_name,
        year=year,
        abstract=_clean(item.get("abstract", "")),
        doi=doi,
        url=f"https://doi.org/{doi}",
        published=published,
    )


def fetch_journal_window(issn: str, journal_name: str,
                         date_from: str, date_to: str,
                         datefield: str = "pub") -> list[Paper]:
    """Every journal-article the journal published in [date_from, date_to].

    `datefield` picks which Crossref date the window filters on, because
    publishers deposit different things:

      * "pub"     — best available publication date. Day-granular for Nature
        Portfolio, PNAS and ACS. This is the default.
      * "created" — the date the DOI was first deposited. Required for Elsevier
        (Cell Press / ScienceDirect), which deposits `published-print` at MONTH
        granularity and no `published-online`: Crossref reads 2026-09 as
        2026-09-01, so a mid-month weekly window matches nothing at all.
        Measured 2026-08-24 over 2026-08-14..24 — iScience pub 0 / created 80,
        Cell Reports 0 / 49, Cell 0 / 16.

    `created` is assigned once per DOI, so consecutive windows partition every
    DOI with no gaps. It can over-collect when a publisher re-deposits records;
    seen.db absorbs that, and over-collecting is the safe direction.
    """
    papers: list[Paper] = []
    cursor = "*"
    while True:
        query = urllib.parse.urlencode({
            "filter": (f"from-{datefield}-date:{date_from},"
                       f"until-{datefield}-date:{date_to},type:journal-article"),
            "rows": ROWS, "cursor": cursor, "select": SELECT, "mailto": MAILTO,
        })
        msg = _get(f"{CROSSREF.format(issn=issn)}?{query}")["message"]
        items = msg.get("items", [])
        for it in items:
            p = _to_paper(it, journal_name)
            if p:
                papers.append(p)
        cursor = msg.get("next-cursor")
        total = msg.get("total-results", 0)
        if not items or not cursor or len(papers) >= total:
            return papers
        time.sleep(0.3)


def load_toc_config(path: Path | None = None) -> tuple[list[dict], int]:
    cfg = _load_yaml(path or ROOT / "config" / "toc_journals.yml")
    journals = [j for j in (cfg.get("journals") or []) if j.get("active")]
    return journals, int(cfg.get("overlap_days", 3))


def fetch_toc_sweep(since_days: int, *, today: dt.date | None = None,
                    log=lambda msg: None) -> list[Paper]:
    """Full-coverage sweep over the active journals in config/toc_journals.yml.

    The window is widened by `overlap_days` because Crossref registration lags
    publication by 1-3 days: without the overlap a paper published just before
    the boundary is absent from this run's window and already behind the next
    run's, so no run ever sees it. seen.db removes the duplicate cost.
    """
    journals, overlap = load_toc_config()
    if not journals:
        return []
    today = today or dt.date.today()
    date_to = today.isoformat()
    date_from = (today - dt.timedelta(days=since_days + overlap)).isoformat()

    papers: list[Paper] = []
    failures: list[str] = []
    for j in journals:
        name, issn = j.get("name", j.get("issn", "?")), j["issn"]
        datefield = str(j.get("datefield", "pub"))
        try:
            got = fetch_journal_window(issn, name, date_from, date_to, datefield)
        except SourceFetchError as e:
            # One dead journal must not cost the other journals their week.
            failures.append(f"{name}: {e}")
            log(f"  ! {name} ({issn}) FAILED — {e}")
            continue
        suffix = "" if datefield == "pub" else f" [{datefield}-date]"
        log(f"  {name} ({issn}): {len(got)} papers{suffix}")
        papers.extend(got)

    if failures and len(failures) == len(journals):
        raise SourceFetchError("all ToC journals failed: " + " | ".join(failures))
    if failures:
        sys.stderr.write(f"  ! {len(failures)}/{len(journals)} ToC journals failed\n")
    return papers
