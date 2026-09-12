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


# Titles taken verbatim from the 2026-08-30 sweep and the 2026-09-05 digest.
@pytest.mark.parametrize("title", [
    "Author Correction: Specific oncogene activation of the cell of origin in mucosal melanoma",
    "Publisher Correction: Activity-dependent ribosome profiling reveals the landscape",
    "Retraction Note: Increased WNT10B/FOXO6 signaling promotes cell fate transition",
    "Corrigendum to 'A modular polyketide synthase platform'",
    "Erratum: Directed evolution of a thermostable lipase",
    "Correction to: Engineering Escherichia coli Nissle 1917",
    "Correction for Sharp et al., Extreme triple oxygen isotope fractionation",
    "Retraction notice to “Advances in ultrasound-assisted synthesis”",
    "Correction to “Potent Racemic Antimicrobial Polypeptides Uncovered by a Stereochemical Series”",
    "Withdrawn: A CRISPRi library for Streptomyces",
])
def test_non_articles_are_recognised(title):
    assert is_non_article(title)


# Real papers whose titles brush against the patterns. A false positive here is
# a permanently missed paper, so these are the cases that matter most.
@pytest.mark.parametrize("title", [
    "Error correction in DNA data storage using engineered polymerases",
    "Retraction-resistant synthetic gene circuits in Escherichia coli",
    "A corrigendum-free workflow for reproducible proteomics",
    "Observation of erratic non-Hermitian skin localization and transport",
    "Corrective gene editing restores enzyme activity in a metabolic disorder model",
    "Withdrawal symptoms alter the gut microbiome composition",
])
def test_real_papers_survive(title):
    assert not is_non_article(title)


# Out of scope by decision (2026-09-05). Front matter is a non-paper too, but it
# is left to the NO list in filter_prompt.md; opinion pieces can carry a real
# argument about a method. Widening prefilter to cover these is a change to
# _LABEL — these assertions exist so that change cannot happen by accident.
@pytest.mark.parametrize("title", [
    "Issue Information",
    "Issue Editorial Masthead",
    "Table of Contents",
    "Subscription and Copyright information",
    "Author Index",
    "Addendum: kinetics of the P450 cascade",
    "Expression of Concern: base editing off-targets",
    "Editorial: the next decade of synthetic biology",
    "Comment on 'A universal biosensor scaffold'",
    "Reply to Panfoli et al.: From O2 consumption in myelin to ATP delivery",
    "Correspondence: reproducibility of directed evolution screens",
])
def test_out_of_scope_titles_are_left_alone(title):
    assert not is_non_article(title)


def test_empty_title_is_not_dropped():
    assert not is_non_article("")
    assert not is_non_article("   ")


# PubMed erratum records routinely carry the ORIGINAL paper's title, so the
# publication type is the only thing that gives them away.
def test_pubmed_publication_types_are_authoritative():
    innocent_title = "Engineering a glycosyltransferase for regioselective glycosylation"
    assert not is_non_article(innocent_title)
    assert is_non_article(innocent_title, {"Published Erratum"})
    assert is_non_article(innocent_title, {"Journal Article", "Retraction of Publication"})
    assert is_non_article(innocent_title, {"Retracted Publication"})
    assert not is_non_article(innocent_title, {"Journal Article", "Review"})


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
