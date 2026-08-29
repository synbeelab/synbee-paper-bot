"""The only place a paper is dropped before the LLM sees it, so: narrow, loud."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synbee_bot.models import Paper  # noqa: E402
from synbee_bot.prefilter import drop_non_articles, is_non_article  # noqa: E402


def paper(title: str) -> Paper:
    return Paper(id=f"doi:{abs(hash(title))}", source="crossref_toc", title=title,
                 authors=["A B"], journal="J", year=2026, abstract="",
                 doi=None, url="https://e.org", published="2026-08-30")


# Titles taken verbatim from the 2026-08-30 sweep.
@pytest.mark.parametrize("title", [
    "Author Correction: Specific oncogene activation of the cell of origin in mucosal melanoma",
    "Publisher Correction: Activity-dependent ribosome profiling reveals the landscape",
    "Retraction Note: Increased WNT10B/FOXO6 signaling promotes cell fate transition",
    "Corrigendum to 'A modular polyketide synthase platform'",
    "Erratum: Directed evolution of a thermostable lipase",
    "Correction to: Engineering Escherichia coli Nissle 1917",
    "Correction for Sharp et al., Extreme triple oxygen isotope fractionation",
    "Retraction notice to “Advances in ultrasound-assisted synthesis”",
    "Issue Editorial Masthead",
    "Issue Information",
    "Masthead",
    "Table of Contents",
    "Author Index",
    "Acknowledgment of Reviewers",
    "In Memoriam: A great enzymologist",
    "Expression of Concern: base editing off-targets",
])
def test_non_articles_are_recognised(title):
    assert is_non_article(title)


# Real papers whose titles brush against the patterns. A false positive here is
# a permanently missed paper, so these are the cases that matter most.
@pytest.mark.parametrize("title", [
    "Error correction in DNA data storage using engineered polymerases",
    "Retraction-resistant synthetic gene circuits in Escherichia coli",
    "Indexing the metabolome of Streptomyces coelicolor",
    "Cover crops shape the rhizosphere microbiome",
    "A corrigendum-free workflow for reproducible proteomics",
    "Contents of the polyketide chemical space explored by module swapping",
    "Observation of erratic non-Hermitian skin localization and transport",
    "3D printed designer color routers with low refractive index for low-light imaging",
])
def test_real_papers_survive(title):
    assert not is_non_article(title)


# Deliberately not dropped: these can carry a real argument about a method.
@pytest.mark.parametrize("title", [
    "Editorial: the next decade of synthetic biology",
    "Comment on 'A universal biosensor scaffold'",
    "Reply to Panfoli et al.: From O2 consumption in myelin to ATP delivery",
    "Correspondence: reproducibility of directed evolution screens",
])
def test_opinion_pieces_are_left_alone(title):
    assert not is_non_article(title)


def test_empty_title_is_not_dropped():
    assert not is_non_article("")
    assert not is_non_article("   ")


def test_drop_non_articles_keeps_the_rest_and_names_every_drop():
    papers = [
        paper("Author Correction: something"),
        paper("A modular PKS platform for polyketide diversification"),
        paper("Retraction Note: something else"),
    ]
    lines: list[str] = []
    kept = drop_non_articles(papers, log=lines.append)

    assert [p.title for p in kept] == ["A modular PKS platform for polyketide diversification"]
    # Silent truncation reads as "nothing was there" — every drop is named.
    assert any("Author Correction" in ln for ln in lines)
    assert any("Retraction Note" in ln for ln in lines)


def test_nothing_to_drop_logs_nothing():
    papers = [paper("A modular PKS platform")]
    lines: list[str] = []
    assert drop_non_articles(papers, log=lines.append) == papers
    assert lines == []
