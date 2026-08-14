"""Gmail spam-folder rescue — pull false-positive spam back into the inbox.

The claude.ai Gmail connector walls off the spam folder (`in:spam` returns
nothing, `unmark_message_spam` is permission-denied), so this runs against the
Gmail REST API directly with a `gmail.modify` refresh token.
"""
