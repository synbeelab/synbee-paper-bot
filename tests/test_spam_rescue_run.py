"""End-to-end run_rescue behaviour against a fake Gmail, with a stubbed model.

Checks the things that would be expensive to discover in production: that
rescued mail gets the right label moves, that kept mail is marked so it is
never re-billed, that failures are left untouched, and that the circuit
breaker refuses the whole run rather than applying half of it.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from synbee_bot.spam_rescue import rescue as rescue_mod
from synbee_bot.spam_rescue.classify import Judgment
from synbee_bot.spam_rescue.gmail import GmailMessage
from synbee_bot.spam_rescue.rescue import RescueConfig, run_rescue

SAFE_ID, CHECKED_ID = "Label_safe", "Label_checked"


class FakeGmail:
    """Records label mutations instead of performing them."""

    def __init__(self, messages: list[GmailMessage]) -> None:
        self._messages = {m.id: m for m in messages}
        self.modifications: list[tuple[str, list[str], list[str]]] = []
        self.created_labels: list[str] = []

    def ensure_label(self, name: str, *, background: str = "", text: str = "") -> str:
        self.created_labels.append(name)
        return SAFE_ID if "안전" in name else CHECKED_ID

    def list_spam_message_ids(self, *, exclude_label: str = "",
                              max_results: int = 500) -> list[str]:
        return list(self._messages)[:max_results]

    def get_message(self, message_id: str, *, body_chars: int = 4000) -> GmailMessage:
        return self._messages[message_id]

    def modify(self, message_id: str, *, add: list[str] | None = None,
               remove: list[str] | None = None) -> None:
        self.modifications.append((message_id, add or [], remove or []))


def message(msg_id: str, sender: str = "student@snu.ac.kr") -> GmailMessage:
    return GmailMessage(
        id=msg_id, thread_id=f"t{msg_id}", label_ids=("SPAM",), internal_date_ms=0,
        headers={"From": sender, "Subject": f"subject {msg_id}",
                 "Authentication-Results": "spf=pass; dkim=pass; dmarc=pass"},
        body_text="body",
    )


@pytest.fixture
def cfg(tmp_path) -> RescueConfig:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("{sender} {subject} {auth_results} {body}", encoding="utf-8")
    return RescueConfig(
        safe_label="안전함", checked_label="SpamRescueChecked",
        safe_label_bg="#b9e4d0", safe_label_fg="#0b804b",
        model="stub", fallback_models=[], prompt_path=prompt,
        min_confidence=6, body_chars=100, parallel=2, timeout=5,
        max_messages_per_run=100, max_rescues_per_run=3, max_error_ratio=0.5,
    )


def stub_classifier(monkeypatch, verdicts: dict[str, Judgment]) -> None:
    def fake(msg, template, **kwargs):
        return verdicts[msg.id]
    monkeypatch.setattr(rescue_mod, "classify", fake)


def rescue_verdict() -> Judgment:
    return Judgment(verdict="RESCUE", category="student_inquiry", confidence=9,
                    reason="인턴 지원", model_used="stub")


def spam_verdict() -> Judgment:
    return Judgment(verdict="SPAM", category="predatory", confidence=9,
                    reason="약탈적 학회", model_used="stub")


def error_verdict() -> Judgment:
    return Judgment(verdict="SPAM", category="other", confidence=0,
                    reason="(gemini error)", model_used="stub", is_error=True)


def test_rescued_mail_leaves_spam_and_gains_inbox_and_safe_label(monkeypatch, cfg):
    client = FakeGmail([message("a")])
    stub_classifier(monkeypatch, {"a": rescue_verdict()})

    summary = run_rescue(client, cfg, api_key="k")

    assert (summary.rescued, summary.kept, summary.retry) == (1, 0, 0)
    msg_id, added, removed = client.modifications[0]
    assert msg_id == "a"
    assert set(added) == {"INBOX", SAFE_ID, CHECKED_ID}
    assert removed == ["SPAM"]


def test_kept_mail_stays_in_spam_but_is_marked_checked(monkeypatch, cfg):
    client = FakeGmail([message("a")])
    stub_classifier(monkeypatch, {"a": spam_verdict()})

    summary = run_rescue(client, cfg, api_key="k")

    assert (summary.rescued, summary.kept) == (0, 1)
    assert client.modifications == [("a", [CHECKED_ID], [])]


def test_failed_classification_is_left_completely_untouched(monkeypatch, cfg):
    """No checked label — otherwise a transient API blip loses the message."""
    client = FakeGmail([message("a")])
    stub_classifier(monkeypatch, {"a": error_verdict()})

    summary = run_rescue(client, cfg, api_key="k")

    assert (summary.retry, summary.error_ratio) == (1, 1.0)
    assert client.modifications == []


def test_dry_run_changes_nothing(monkeypatch, cfg):
    client = FakeGmail([message("a"), message("b")])
    stub_classifier(monkeypatch, {"a": rescue_verdict(), "b": spam_verdict()})

    summary = run_rescue(client, cfg, api_key="k", dry_run=True)

    assert summary.rescued == 1
    assert client.modifications == []


def test_circuit_breaker_applies_nothing_at_all(monkeypatch, cfg):
    """Over the cap it must refuse the run, not rescue the first N."""
    ids = ["a", "b", "c", "d"]
    client = FakeGmail([message(i) for i in ids])
    stub_classifier(monkeypatch, {i: rescue_verdict() for i in ids})

    summary = run_rescue(client, replace(cfg, max_rescues_per_run=3), api_key="k")

    assert summary.aborted is True
    assert summary.rescued == 4
    assert client.modifications == []
    assert "max_rescues_per_run" in summary.abort_reason


def test_run_at_the_cap_is_allowed(monkeypatch, cfg):
    ids = ["a", "b", "c"]
    client = FakeGmail([message(i) for i in ids])
    stub_classifier(monkeypatch, {i: rescue_verdict() for i in ids})

    summary = run_rescue(client, replace(cfg, max_rescues_per_run=3), api_key="k")

    assert summary.aborted is False
    assert len(client.modifications) == 3


def test_empty_spam_folder_is_a_clean_no_op(monkeypatch, cfg):
    client = FakeGmail([])
    stub_classifier(monkeypatch, {})

    summary = run_rescue(client, cfg, api_key="k")

    assert (summary.scanned, summary.rescued, summary.error_ratio) == (0, 0, 0.0)
    assert client.modifications == []


def test_mixed_batch_routes_each_message_correctly(monkeypatch, cfg):
    client = FakeGmail([message("a"), message("b"), message("c")])
    stub_classifier(monkeypatch, {
        "a": rescue_verdict(), "b": spam_verdict(), "c": error_verdict(),
    })

    summary = run_rescue(client, cfg, api_key="k")

    assert (summary.rescued, summary.kept, summary.retry) == (1, 1, 1)
    touched = {mid for mid, _, _ in client.modifications}
    assert touched == {"a", "b"}


def test_both_labels_are_ensured_before_any_modification(monkeypatch, cfg):
    client = FakeGmail([message("a")])
    stub_classifier(monkeypatch, {"a": rescue_verdict()})

    run_rescue(client, cfg, api_key="k")

    assert client.created_labels == ["SpamRescueChecked", "안전함"]
