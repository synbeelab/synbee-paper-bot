"""The prompt's authentication rule must agree with the code's spoof guard.

2026-08-18: two Korea University LMS notices (`elearning@korea.ac.kr`, TA
approval requests) were kept in spam as *phishing*, reasoned as "고려대 발신
메일의 DMARC 인증이 실패하여 피싱으로 간주됩니다".

The mail is genuine. KU's LMS is operated by Xinics and delivered through
Mailgun, so the real headers read::

    dkim=pass header.i=@xinics.com header.s=krs
    spf=pass  (bounce+...@xinics.com designates 204.220.184.20 ...)
    dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=korea.ac.kr

DKIM and SPF both pass — for the vendor's domain. Only DMARC fails, because
`header.from` is korea.ac.kr and neither authenticated identifier aligns with
it, and korea.ac.kr itself publishes `p=NONE` (monitor only). That is the
signature of a third-party sender, not of forgery.

`rescue.fails_all_authentication` already encodes the right policy and says
why: any one mechanism fails on legitimate mail, so only all three failing
together may veto a rescue. The prompt contradicted it with an OR over the
three, and the model's verdict is read first — a model SPAM can never be
reversed by the more forgiving guard downstream.
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from synbee_bot.spam_rescue.gmail import GmailMessage
from synbee_bot.spam_rescue.rescue import fails_all_authentication

PROMPT = Path(__file__).resolve().parent.parent / "config" / "spam_rescue_prompt.md"

# Verbatim from message 1a061d7a3e6c074b (2026-09-02 KU LMS Q&A notice).
LMS_AUTH = (
    "mx.google.com; dkim=pass header.i=@xinics.com header.s=krs "
    "header.b=0gHznsbb; spf=pass (google.com: domain of "
    "bounce+d56470.d31de-dosoyang=korea.ac.kr@xinics.com designates "
    "204.220.184.20 as permitted sender) "
    'smtp.mailfrom="bounce+d56470.d31de-dosoyang=korea.ac.kr@xinics.com"; '
    "dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=korea.ac.kr"
)

LMS = GmailMessage(
    id="m1", thread_id="t1", label_ids=("SPAM",), internal_date_ms=0,
    headers={
        "From": "고려대학교 LMS <elearning@korea.ac.kr>",
        "Subject": "[262R (서울-학부)화공생명공학실험Ⅰ-04분반] 조교 신청",
        "Authentication-Results": LMS_AUTH,
    },
    body_text="조연서(2026####57)의 조교 승인을 요청드립니다.",
)


def _prompt() -> str:
    return PROMPT.read_text(encoding="utf-8")


def test_vendor_relayed_ku_mail_does_not_trip_the_spoof_guard():
    """dkim=pass + spf=pass + dmarc=fail is a relay, not a forgery."""
    assert fails_all_authentication(LMS) is False


def test_a_real_forgery_still_trips_the_spoof_guard():
    forged = replace(LMS, headers={
        **LMS.headers,
        "Authentication-Results": ("mx.google.com; dkim=fail; spf=fail; "
                                   "dmarc=fail (p=REJECT) header.from=korea.ac.kr"),
    })
    assert fails_all_authentication(forged) is True


def test_the_prompt_does_not_call_one_failed_mechanism_phishing():
    """The rule the model actually followed on 2026-08-18."""
    text = _prompt()
    single_failure_rule = re.search(
        r"`spf=fail`.{0,40}`dkim=fail`.{0,40}`dmarc=fail`", text, re.S
    )
    assert single_failure_rule is None, (
        "the prompt still treats any one of spf/dkim/dmarc failing as "
        "phishing; rescue.fails_all_authentication requires all three, and "
        "KU's own LMS fails DMARC alone on every message it sends"
    )


def test_the_prompt_keeps_a_worked_example_of_vendor_relayed_mail():
    """Worked examples pin this boundary better than prose — see git log."""
    rows = [line for line in _prompt().splitlines()
            if "elearning@korea.ac.kr" in line]
    assert rows, "no calibration row for vendor-relayed institutional mail"
    assert any("RESCUE" in row for row in rows), rows


def test_the_prompt_still_catches_display_name_spoofing():
    rows = [line for line in _prompt().splitlines() if "microsocket" in line]
    assert rows and all("SPAM" in row for row in rows), rows
