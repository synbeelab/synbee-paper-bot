"""Applicant decline drafts — what gets drafted, with which text, and what is left alone."""
from __future__ import annotations

import base64
import email
from dataclasses import replace
from datetime import datetime
from email import policy
from pathlib import Path

import pytest
import yaml

from synbee_bot.spam_rescue import applicant as mod
from synbee_bot.spam_rescue.applicant import (
    DeclineAction,
    DeclineConfig,
    Inquiry,
    build_draft_mime,
    decide_inquiry,
    load_decline_config,
    parse_inquiry,
    prefilter,
    render_decline,
    run_decline,
)
from synbee_bot.spam_rescue.gmail import GmailMessage

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = yaml.safe_load(
    (ROOT / "config" / "applicant_decline_templates.yml").read_text(encoding="utf-8")
)
AFTER_START_MS = int(datetime.fromisoformat("2026-10-05T09:00:00+09:00").timestamp() * 1000)
SIG = '<div dir="ltr"><b>Dr. Dongsoo Yang</b><div>Associate Professor</div></div>'


def make_message(msg_id: str = "m1", *, sender: str = "Samaneh Khodi <khodi.sama@gmail.com>",
                 subject: str = "Postdoctoral enquiry", body: str = "Dear Prof. Yang",
                 extra: dict[str, str] | None = None) -> GmailMessage:
    headers = {"From": sender, "Subject": subject, "Message-ID": f"<{msg_id}@mail.gmail.com>"}
    headers.update(extra or {})
    return GmailMessage(id=msg_id, thread_id=f"t{msg_id}", label_ids=("INBOX",),
                        internal_date_ms=AFTER_START_MS, headers=headers, body_text=body)


def inquiry(**kw) -> Inquiry:
    base = dict(is_applicant=True, role="postdoc", language="en", full_name="Samaneh Khodi",
                given_name="Samaneh", family_name="Khodi", has_doctorate=True,
                confidence=9, reason="포닥 문의")
    base.update(kw)
    return Inquiry(**base)


# --- templates: his choice of wording per role ------------------------------
def test_grad_applicant_gets_the_already_secured_text():
    text = render_decline(inquiry(role="grad", has_doctorate=False), TEMPLATES)
    assert text.startswith("Dear Samaneh,")
    assert "already secured more than enough grad student candidates" in text
    assert "resources" not in text


def test_postdoc_gets_the_resources_and_space_text():
    text = render_decline(inquiry(), TEMPLATES)
    assert text.startswith("Dear Dr. Khodi,")
    assert "constraints in resources and lab space" in text
    assert "additional postdoctoral researchers" in text


@pytest.mark.parametrize("role, phrase", [("visiting", "visiting researchers"),
                                          ("intern", "interns")])
def test_other_roles_get_the_resources_text_with_their_role(role, phrase):
    text = render_decline(inquiry(role=role, has_doctorate=False), TEMPLATES)
    assert f"additional {phrase}" in text


def test_korean_grad_applicant_gets_his_korean_text():
    text = render_decline(inquiry(role="grad", language="ko", full_name="홍길동",
                                  has_doctorate=False), TEMPLATES)
    assert text.startswith("홍길동 학생에게,")
    assert "입학 예정인 학생들이 모두 이미 계획되어" in text
    assert text.rstrip().endswith("양동수 드림.")


def test_korean_postdoc_is_addressed_as_doctor():
    text = render_decline(inquiry(language="ko", full_name="김박사"), TEMPLATES)
    assert text.startswith("김박사 박사님께,")
    assert "박사후연구원 추가 선발" in text


def test_missing_name_falls_back_to_neutral_salutation():
    text = render_decline(inquiry(given_name="", family_name="", full_name=""), TEMPLATES)
    assert text.startswith("Dear Applicant,")


def test_names_with_braces_are_inserted_literally():
    text = render_decline(inquiry(has_doctorate=False, given_name="{role}"), TEMPLATES)
    assert text.startswith("Dear {role},")


# --- decision ---------------------------------------------------------------
def test_confident_postdoc_inquiry_is_drafted():
    action, _ = decide_inquiry(inquiry(), min_confidence=7)
    assert action is DeclineAction.DRAFT


@pytest.mark.parametrize("override, fragment", [
    ({"is_applicant": False}, "not applicant"),
    ({"ku_affiliated": True}, "Korea University"),
    ({"is_referral": True}, "referral"),
    ({"role": "other"}, "unclear role"),
    ({"confidence": 6}, "confidence 6"),
])
def test_exclusions_skip(override, fragment):
    action, note = decide_inquiry(inquiry(**override), min_confidence=7)
    assert action is DeclineAction.SKIP
    assert fragment in note


def test_classification_error_retries():
    action, _ = decide_inquiry(Inquiry(is_applicant=False, role="other", language="en",
                                       is_error=True), min_confidence=7)
    assert action is DeclineAction.RETRY


def test_ku_address_is_skipped_before_the_model():
    msg = make_message(sender="student@korea.ac.kr")
    assert "korea.ac.kr" in prefilter(msg, excluded_domains=("korea.ac.kr",))


def test_ku_subdomain_is_skipped_too():
    msg = make_message(sender="a@chem.korea.ac.kr")
    assert prefilter(msg, excluded_domains=("korea.ac.kr",))


@pytest.mark.parametrize("sender, extra", [
    ("noreply@journal.org", None),
    ("news@vendor.com", {"List-Unsubscribe": "<mailto:u@vendor.com>"}),
])
def test_automated_and_bulk_mail_skip_the_model(sender, extra):
    assert prefilter(make_message(sender=sender, extra=extra), excluded_domains=())


def test_personal_gmail_goes_to_the_model():
    assert prefilter(make_message(), excluded_domains=("korea.ac.kr",)) is None


# --- parsing ----------------------------------------------------------------
def test_parse_inquiry_reads_fields():
    inq = parse_inquiry('```json\n{"is_applicant": true, "role": "PostDoc", "language": "en",'
                        ' "family_name": "Khodi", "has_doctorate": "true", "confidence": 12}\n```')
    assert (inq.is_applicant, inq.role, inq.has_doctorate, inq.confidence) == \
        (True, "postdoc", True, 10)


def test_parse_inquiry_maps_unknown_role_and_language():
    inq = parse_inquiry('{"is_applicant": true, "role": "professor", "language": "fr"}')
    assert (inq.role, inq.language) == ("other", "en")


def test_parse_without_is_applicant_is_an_error():
    assert parse_inquiry('{"role": "grad"}').is_error


def test_unparseable_reply_is_an_error():
    assert parse_inquiry("I think this is an applicant").is_error


# --- MIME -------------------------------------------------------------------
def _decode(raw: str) -> email.message.EmailMessage:
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def test_draft_is_a_threaded_reply_with_signature():
    msg = make_message(extra={"References": "<a@x>"})
    mime = _decode(build_draft_mime(msg, "Dear Dr. Khodi,\n\nBody\n", SIG))
    assert mime["To"] == "khodi.sama@gmail.com"
    assert mime["Subject"] == "Re: Postdoctoral enquiry"
    assert mime["In-Reply-To"] == "<m1@mail.gmail.com>"
    assert mime["References"] == "<a@x> <m1@mail.gmail.com>"
    plain = mime.get_body(("plain",)).get_content()
    html_part = mime.get_body(("html",)).get_content()
    assert "Dr. Dongsoo Yang" in plain and "Associate Professor" in plain
    assert "gmail_signature" in html_part and "<b>Dr. Dongsoo Yang</b>" in html_part


def test_draft_prefers_reply_to_and_keeps_existing_re_prefix():
    msg = make_message(subject="RE: inquiry", extra={"Reply-To": "Real <real@uni.edu>"})
    mime = _decode(build_draft_mime(msg, "x\n", ""))
    assert mime["To"] == "real@uni.edu"
    assert mime["Subject"] == "RE: inquiry"


def test_body_html_is_escaped():
    mime = _decode(build_draft_mime(make_message(), "Dear <script>,\n", ""))
    assert "<script>" not in mime.get_body(("html",)).get_content()


# --- config -----------------------------------------------------------------
def test_shipped_config_loads_and_excludes_ku():
    cfg = load_decline_config()
    assert "korea.ac.kr" in cfg.excluded_domains
    assert cfg.start_after.tzinfo is not None
    assert cfg.templates["role_template"] == {
        "grad": "secured", "postdoc": "resources", "visiting": "resources",
        "intern": "resources"}


# --- run --------------------------------------------------------------------
class FakeGmail:
    def __init__(self, messages, *, threads=None, sent_to=(), signature=SIG):
        self._messages = {m.id: m for m in messages}
        self._threads = threads or {}
        self._sent_to = set(sent_to)
        self._signature = signature
        self.queries: list[str] = []
        self.drafts: list[tuple[str, str]] = []
        self.modifications: list[tuple[str, list[str]]] = []

    def ensure_label(self, name, *, background="", text="", hidden=False):
        return f"L_{name}"

    def search_message_ids(self, query, *, max_results=100):
        self.queries.append(query)
        return list(self._messages)[:max_results]

    def get_message(self, message_id, *, body_chars=4000):
        return self._messages[message_id]

    def thread_label_sets(self, thread_id):
        return self._threads.get(thread_id, [("INBOX",)])

    def has_sent_to(self, address):
        return address in self._sent_to

    def default_signature_html(self):
        return self._signature

    def create_draft(self, raw, *, thread_id):
        self.drafts.append((thread_id, raw))
        return "d1"

    def modify(self, message_id, *, add=None, remove=None):
        self.modifications.append((message_id, add or []))


@pytest.fixture
def cfg(tmp_path) -> DeclineConfig:
    prompt = tmp_path / "p.md"
    prompt.write_text("{sender} {subject} {body}", encoding="utf-8")
    return DeclineConfig(
        enabled=True, start_after=datetime.fromisoformat("2026-10-04T16:30:00+09:00"),
        drafted_label="거절초안", checked_label="ApplicantChecked",
        excluded_domains=("korea.ac.kr",), prompt_path=prompt, templates=TEMPLATES,
        model="stub", fallback_models=[], min_confidence=7, body_chars=100,
        parallel=2, timeout=5, max_messages_per_run=50, max_drafts_per_run=2,
    )


def stub_model(monkeypatch, replies: dict[str, str]) -> None:
    def fake(prompt, model, api_key, timeout):
        for key, reply in replies.items():
            if key in prompt:
                return reply, ""
        raise AssertionError(f"unexpected model call: {prompt[:80]}")
    monkeypatch.setattr(mod, "generate_text", fake)


POSTDOC = ('{"is_applicant": true, "role": "postdoc", "language": "en", "given_name": '
           '"Samaneh", "family_name": "Khodi", "has_doctorate": true, "confidence": 9}')
NOT_APPLICANT = '{"is_applicant": false, "role": "other", "language": "en", "confidence": 9}'


def test_applicant_gets_draft_unread_and_label(monkeypatch, cfg):
    client = FakeGmail([make_message("a")])
    stub_model(monkeypatch, {"khodi": POSTDOC})

    summary = run_decline(client, cfg, api_key="k")

    assert summary.drafted == 1
    thread_id, raw = client.drafts[0]
    assert thread_id == "ta"
    assert "Dear Dr. Khodi," in _decode(raw).get_body(("plain",)).get_content()
    assert client.modifications == [("a", ["UNREAD", "L_거절초안", "L_ApplicantChecked"])]


def test_query_never_reaches_mail_before_start(monkeypatch, cfg):
    client = FakeGmail([])
    run_decline(client, cfg, api_key="k")
    expected = int(datetime.fromisoformat("2026-10-04T16:30:00+09:00").timestamp())
    assert f"after:{expected}" in client.queries[0]
    assert "in:inbox" in client.queries[0] and "-label:ApplicantChecked" in client.queries[0]


def test_non_applicant_is_only_marked_checked(monkeypatch, cfg):
    client = FakeGmail([make_message("a", sender="colleague@snu.ac.kr", subject="meeting")])
    stub_model(monkeypatch, {"colleague": NOT_APPLICANT})

    run_decline(client, cfg, api_key="k")

    assert client.drafts == []
    assert client.modifications == [("a", ["L_ApplicantChecked"])]


def test_ku_sender_never_reaches_the_model(monkeypatch, cfg):
    client = FakeGmail([make_message("a", sender="학생 <stu@korea.ac.kr>")])
    stub_model(monkeypatch, {})  # any call raises

    summary = run_decline(client, cfg, api_key="k")

    assert (summary.drafted, summary.skipped) == (0, 1)


@pytest.mark.parametrize("threads, sent_to", [
    ({"ta": [("INBOX",), ("SENT",)]}, ()),
    ({"ta": [("INBOX",), ("DRAFT",)]}, ()),
    (None, ("khodi.sama@gmail.com",)),
])
def test_threads_he_already_engaged_with_get_no_draft(monkeypatch, cfg, threads, sent_to):
    client = FakeGmail([make_message("a")], threads=threads, sent_to=sent_to)
    stub_model(monkeypatch, {"khodi": POSTDOC})

    summary = run_decline(client, cfg, api_key="k")

    assert summary.drafted == 0 and client.drafts == []


def test_circuit_breaker_changes_nothing(monkeypatch, cfg):
    client = FakeGmail([make_message(i, sender=f"Khodi {i} <k{i}@gmail.com>") for i in "abc"])
    stub_model(monkeypatch, {"Khodi": POSTDOC})

    summary = run_decline(client, cfg, api_key="k")

    assert summary.aborted
    assert client.drafts == [] and client.modifications == []


def test_dry_run_changes_nothing(monkeypatch, cfg):
    client = FakeGmail([make_message("a")])
    stub_model(monkeypatch, {"khodi": POSTDOC})

    summary = run_decline(client, cfg, api_key="k", dry_run=True)

    assert summary.drafted == 1
    assert client.drafts == [] and client.modifications == []


def test_model_failure_leaves_message_untouched(monkeypatch, cfg):
    client = FakeGmail([make_message("a")])
    monkeypatch.setattr(mod, "generate_text", lambda *a: (None, "(gemini error)"))

    summary = run_decline(client, cfg, api_key="k")

    assert summary.retry == 1 and client.modifications == []


def test_fallback_signature_used_when_live_one_is_empty(monkeypatch, cfg):
    client = FakeGmail([make_message("a")], signature="")
    stub_model(monkeypatch, {"khodi": POSTDOC})

    run_decline(client, cfg, api_key="k")

    plain = _decode(client.drafts[0][1]).get_body(("plain",)).get_content()
    assert "Associate Professor" in plain and "yanglaboratory.com" in plain


def test_disabled_stage_does_nothing(cfg):
    client = FakeGmail([make_message("a")])
    summary = run_decline(client, replace(cfg, enabled=False), api_key="k")
    assert summary.scanned == 0 and client.queries == []


# --- review fixes (2026-10-04) ----------------------------------------------
def test_two_letters_from_one_applicant_get_one_draft(monkeypatch, cfg):
    """Mass-mailers and follow-ups: the history checks can't see this run."""
    client = FakeGmail([make_message("a"), make_message("b", subject="Follow-up")])
    stub_model(monkeypatch, {"khodi": POSTDOC})

    summary = run_decline(client, cfg, api_key="k")

    assert len(client.drafts) == 1
    assert summary.drafted == 1 and summary.skipped == 1
    assert ("b", ["L_ApplicantChecked"]) in client.modifications


def test_two_messages_in_one_thread_get_one_draft(monkeypatch, cfg):
    a = make_message("a")
    b = replace(make_message("b", sender="Other <other@gmail.com>"), thread_id=a.thread_id)
    client = FakeGmail([a, b])
    stub_model(monkeypatch, {"khodi": POSTDOC, "other": POSTDOC})

    run_decline(client, cfg, api_key="k")

    assert len(client.drafts) == 1


def test_folded_headers_do_not_crash_the_draft():
    msg = make_message(subject="Postdoctoral\r\n enquiry",
                       extra={"References": "<a@b>\r\n <c@d>",
                              "Message-ID": "<m1@x>\r\n"})
    mime = _decode(build_draft_mime(msg, "x\n", ""))
    assert mime["Subject"] == "Re: Postdoctoral enquiry"
    assert mime["References"] == "<a@b> <c@d> <m1@x>"


def test_one_failing_draft_does_not_starve_the_rest(monkeypatch, cfg):
    client = FakeGmail([make_message("a", sender="A <a@gmail.com>"),
                        make_message("b", sender="B <b@gmail.com>")])
    stub_model(monkeypatch, {"a@gmail": POSTDOC, "b@gmail": POSTDOC})
    real = client.create_draft

    def flaky(raw, *, thread_id):
        if thread_id == "ta":
            raise mod.GmailError("boom")
        return real(raw, thread_id=thread_id)
    client.create_draft = flaky

    summary = run_decline(client, cfg, api_key="k")

    assert [t for t, _ in client.drafts] == ["tb"]
    assert summary.retry == 1
    assert all(mid != "a" for mid, _ in client.modifications)  # retried next run


def test_ku_reply_to_is_excluded_even_from_gmail():
    msg = make_message(extra={"Reply-To": "학생 <stu@korea.ac.kr>"})
    assert prefilter(msg, excluded_domains=("korea.ac.kr",))


def test_reply_to_address_he_wrote_to_is_skipped(monkeypatch, cfg):
    msg = make_message("a", extra={"Reply-To": "old@uni.edu"})
    client = FakeGmail([msg], sent_to=("old@uni.edu",))
    stub_model(monkeypatch, {"khodi": POSTDOC})

    assert run_decline(client, cfg, api_key="k").drafted == 0


def test_mail_before_start_after_is_skipped_even_if_query_lets_it_through():
    msg = replace(make_message(), internal_date_ms=1_000)
    assert prefilter(msg, excluded_domains=(), start_after_ms=2_000) == \
        "received before start_after"


def test_breaker_still_marks_non_applicants(monkeypatch, cfg):
    msgs = [make_message(i, sender=f"Khodi {i} <k{i}@gmail.com>") for i in "abc"]
    msgs.append(make_message("z", sender="colleague@snu.ac.kr", subject="meeting"))
    client = FakeGmail(msgs)
    stub_model(monkeypatch, {"Khodi": POSTDOC, "colleague": NOT_APPLICANT})

    summary = run_decline(client, cfg, api_key="k")

    assert summary.aborted and client.drafts == []
    assert client.modifications == [("z", ["L_ApplicantChecked"])]


def test_has_sent_to_quotes_and_sanitises_the_address():
    from synbee_bot.spam_rescue.gmail import GmailClient
    client = GmailClient("id", "secret", "token")
    seen = []
    client.search_message_ids = lambda q, max_results=100: seen.append(q) or []
    client.has_sent_to('evil@x.com OR from:me) {"')
    assert seen == ['in:sent to:"evil@x.comORfromme"']
