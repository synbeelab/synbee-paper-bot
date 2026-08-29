"""Drop items that are typographically incapable of being a paper.

The Crossref sweep already asks for `type:journal-article`, but publishers file
corrections, retraction notices, issue front matter and indexes under that type
too. Measured 2026-08-30 over one week of the sweep: 18 of 848 items (2.1%).

Spending a filter call on "Author Correction: ..." buys nothing — the verdict is
NO before the model reads it — so this is free money. It is also the only place
in this bot where papers are dropped before the LLM sees them, which is why the
pattern list stays deliberately narrow and why every drop is logged by title.

Explicitly NOT dropped: Editorial, Comment on, Reply to, Correspondence, News &
Views. Those can carry a real argument about a method, and they were 1 item in
the same measured week — the recall risk is real and the saving is nil.
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
    | addendum
    | (?:editorial\s+)?expression\s+of\s+concern
    | issue\s+information
    | (?:issue\s+)?(?:editorial\s+)?masthead
    | (?:front|back)\s+matter
    | table\s+of\s+contents
    | contents
    | cover\s+(?:image|picture|story|feature)
    | (?:author|subject|keyword)\s+index
    | acknowledg(?:e)?ments?\s+(?:to|of)\s+reviewers
    | list\s+of\s+reviewers
    | obituary
    | in\s+memoriam
)"""

# The label has to be the WHOLE title or be closed off — by a colon, a dash, a
# bracket, or the "to"/"for" that introduces the corrected paper (PNAS files
# "Correction for Sharp et al., ...", Elsevier files "Retraction notice to ...").
# Matching the bare word instead would eat "Retraction-resistant synthetic gene
# circuits" and "Contents of the polyketide chemical space", which is exactly how
# a prefilter turns into a permanent miss.
_LEADING = re.compile(
    rf"^\s*{_LABEL}\s*(?:$|[:.—–]|-\s|\(|\[|\b(?:to|for)\b)",
    re.IGNORECASE | re.VERBOSE,
)


def is_non_article(title: str) -> bool:
    """True for corrections, retractions, issue front matter and indexes."""
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
            f"(corrections/retractions/front matter), {len(kept)} to the filter")
        for p in dropped:
            log(f"    · {p.title[:100]}")
    return kept
