"""Spam-rescue decision logic — the part that decides what moves and what stays.

The Gmail and Gemini calls are I/O and are not exercised here; everything that
can silently mis-file a message is.
"""
from __future__ import annotations

import pytest

from synbee_bot.spam_rescue.classify import Judgment, parse_judgment, render_prompt
from synbee_bot.spam_rescue.gmail import GmailMessage, _extract_body, _strip_html
from synbee_bot.spam_rescue.rescue import (
    Action, decide, fails_all_authentication, never_rescue_match,
)


def make_message(*, sender: str = "student@snu.ac.kr",
                 subject: str = "Internship inquiry",
                 auth: str = "mx.google.com; spf=pass; dkim=pass; dmarc=pass",
                 body: str = "Dear Professor Yang, I would like to apply.") -> GmailMessage:
    return GmailMessage(
        id="m1", thread_id="t1", label_ids=("SPAM",), internal_date_ms=0,
        headers={"From": sender, "Subject": subject, "Authentication-Results": auth},
        body_text=body,
    )


def judgment(verdict: str = "RESCUE", confidence: int = 9,
             category: str = "student_inquiry", is_error: bool = False) -> Judgment:
    return Judgment(verdict=verdict, category=category, confidence=confidence,
                    reason="테스트", model_used="test", is_error=is_error)


# --- decide ----------------------------------------------------------------
def test_rescues_confident_legitimate_mail():
    action, _ = decide(make_message(), judgment(), min_confidence=6)
    assert action is Action.RESCUE


def test_keeps_mail_the_model_called_spam():
    action, _ = decide(make_message(), judgment(verdict="SPAM", category="predatory"),
                       min_confidence=6)
    assert action is Action.KEEP


def test_keeps_rescue_below_confidence_threshold():
    action, note = decide(make_message(), judgment(confidence=5), min_confidence=6)
    assert action is Action.KEEP
    assert "confidence 5" in note


def test_confidence_exactly_at_threshold_rescues():
    action, _ = decide(make_message(), judgment(confidence=6), min_confidence=6)
    assert action is Action.RESCUE


def test_classification_failure_retries_instead_of_filing():
    """A failed call must not mark the message checked — that would lose it."""
    action, _ = decide(make_message(), judgment(is_error=True), min_confidence=6)
    assert action is Action.RETRY


def test_spoof_guard_overrides_a_confident_rescue():
    spoofed = make_message(
        sender="admin@korea.ac.kr",
        auth="mx.google.com; spf=fail; dkim=fail; dmarc=fail",
    )
    action, note = decide(spoofed, judgment(confidence=10, category="ku_internal"),
                          min_confidence=6)
    assert action is Action.KEEP
    assert "spoof guard" in note


# --- authentication --------------------------------------------------------
@pytest.mark.parametrize("auth", [
    "mx.google.com; spf=pass; dkim=pass; dmarc=pass",
    "mx.google.com; spf=fail; dkim=pass; dmarc=pass",   # forwarded mailing list
    "mx.google.com; spf=pass; dkim=fail; dmarc=fail",   # broken relay signing
    "(none)",
    "",
])
def test_partial_auth_failure_is_not_a_spoof(auth):
    """One or two failures happen to real mail; vetoing on them loses students."""
    assert fails_all_authentication(make_message(auth=auth)) is False


@pytest.mark.parametrize("auth", [
    "mx.google.com; spf=fail; dkim=fail; dmarc=fail",
    "mx.google.com; spf=softfail; dkim=fail; dmarc=fail",
    "mx.google.com; dmarc=fail; spf=permerror; dkim=fail",
])
def test_all_three_failing_is_a_spoof(auth):
    assert fails_all_authentication(make_message(auth=auth)) is True


# --- parsing ---------------------------------------------------------------
def test_parses_fenced_json():
    out = parse_judgment('```json\n{"verdict":"RESCUE","category":"seminar_invite",'
                         '"confidence":8,"reason":"세미나 초청"}\n```')
    assert (out.verdict, out.category, out.confidence) == ("RESCUE", "seminar_invite", 8)
    assert out.is_error is False


def test_unparseable_reply_is_an_error_not_a_spam_verdict():
    out = parse_judgment("I think this one is fine, actually.")
    assert out.is_error is True
    assert out.is_rescue is False


def test_unknown_verdict_string_is_an_error():
    out = parse_judgment('{"verdict":"MAYBE","confidence":9}')
    assert out.is_error is True


def test_confidence_is_clamped():
    assert parse_judgment('{"verdict":"SPAM","confidence":99}').confidence == 10
    assert parse_judgment('{"verdict":"SPAM","confidence":-4}').confidence == 0


def test_missing_confidence_defaults_to_zero():
    out = parse_judgment('{"verdict":"RESCUE"}')
    assert out.confidence == 0
    assert decide(make_message(), out, min_confidence=6)[0] is Action.KEEP


# --- prompt rendering ------------------------------------------------------
def test_prompt_carries_the_signals_the_model_needs():
    template = ("{sender}|{subject}|{auth_results}|{body}|{body_chars}|"
                "{reply_to}|{to}|{date}|{list_unsubscribe}")
    out = render_prompt(template, make_message(), body_chars=100)
    assert "student@snu.ac.kr" in out
    assert "Internship inquiry" in out
    assert "spf=pass" in out
    assert "{" not in out  # every placeholder substituted


def test_prompt_truncates_long_bodies():
    long_body = "x" * 9000
    out = render_prompt("{body}", make_message(body=long_body), body_chars=50)
    assert len(out) == 50


def test_empty_body_is_labelled_not_blank():
    assert render_prompt("{body}", make_message(body=""), body_chars=50) == "(empty body)"


# --- message parsing -------------------------------------------------------
def test_sender_domain_extraction():
    msg = make_message(sender="Someone <contact@shared1.ccsend.com>")
    assert msg.sender_domain == "shared1.ccsend.com"


def test_sender_domain_is_empty_when_unparseable():
    assert make_message(sender="no-at-sign").sender_domain == ""


def test_strip_html_drops_script_and_tags():
    text = _strip_html("<div>Hello <script>evil()</script><b>world</b></div>")
    assert "evil" not in text
    assert text == "Hello world"


def test_extract_body_prefers_plain_text_over_html():
    import base64

    def b64(text: str) -> str:
        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("plain version")}},
            {"mimeType": "text/html", "body": {"data": b64("<p>html version</p>")}},
        ],
    }
    assert _extract_body(payload) == "plain version"


def test_extract_body_falls_back_to_html():
    import base64

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [{
            "mimeType": "text/html",
            "body": {"data": base64.urlsafe_b64encode(b"<p>only html</p>").decode().rstrip("=")},
        }],
    }
    assert _extract_body(payload) == "only html"


# --- never-rescue list -----------------------------------------------------
@pytest.mark.parametrize("sender", [
    '"인권·성평등센터" <humanrights@korea.ac.kr>',
    "HumanRights@Korea.ac.kr",
])
def test_never_rescue_matches_listed_address(sender):
    hit = never_rescue_match(make_message(sender=sender),
                             senders=("humanrights@korea.ac.kr",), keywords=())
    assert hit == "humanrights@korea.ac.kr"


def test_never_rescue_does_not_spill_onto_the_rest_of_the_domain():
    """Excluding one korea.ac.kr address must not silence the 행정실."""
    hit = never_rescue_match(make_message(sender="bsy1025@korea.ac.kr"),
                             senders=("humanrights@korea.ac.kr",), keywords=())
    assert hit is None


def test_never_rescue_domain_entry_covers_subdomains_only_by_suffix():
    senders = ("@example.org",)
    assert never_rescue_match(make_message(sender="a@example.org"),
                              senders=senders, keywords=()) == "@example.org"
    assert never_rescue_match(make_message(sender="a@mail.example.org"),
                              senders=senders, keywords=()) == "@example.org"
    assert never_rescue_match(make_message(sender="a@notexample.org"),
                              senders=senders, keywords=()) is None


@pytest.mark.parametrize("sender, subject", [
    ("고려대학교 고령사회연구원 <aging@korea.ac.kr>", "세미나 안내"),
    ("noreply@korea.ac.kr", "[고령사회연구원] 정기 포럼 개최 안내"),
])
def test_never_rescue_keyword_matches_display_name_or_subject(sender, subject):
    hit = never_rescue_match(make_message(sender=sender, subject=subject),
                             senders=(), keywords=("고령사회연구원",))
    assert hit == "고령사회연구원"


def test_never_rescue_keyword_ignores_body():
    """A student citing the institute in a letter is still a student."""
    msg = make_message(body="저는 고령사회연구원 과제에 참여한 경험이 있습니다.")
    assert never_rescue_match(msg, senders=(), keywords=("고령사회연구원",)) is None
