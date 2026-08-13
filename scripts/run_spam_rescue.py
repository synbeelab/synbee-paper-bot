#!/usr/bin/env python
"""Screen the Gmail spam folder and pull legitimate mail back to the inbox.

    python scripts/run_spam_rescue.py --dry-run     # inspect, change nothing
    python scripts/run_spam_rescue.py               # apply

Exits non-zero when the run should be looked at: missing credentials, the
rescue circuit breaker tripping, or too many messages failing to classify.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synbee_bot.spam_rescue.gmail import GmailClient, GmailError  # noqa: E402
from synbee_bot.spam_rescue.rescue import (  # noqa: E402
    load_rescue_config,
    run_rescue,
)

try:
    from dotenv import load_dotenv
except ImportError:  # optional locally, absent in CI
    load_dotenv = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gmail spam-folder rescue")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify and print, change no labels")
    parser.add_argument("--config", type=Path, default=None,
                        help="override config/spam_rescue.yml")
    parser.add_argument("--max-messages", type=int, default=None,
                        help="override limits.max_messages_per_run")
    parser.add_argument("--max-rescues", type=int, default=None,
                        help="override the rescue circuit breaker")
    parser.add_argument("--min-confidence", type=int, default=None,
                        help="override llm.min_confidence")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if load_dotenv is not None:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    missing = [name for name in
               ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                "GMAIL_REFRESH_TOKEN", "GEMINI_API_KEY")
               if not os.environ.get(name)]
    if missing:
        sys.stderr.write(f"missing environment variable(s): {', '.join(missing)}\n")
        return 2

    cfg = load_rescue_config(args.config)
    overrides = {
        name: value for name, value in (
            ("max_messages_per_run", args.max_messages),
            ("max_rescues_per_run", args.max_rescues),
            ("min_confidence", args.min_confidence),
        ) if value is not None
    }
    if overrides:
        cfg = replace(cfg, **overrides)

    client = GmailClient(
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        timeout=cfg.timeout,
    )

    try:
        summary = run_rescue(
            client, cfg, api_key=os.environ["GEMINI_API_KEY"], dry_run=args.dry_run
        )
    except GmailError as exc:
        sys.stderr.write(f"Gmail API error: {exc}\n")
        return 1

    if summary.aborted:
        return 1
    if summary.scanned and summary.error_ratio > cfg.max_error_ratio:
        sys.stderr.write(
            f"{summary.retry}/{summary.scanned} messages failed to classify "
            f"(> {cfg.max_error_ratio:.0%}); they were left in spam unmarked "
            f"and will be retried, but the cause needs checking.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
