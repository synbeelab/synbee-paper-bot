"""Abstract backfill — the filter cannot judge what the source never sent.

Measured 2026-08-30 over one full week of the Crossref ToC sweep (848 papers):
**498 of them (58.7%) reached the LLM with no abstract at all**, so the prompt
carried `(abstract unavailable)` and the verdict rested on the title alone.

  iScience, Cell Reports, Cell, Molecular Cell, Cell Host & Microbe,
  Cell Chemical Biology, Cell Systems, Trends in Biotechnology / Microbiology
  / Chemistry ...... 220 papers, 100% missing. Elsevier deposits no abstract
                     to Crossref at all.
  Nature Communications ... 255 of 414 missing (61.6%)
  PNAS .................... 15 of 118 missing (12.7%)
  JACS / JACS Au / J Nat Prod / Biochemistry ... 0% missing

The daily bot has the same hole from a different direction: the RSS source
returns entries with no summary at all (see sources.py).

Europe PMC carries the abstract for most of them and is free and unauthenticated.
Spot-checked over 21 of the missing DOIs: 13 recovered (Cell Press near-complete;
Nature Communications and iScience miss only because PMC has not indexed them
yet — a later run picks them up, which is why this never marks anything seen).

Cost note: this ADDS input tokens. That is the right direction. Input is ~7% of
what a filter call costs (the 1,384 thinking tokens dominate), so filling 59% of
the papers back in moves the bill by pennies and moves recall by a lot.

Failure policy: every lookup is best-effort. A paper whose abstract cannot be
recovered keeps its empty abstract and goes to the filter exactly as it does
today — this step can only add information, never drop a paper.
"""
from __future__ import annotations

import dataclasses
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import Paper

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Shorter than this is a stub ("Abstract", a copyright line), not an abstract.
# Same threshold the measurement above used, so the numbers stay comparable.
MIN_ABSTRACT_CHARS = 40

# DOIs per Europe PMC query. 25 keeps the OR-query URL near 1.5 kB — well inside
# any gateway limit — and turns ~500 lookups into ~20 requests.
BATCH_SIZE = 25

# Europe PMC asks for courtesy, not a key. One request every 200 ms is far under
# their published guidance and keeps a 20-request backfill under 5 seconds.
SLEEP_BETWEEN_BATCHES = 0.2

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Europe PMC returns HTML-ish markup inside abstractText."""
    return _WS.sub(" ", _TAG.sub(" ", text or "")).strip()


def needs_abstract(paper: Paper) -> bool:
    """True when the filter would see `(abstract unavailable)` for this paper."""
    if not paper.doi:
        return False
    return len((paper.abstract or "").strip()) < MIN_ABSTRACT_CHARS


def _lookup(dois: list[str], *, timeout: int) -> dict[str, str]:
    """Map lowercase DOI -> abstract for whichever of `dois` Europe PMC knows."""
    query = " OR ".join(f'DOI:"{d}"' for d in dois)
    url = EUROPEPMC + "?" + urllib.parse.urlencode({
        "query": query,
        "resultType": "core",
        "format": "json",
        "pageSize": max(len(dois), 25),
    })
    req = urllib.request.Request(url, headers={"User-Agent": "synbee-paper-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)

    out: dict[str, str] = {}
    for item in payload.get("resultList", {}).get("result", []) or []:
        doi = (item.get("doi") or "").strip().lower()
        abstract = _clean(item.get("abstractText") or "")
        if doi and len(abstract) >= MIN_ABSTRACT_CHARS:
            out[doi] = abstract
    return out


def backfill_abstracts(papers: list[Paper], *, timeout: int = 30,
                       batch_size: int = BATCH_SIZE,
                       log=lambda msg: None) -> list[Paper]:
    """Return `papers` with missing abstracts filled in from Europe PMC.

    Never raises and never drops: a batch that fails leaves its papers exactly
    as they arrived, and the run continues. Returns new Paper objects rather
    than mutating the inputs.
    """
    wanted = [p for p in papers if needs_abstract(p)]
    if not wanted:
        return list(papers)

    by_doi = {p.doi.strip().lower(): p for p in wanted if p.doi}
    dois = list(by_doi)
    log(f"Abstract backfill: {len(dois)} of {len(papers)} papers have no abstract…")

    found: dict[str, str] = {}
    failures = 0
    for i in range(0, len(dois), batch_size):
        chunk = dois[i:i + batch_size]
        try:
            found.update(_lookup(chunk, timeout=timeout))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            # One dead batch must not cost the other batches their abstracts,
            # and must not cost anyone their paper.
            failures += 1
            sys.stderr.write(f"  ! Europe PMC batch failed ({type(e).__name__}: {e}); "
                             f"{len(chunk)} papers keep their empty abstract\n")
        if i + batch_size < len(dois):
            time.sleep(SLEEP_BETWEEN_BATCHES)

    if not found:
        log(f"  recovered 0 abstracts ({failures} batch failures)"
            if failures else "  recovered 0 abstracts")
        return list(papers)

    out = [
        dataclasses.replace(p, abstract=found[p.doi.strip().lower()])
        if (needs_abstract(p) and p.doi and p.doi.strip().lower() in found)
        else p
        for p in papers
    ]
    still_missing = len(dois) - len(found)
    suffix = f", {failures} batch failures" if failures else ""
    log(f"  recovered {len(found)} abstracts, {still_missing} still title-only"
        f"{suffix}")
    return out
