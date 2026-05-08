"""
Slack token verifier and channel listing helper.

Usage (after setting SLACK_BOT_TOKEN in .env):
    python scripts/slack_setup_helper.py
    python scripts/slack_setup_helper.py --post-test "안녕 SynBEE 봇!"
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-test", metavar="TEXT",
                    help="Post a test message to all reachable channels")
    ap.add_argument("--channel", help="Channel ID for --post-test (default: first writable)")
    args = ap.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("✗ SLACK_BOT_TOKEN missing — set it in .env", file=sys.stderr)
        return 1
    if not token.startswith("xoxb-"):
        print(f"⚠️  Token does not start with 'xoxb-' (got '{token[:10]}…') — "
              "make sure it's the Bot User OAuth Token, not the App-Level Token",
              file=sys.stderr)

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        print("Run: pip install slack_sdk python-dotenv", file=sys.stderr)
        return 1

    client = WebClient(token=token)

    # auth.test
    print("=" * 70)
    print("auth.test")
    print("=" * 70)
    try:
        resp = client.auth_test()
        print(f"  ✓ team       : {resp['team']}")
        print(f"  ✓ user (bot) : {resp['user']} ({resp.get('user_id')})")
        print(f"  ✓ bot_id     : {resp.get('bot_id')}")
        print(f"  ✓ url        : {resp.get('url')}")
    except SlackApiError as e:
        print(f"  ✗ auth.test failed: {e.response['error']}")
        return 2

    # channels list
    print()
    print("=" * 70)
    print("conversations.list (public + private + IM)")
    print("=" * 70)
    print(f"  {'ID':<14} {'Name':<32} {'Type':<10} {'Member?':<8}")
    print(f"  {'-'*14} {'-'*32} {'-'*10} {'-'*8}")
    try:
        cursor = None
        candidates: list[tuple[str, str]] = []
        while True:
            resp = client.conversations_list(
                types="public_channel,private_channel",
                limit=200, cursor=cursor,
            )
            for ch in resp["channels"]:
                ch_type = "private" if ch.get("is_private") else "public"
                is_member = "yes" if ch.get("is_member") else "no"
                name = ch.get("name", "")
                cid = ch.get("id", "")
                print(f"  {cid:<14} #{name:<31} {ch_type:<10} {is_member:<8}")
                candidates.append((cid, name))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except SlackApiError as e:
        print(f"  ✗ conversations.list failed: {e.response['error']}")

    # post test
    if args.post_test:
        target = args.channel
        if not target:
            print("\n  No --channel given. Pick one from the list above and re-run with "
                  "--channel C0XXXXXXXX.")
            return 0
        print()
        print("=" * 70)
        print(f"chat.postMessage to {target}")
        print("=" * 70)
        try:
            resp = client.chat_postMessage(channel=target, text=args.post_test)
            print(f"  ✓ posted ts={resp['ts']}")
        except SlackApiError as e:
            print(f"  ✗ {e.response['error']}")
            print("    Hint: invite the bot to the channel first — `/invite @SynBEE Paper Bot`")
            return 3

    # config.yml hints
    print()
    print("=" * 70)
    print("Next: paste channel IDs into config/config.yml")
    print("=" * 70)
    print("  slack:")
    print("    enabled: true")
    print("    channels:")
    print("      daily_digest: \"C0XXXXXXXX\"   # #papers-daily")
    print("      high_priority: \"C0XXXXXXXX\"  # #papers-priority")
    print("      test:          \"C0XXXXXXXX\"  # #papers-test")
    print("    use_test_channel: true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
