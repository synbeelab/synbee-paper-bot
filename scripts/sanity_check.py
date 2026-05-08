"""
Sanity-check the generated PubMed query against live NCBI E-utilities.

Reports:
  - hit count over multiple time windows (1d / 7d / 30d / 365d)
  - the actual final query string sent to PubMed
  - per-journal hit counts (if --per-journal)
  - sample titles of the most recent hits

Usage:
    python scripts/sanity_check.py
    python scripts/sanity_check.py --windows 1 7 30
    python scripts/sanity_check.py --per-journal
    python scripts/sanity_check.py --sample 10

Notes:
  - NCBI E-utilities allows 3 req/sec without API key, 10 req/sec with one.
    Set NCBI_API_KEY env var to use the higher rate.
  - This script is read-only. It does not modify anything.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_query import (  # noqa: E402
    _load_yaml,
    build_pubmed_query,
    collect_journals,
    collect_keywords,
)

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
TOOL = "synbee-paper-bot-sanity-check"
EMAIL = os.environ.get("NCBI_EMAIL", "dosoyang@korea.ac.kr")


def _request(url: str, params: dict, retries: int = 3, sleep: float = 0.4) -> str:
    """POST to NCBI E-utilities (handles long query strings; GET caps at ~2KB)."""
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    params["tool"] = TOOL
    params["email"] = EMAIL
    data = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"NCBI request failed: {last_err}") from last_err


def esearch_count(query: str, days_back: int | None = None) -> tuple[int, str]:
    """Return (count, final_translation) for a query, optionally limited to last N days."""
    params: dict = {"db": "pubmed", "term": query, "retmax": 0, "retmode": "xml"}
    if days_back:
        params["reldate"] = days_back
        params["datetype"] = "pdat"
    text = _request(ESEARCH, params)
    root = ET.fromstring(text)
    count_el = root.find("Count")
    count = int(count_el.text) if count_el is not None and count_el.text else 0
    qt_el = root.find("QueryTranslation")
    qt = qt_el.text if qt_el is not None and qt_el.text else ""
    return count, qt


def esearch_recent_ids(query: str, days_back: int, retmax: int = 10) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": retmax,
        "retmode": "xml",
        "reldate": days_back,
        "datetype": "pdat",
        "sort": "pub_date",
    }
    text = _request(ESEARCH, params)
    root = ET.fromstring(text)
    return [el.text for el in root.findall(".//IdList/Id") if el.text]


def esummary_titles(pmids: list[str]) -> list[tuple[str, str, str]]:
    """Return list of (pmid, title, source_journal)."""
    if not pmids:
        return []
    params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
    text = _request(ESUMMARY, params)
    root = ET.fromstring(text)
    out: list[tuple[str, str, str]] = []
    for doc in root.findall("DocSum"):
        pmid_el = doc.find("Id")
        title = ""
        source = ""
        for item in doc.findall("Item"):
            name = item.attrib.get("Name", "")
            if name == "Title":
                title = item.text or ""
            elif name == "Source":
                source = item.text or ""
        if pmid_el is not None:
            out.append((pmid_el.text or "", title, source))
    return out


def per_journal_hits(query_no_journal: str, journals: list[str], days_back: int) -> list[tuple[str, int]]:
    """For each journal, count hits within window."""
    results: list[tuple[str, int]] = []
    for j in journals:
        scoped = f'({query_no_journal}) AND "{j}"[Journal]'
        try:
            count, _ = esearch_count(scoped, days_back=days_back)
        except Exception as e:
            sys.stderr.write(f"  ! {j}: {e}\n")
            count = -1
        results.append((j, count))
        time.sleep(0.35)  # polite — stay under 3 req/sec without key
    results.sort(key=lambda x: -x[1])
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Live sanity check against PubMed")
    ap.add_argument("--windows", type=int, nargs="+", default=[1, 7, 30, 365],
                    help="Day windows to check")
    ap.add_argument("--per-journal", action="store_true",
                    help="Per-journal hit counts (slow: 1 req/journal)")
    ap.add_argument("--sample", type=int, default=5,
                    help="Show N most recent sample titles")
    ap.add_argument("--no-aux", action="store_true")
    ap.add_argument("--no-journal-filter", action="store_true")
    args = ap.parse_args()

    journals_yaml = _load_yaml(ROOT / "config" / "journals.yml")
    keywords_yaml = _load_yaml(ROOT / "config" / "keywords.yml")
    constraints = keywords_yaml.get("constraints", {}) or {}

    journals = collect_journals(journals_yaml)
    keywords = collect_keywords(keywords_yaml, include_aux=not args.no_aux)

    query = build_pubmed_query(
        journals, keywords,
        search_field=constraints.get("search_field", "tiab"),
        journal_filter=not args.no_journal_filter,
        language_filter=bool(constraints.get("language_filter", True)),
    )

    print("=" * 78)
    print(f"Active journals  : {len(journals)}")
    print(f"Mission keywords : {len(keywords['mission'])}")
    print(f"Aux keywords     : {len(keywords['aux'])}")
    print(f"Exclude keywords : {len(keywords['exclude'])}")
    print(f"Final query bytes: {len(query)}")
    print("=" * 78)

    print("\nWindow      Hits")
    print("-" * 30)
    for days in args.windows:
        try:
            count, _ = esearch_count(query, days_back=days)
        except Exception as e:
            print(f"  Last {days:>3}d  ERROR: {e}")
            continue
        print(f"  Last {days:>3}d  {count:>6}")
        time.sleep(0.35)

    if args.sample > 0:
        sample_window = args.windows[0] if args.windows else 1
        # Find a window that has hits
        for w in [args.windows[0], 7, 30, 365]:
            ids = esearch_recent_ids(query, w, retmax=args.sample)
            if ids:
                sample_window = w
                break
        else:
            ids = []
        print(f"\nSample of {len(ids)} most recent (last {sample_window}d):")
        print("-" * 78)
        if ids:
            time.sleep(0.35)
            for pmid, title, source in esummary_titles(ids):
                print(f"  [{pmid}] {source}")
                print(f"          {title[:100]}")
        else:
            print("  (no hits)")

    if args.per_journal and not args.no_journal_filter:
        # Build query without journal clause for per-journal scoping
        kw_only_query = build_pubmed_query(
            journals, keywords,
            search_field=constraints.get("search_field", "tiab"),
            journal_filter=False,
            language_filter=bool(constraints.get("language_filter", True)),
        )
        days = max(args.windows)
        print(f"\nPer-journal hits (last {days}d):")
        print("-" * 50)
        results = per_journal_hits(kw_only_query, journals, days_back=days)
        for j, n in results:
            bar = "#" * min(40, max(0, n))
            print(f"  {j:<32} {n:>5}  {bar}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
