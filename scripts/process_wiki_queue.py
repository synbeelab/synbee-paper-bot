"""
Process GitHub Issues labeled `wiki-queue` → write markdown files to
the SynBEE Wiki `raw/` directory for downstream mode A ingest.

Requires `gh` CLI authenticated (`gh auth login`).

Usage:
    python scripts/process_wiki_queue.py                 # list + write all open
    python scripts/process_wiki_queue.py --dry-run       # preview, don't write
    python scripts/process_wiki_queue.py --close         # close issues after writing
    python scripts/process_wiki_queue.py --limit 5
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Force UTF-8 on Windows stdout
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from synbee_bot.config import load_config  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Resolve `gh` once. shutil.which uses PATH; if missing, check known Windows
# install locations as fallback (user may have installed gh without restarting
# shell / updating PATH).
_GH_FALLBACK_PATHS = [
    r"C:\Program Files\GitHub CLI\gh.exe",
    r"C:\Program Files (x86)\GitHub CLI\gh.exe",
    str(Path.home() / "AppData" / "Local" / "Programs" / "GitHub CLI" / "gh.exe"),
]


def _find_gh() -> str | None:
    found = shutil.which("gh") or shutil.which("gh.exe")
    if found:
        return found
    for p in _GH_FALLBACK_PATHS:
        if Path(p).exists():
            return p
    return None


GH = _find_gh()


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, capture stdout/stderr, return CompletedProcess.
    Replaces literal 'gh' with the resolved absolute path."""
    if cmd and cmd[0] == "gh":
        if not GH:
            raise FileNotFoundError("gh CLI not found in PATH")
        cmd = [GH] + cmd[1:]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", check=check)


def _slugify(text: str, max_len: int = 70) -> str:
    """Slugify keeping Korean characters intact."""
    text = text.lower().strip()
    text = re.sub(r"[^\w가-힣\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:max_len] or "untitled"


def _parse_issue_body(body: str) -> dict[str, str]:
    """Extract metadata from the bot-generated issue body."""
    fields: dict[str, str] = {}
    # Lines like "**Paper ID**: `pubmed:12345`" → fields["paper_id"] = "pubmed:12345"
    for m in re.finditer(r"\*\*([^*]+)\*\*:\s*`?([^`\n]+)`?", body):
        key = m.group(1).strip().lower().replace(" ", "_")
        val = m.group(2).strip()
        fields[key] = val

    # Section bodies
    def _section(name: str) -> str:
        pat = rf"##\s*{re.escape(name)}\s*\n(.+?)(?=\n##\s|\n---|\Z)"
        sm = re.search(pat, body, re.S)
        return sm.group(1).strip() if sm else ""

    fields["kr_summary"] = _section("🇰🇷 KR Summary")
    fields["en_summary"] = _section("🇬🇧 EN Summary")
    fields["abstract"] = _section("Abstract")
    return fields


def _markdown_from_issue(issue: dict, fields: dict[str, str]) -> str:
    """Render the markdown file content."""
    issue_num = issue["number"]
    issue_url = issue["url"]
    title = issue["title"].replace("Wiki: ", "", 1)
    today = dt.date.today().isoformat()

    fm = [
        "---",
        "source: synbee-paper-bot",
        f"paper_id: {fields.get('paper_id', 'unknown')}",
        f"paper_source: {fields.get('source', 'unknown')}",
        f"journal: {fields.get('journal', 'unknown')}",
        f"doi: {fields.get('doi', 'n/a')}",
        f"url: {fields.get('url', 'n/a')}",
        f"mission: {fields.get('mission', 'n/a')}",
        f"score: {fields.get('score', 'n/a')}",
        f"github_issue: {issue_num}",
        f"github_issue_url: {issue_url}",
        f"ingested_at: {today}",
        "---",
        "",
        f"# {title}",
        "",
        "## Authors",
        fields.get("authors", "n/a"),
        "",
        "## 🇰🇷 KR Summary",
        fields.get("kr_summary", "(n/a)"),
        "",
        "## 🇬🇧 EN Summary",
        fields.get("en_summary", "(n/a)"),
        "",
        "## Abstract",
        fields.get("abstract", "(n/a)"),
        "",
        f"_GitHub Issue: [#{issue_num}]({issue_url})_",
        "",
    ]
    return "\n".join(fm)


def _write_markdown(out_dir: Path, paper_id: str, title: str, content: str,
                    dry_run: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().strftime("%Y%m%d")
    base = _slugify(title) or _slugify(paper_id) or "paper"
    path = out_dir / f"{today}_{base}.md"
    # Avoid overwrite — append numeric suffix
    n = 1
    while path.exists():
        path = out_dir / f"{today}_{base}-{n}.md"
        n += 1
    if dry_run:
        return path
    path.write_text(content, encoding="utf-8")
    return path


def _close_issue(github_repo: str, number: int) -> None:
    _run(["gh", "issue", "close", str(number),
          "--repo", github_repo, "--reason", "completed",
          "--comment", "Ingested into SynBEE Wiki raw/. Closed by process_wiki_queue.py."])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="List + preview content, don't write or close")
    ap.add_argument("--close", action="store_true",
                    help="Close issue after successful ingest")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--state", choices=["open", "closed", "all"], default="open")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.wiki_github_repo:
        sys.stderr.write("config: wiki_queue.github_repo not set. Aborting.\n")
        return 1
    if not cfg.wiki_vault_raw_dir.parent.exists():
        sys.stderr.write(f"raw/ parent dir missing: {cfg.wiki_vault_raw_dir.parent}\n")
        return 1

    # Check gh is available + authenticated
    if not GH:
        sys.stderr.write(
            "`gh` CLI not found in PATH.\n"
            "  • Install: https://cli.github.com/\n"
            "  • Or open a new PowerShell window if just installed.\n"
            "  • Verify: gh --version\n"
        )
        return 1
    auth = _run(["gh", "auth", "status"], check=False)
    if auth.returncode != 0:
        sys.stderr.write("`gh auth login` first.\n" + auth.stderr)
        return 1

    print(f"Fetching wiki-queue issues from {cfg.wiki_github_repo}…")
    proc = _run([
        "gh", "issue", "list",
        "--repo", cfg.wiki_github_repo,
        "--label", "wiki-queue",
        "--state", args.state,
        "--limit", str(args.limit),
        "--json", "number,title,body,url,createdAt,labels",
    ])
    issues = json.loads(proc.stdout or "[]")
    if not issues:
        print("No wiki-queue issues found.")
        return 0

    print(f"Found {len(issues)} issue(s). Output dir: {cfg.wiki_vault_raw_dir}")
    print()

    written = 0
    closed = 0
    for issue in issues:
        num = issue["number"]
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        fields = _parse_issue_body(body)
        paper_id = fields.get("paper_id", "")

        content = _markdown_from_issue(issue, fields)
        path = _write_markdown(cfg.wiki_vault_raw_dir, paper_id, title,
                                content, dry_run=args.dry_run)

        action = "[DRY-RUN]" if args.dry_run else "[WROTE]"
        print(f"  {action} #{num} {title[:60]}")
        print(f"          → {path}")
        if not args.dry_run:
            written += 1
            if args.close:
                try:
                    _close_issue(cfg.wiki_github_repo, num)
                    closed += 1
                    print(f"          ✓ closed issue #{num}")
                except subprocess.CalledProcessError as e:
                    sys.stderr.write(f"          ! close failed: {e.stderr}\n")
        print()

    print(f"Done. {written} file(s) written, {closed} issue(s) closed.")
    if args.dry_run:
        print("(dry-run — no files written, no issues closed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
