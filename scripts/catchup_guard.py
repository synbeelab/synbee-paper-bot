#!/usr/bin/env python3
"""Workflow-side entry point for the catch-up guard.

Writes ``should_run=true|false`` to ``$GITHUB_OUTPUT`` so the real job can gate
on it. See ``synbee_bot/catchup.py`` for why the catch-up crons exist and why
this guard fails open.

Stdlib only, on purpose: the two ping workflows call it without a pip install.

Env:
  WORKFLOW_FILE       e.g. "daily.yml" (required)
  GITHUB_REPOSITORY   "owner/repo"    (provided by Actions)
  GITHUB_TOKEN        needs `actions: read`
  GITHUB_EVENT_NAME   "schedule" | "workflow_dispatch" | ...
  GITHUB_RUN_ID       this run, so it never blocks itself
  GITHUB_OUTPUT       Actions output file
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synbee_bot.catchup import decide, fetch_successful_runs  # noqa: E402


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    workflow_file = os.environ.get("WORKFLOW_FILE", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "schedule")
    run_id = _int_or_none(os.environ.get("GITHUB_RUN_ID", ""))

    if not (workflow_file and repo and token):
        # Misconfiguration must not be the reason a digest goes missing.
        sys.stderr.write(
            "  ! catch-up guard is missing WORKFLOW_FILE/GITHUB_REPOSITORY/"
            "GITHUB_TOKEN; running anyway\n"
        )
        should_run = True
    else:
        should_run = decide(
            lambda: fetch_successful_runs(repo, workflow_file, token=token),
            event_name=event_name,
            current_run_id=run_id,
        )

    if should_run:
        print(f"✓ proceeding ({event_name})")
    else:
        print(
            f"↷ skipping: {workflow_file} already delivered today (KST). "
            "This is a catch-up cron doing its job."
        )

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"should_run={'true' if should_run else 'false'}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
