"""
SynBEE Lab paper bot — query builder.

Read config/journals.yml + config/keywords.yml and emit:
  - PubMed query (rich syntax: Title/Abstract + Journal + AND NOT)
  - bioRxiv query (1-level OR only)
  - arXiv category list (paperscraper / arXiv API hint)

Usage:
    python scripts/build_query.py
    python scripts/build_query.py --pubmed-only
    python scripts/build_query.py --no-journal-filter
    python scripts/build_query.py --output queries.txt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML required. Install: pip install pyyaml\n")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _collect_active(yaml_data: dict, group_predicate=lambda v: True) -> list[str]:
    """Walk a yaml dict, collect items from groups where active=true."""
    items: list[str] = []
    for group_name, group in yaml_data.items():
        if not isinstance(group, dict):
            continue
        if not group.get("active", False):
            continue
        if not group_predicate(group):
            continue
        for key in ("journals", "keywords"):
            if key in group and isinstance(group[key], list):
                items.extend(group[key])
    return items


def collect_journals(journals_yaml: dict) -> list[str]:
    return sorted(set(_collect_active(journals_yaml)))


def collect_keywords(keywords_yaml: dict, *, include_aux: bool = True) -> dict[str, list[str]]:
    """Return {'mission': [...], 'aux': [...], 'exclude': [...]}."""
    out = {"mission": [], "aux": [], "exclude": []}
    for group_name, group in keywords_yaml.items():
        if group_name == "constraints":
            continue
        if not isinstance(group, dict) or not group.get("active", False):
            continue
        kws = group.get("keywords", [])
        if group_name == "exclude":
            out["exclude"].extend(kws)
        elif group_name == "auxiliary":
            if include_aux:
                out["aux"].extend(kws)
        elif group_name.startswith("mission"):
            out["mission"].extend(kws)
    out["mission"] = sorted(set(out["mission"]))
    out["aux"] = sorted(set(out["aux"]))
    out["exclude"] = sorted(set(out["exclude"]))
    return out


def _quote(term: str) -> str:
    """PubMed quoting — wrap multi-word terms in double quotes."""
    term = term.strip()
    if " " in term and not (term.startswith('"') and term.endswith('"')):
        return f'"{term}"'
    return term


def build_pubmed_query(
    journals: list[str],
    keywords: dict[str, list[str]],
    *,
    search_field: str = "tiab",
    journal_filter: bool = True,
    language_filter: bool = True,
) -> str:
    """Build a PubMed boolean query string."""
    field = f"[{search_field}]"

    all_kw = keywords["mission"] + keywords["aux"]
    if not all_kw:
        raise ValueError("No active keywords found in keywords.yml")

    kw_clause = " OR ".join(f"{_quote(k)}{field}" for k in all_kw)
    parts = [f"({kw_clause})"]

    if journal_filter and journals:
        j_clause = " OR ".join(f'"{j}"[Journal]' for j in journals)
        parts.append(f"({j_clause})")

    if keywords["exclude"]:
        ex_clause = " OR ".join(f"{_quote(k)}{field}" for k in keywords["exclude"])
        # NOT applied to whole expression below
        not_clause = f"NOT ({ex_clause})"
    else:
        not_clause = None

    query = " AND ".join(parts)
    if not_clause:
        query = f"({query}) {not_clause}"

    if language_filter:
        query = f"({query}) AND English[Language]"

    return query


def build_pubmed_journal_query(
    journals: list[str],
    exclude: list[str] | None = None,
    *,
    language_filter: bool = True,
) -> str:
    """Journal-only PubMed query (NO keyword gate) — every paper in the
    whitelisted journals, minus the exclude terms. Used by the weekly sweep."""
    if not journals:
        raise ValueError("No active journals for journal-only query")
    j_clause = " OR ".join(f'"{j}"[Journal]' for j in journals)
    query = f"({j_clause})"
    if exclude:
        ex_clause = " OR ".join(f"{_quote(k)}[tiab]" for k in exclude)
        query = f"({query}) NOT ({ex_clause})"
    if language_filter:
        query = f"({query}) AND English[Language]"
    return query


def build_biorxiv_query(keywords: dict[str, list[str]]) -> str:
    """bioRxiv: only 1-level OR allowed; no AND/AND NOT/journal."""
    mission_kw = keywords["mission"]
    if not mission_kw:
        raise ValueError("No mission keywords for bioRxiv")
    return " OR ".join(f"[{k}]" for k in mission_kw)


def collect_arxiv_categories() -> list[str]:
    """Static recommendation — biology + chem subcategories."""
    return [
        "q-bio.BM",     # Biomolecules
        "q-bio.MN",     # Molecular Networks
        "q-bio.GN",     # Genomics
        "q-bio.SC",     # Subcellular Processes
        "q-bio.CB",     # Cell Behavior
        # CS for ML-bio
        "cs.LG",        # Learning (filter heavily downstream)
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Build PubMed / bioRxiv queries from YAML config")
    ap.add_argument("--pubmed-only", action="store_true")
    ap.add_argument("--biorxiv-only", action="store_true")
    ap.add_argument("--no-journal-filter", action="store_true",
                    help="Skip journal whitelist (broader recall, more noise)")
    ap.add_argument("--no-aux", action="store_true", help="Exclude auxiliary keywords")
    ap.add_argument("--output", "-o", type=Path, help="Write to file instead of stdout")
    args = ap.parse_args()

    journals_path = CONFIG_DIR / "journals.yml"
    keywords_path = CONFIG_DIR / "keywords.yml"
    if not journals_path.exists() or not keywords_path.exists():
        sys.stderr.write(f"Missing config files in {CONFIG_DIR}\n")
        return 1

    journals_yaml = _load_yaml(journals_path)
    keywords_yaml = _load_yaml(keywords_path)

    constraints = keywords_yaml.get("constraints", {}) or {}
    search_field = constraints.get("search_field", "tiab")
    language_filter = bool(constraints.get("language_filter", True))

    journals = collect_journals(journals_yaml)
    keywords = collect_keywords(keywords_yaml, include_aux=not args.no_aux)

    out_lines: list[str] = []

    if not args.biorxiv_only:
        pubmed_q = build_pubmed_query(
            journals, keywords,
            search_field=search_field,
            journal_filter=not args.no_journal_filter,
            language_filter=language_filter,
        )
        out_lines.append("=" * 78)
        out_lines.append(f"PubMed query  ({len(journals)} journals, {len(keywords['mission'])+len(keywords['aux'])} kw, {len(keywords['exclude'])} excl)")
        out_lines.append("=" * 78)
        out_lines.append(pubmed_q)
        out_lines.append("")

    if not args.pubmed_only:
        biorxiv_q = build_biorxiv_query(keywords)
        out_lines.append("=" * 78)
        out_lines.append(f"bioRxiv query  ({len(keywords['mission'])} mission keywords)")
        out_lines.append("=" * 78)
        out_lines.append(biorxiv_q)
        out_lines.append("")

        out_lines.append("=" * 78)
        out_lines.append("arXiv categories (recommendation)")
        out_lines.append("=" * 78)
        out_lines.extend(collect_arxiv_categories())

    text = "\n".join(out_lines)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
