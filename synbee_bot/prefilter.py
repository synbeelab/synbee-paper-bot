"""Drop items that are typographically incapable of being a paper.

The Crossref sweep already asks for `type:journal-article`, but publishers file
corrections and retraction notices under that type too. Measured 2026-08-30 over
one week of the sweep: 18 of 848 items (2.1%) were non-articles.

Spending a filter call on "Author Correction: ..." buys nothing — the verdict is
NO before the model reads it — so this is free money. It is also the only place
in this bot where papers are dropped before the LLM sees them, which is why the
pattern list stays deliberately narrow and why every drop is logged by title.

SCOPE (decided 2026-09-05): corrections, corrigenda, errata, retractions and
withdrawals ONLY. Deliberately NOT dropped here, even though they are equally
incapable of being a paper:

  * issue front matter — "Issue Information", mastheads, tables of contents,
    author/subject indexes, "Subscription and Copyright information". These do
    reach the LLM and are rejected there by the NO list in filter_prompt.md.
  * addenda and expressions of concern — these carry content.
  * Editorial, Comment on, Reply to, Correspondence, News & Views — these can
    carry a real argument about a method, and they were 1 item in the same
    measured week. The recall risk is real and the saving is nil.

Widening the list back out is a one-line change to `_LABEL`; the tests below
pin the current boundary in both directions so the change stays deliberate.
"""
from __future__ import annotations

import re

from .models import Paper

_LABEL = r"""(?:
      (?:author|publisher|editorial)\s+correction
    | correction
    | corrigend(?:um|a)
    | errat(?:um|a)
    | retraction(?:\s+(?:note|notice))?
    | withdrawn
)"""

# The label has to be the WHOLE title or be closed off — by a colon, a dash, a
# bracket, or the "to"/"for" that introduces the corrected paper (PNAS files
# "Correction for Sharp et al., ...", Elsevier files "Retraction notice to ...").
# Matching the bare word instead would eat "Retraction-resistant synthetic gene
# circuits" and "Error correction in DNA data storage", which is exactly how a
# prefilter turns into a permanent miss.
_LEADING = re.compile(
    rf"^\s*{_LABEL}\s*(?:$|[:.—–]|-\s|\(|\[|\b(?:to|for)\b)",
    re.IGNORECASE | re.VERBOSE,
)

# PubMed says it outright, so we do not have to read the title at all. The
# retraction notice AND the retracted paper both go: recommending a paper that
# has been retracted is worse than missing it.
NON_ARTICLE_PUBTYPES = frozenset({
    "Published Erratum",
    "Retraction of Publication",
    "Retracted Publication",
})


def is_non_article(title: str, pub_types: frozenset[str] | set[str] | None = None) -> bool:
    """True for corrections, errata, retractions and withdrawals."""
    if pub_types and NON_ARTICLE_PUBTYPES & set(pub_types):
        return True
    t = (title or "").strip()
    if not t:
        return False
    return bool(_LEADING.match(t))


def drop_non_articles(papers: list[Paper], *,
                      log=lambda msg: None) -> list[Paper]:
    """Return `papers` without the non-articles, naming each one that goes.

    A silent drop reads as "nothing was there". These are logged individually
    so a mis-fired pattern shows up in the run log instead of hiding.
    """
    kept, dropped = [], []
    for p in papers:
        (dropped if is_non_article(p.title) else kept).append(p)
    if dropped:
        log(f"Non-article prefilter: -{len(dropped)} "
            f"(corrections/errata/retractions), {len(kept)} to the filter")
        for p in dropped:
            log(f"    · {p.title[:100]}")
    return kept
