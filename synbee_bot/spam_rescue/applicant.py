"""Draft polite declines for unsolicited lab-position inquiries.

Runs right after the spam rescue in the same job, over the inbox only —
applicant mail the rescue just pulled out of spam is already there. It never
sends anything: it saves a reply draft in the applicant's thread, marks the
inquiry UNREAD and labels it, so he decides in Gmail whether to send.

The model only classifies (applicant? which role? which language? name?).
The draft body is his own template text, filled in by code.
"""
from __future__ import annotations

import base64
import html
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .classify import generate_text, render_prompt
from .gmail import GmailClient, GmailError, GmailMessage

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "spam_rescue.yml"

ROLES = ("grad", "postdoc", "visiting", "intern")
_AUTOMATED_LOCALPART = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?)\b", re.I
)


class DeclineAction(str, Enum):
    DRAFT = "DRAFT"   # decline drafted, inquiry marked unread + labelled
    SKIP = "SKIP"     # not an applicant (or excluded) — marked checked only
    RETRY = "RETRY"   # classification failed — untouched, retried next run


@dataclass(frozen=True)
class Inquiry:
    is_applicant: bool
    role: str
    language: str
    full_name: str = ""
    given_name: str = ""
    family_name: str = ""
    has_doctorate: bool = False
    ku_affiliated: bool = False
    is_referral: bool = False
    confidence: int = 0
    reason: str = ""
    is_error: bool = False


def _inquiry_error(reason: str) -> Inquiry:
    return Inquiry(is_applicant=False, role="other", language="en",
                   reason=reason[:200], is_error=True)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def parse_inquiry(text: str) -> Inquiry:
    """Pull the JSON object out of the model's reply."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        return _inquiry_error(f"(parse failed: no JSON) {cleaned[:120]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return _inquiry_error(f"(parse failed: {exc})")
    if "is_applicant" not in data:
        return _inquiry_error("(parse failed: no is_applicant)")
    try:
        confidence = int(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    role = str(data.get("role", "other")).strip().lower()
    language = str(data.get("language", "en")).strip().lower()
    return Inquiry(
        is_applicant=_as_bool(data.get("is_applicant")),
        role=role if role in ROLES else "other",
        language="ko" if language == "ko" else "en",
        full_name=str(data.get("full_name") or "").strip(),
        given_name=str(data.get("given_name") or "").strip(),
        family_name=str(data.get("family_name") or "").strip(),
        has_doctorate=_as_bool(data.get("has_doctorate")),
        ku_affiliated=_as_bool(data.get("ku_affiliated")),
        is_referral=_as_bool(data.get("is_referral")),
        confidence=max(0, min(10, confidence)),
        reason=str(data.get("reason", "")).strip()[:200],
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeclineConfig:
    enabled: bool
    start_after: datetime          # mail received before this is never touched
    drafted_label: str
    checked_label: str
    excluded_domains: tuple[str, ...]
    prompt_path: Path
    templates: dict[str, Any]
    model: str
    fallback_models: list[str]
    min_confidence: int
    body_chars: int
    parallel: int
    timeout: int
    max_messages_per_run: int
    max_drafts_per_run: int


def load_decline_config(path: Path | None = None) -> DeclineConfig:
    cfg_path = path or DEFAULT_CONFIG
    with cfg_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    llm = raw.get("llm", {}) or {}
    sec = raw.get("applicant_decline", {}) or {}

    start = sec.get("start_after")
    if not start:
        raise ValueError("applicant_decline.start_after is required — it is "
                         "what keeps old inquiries from being drafted.")
    start_after = start if isinstance(start, datetime) else datetime.fromisoformat(str(start))
    if start_after.tzinfo is None:
        raise ValueError("applicant_decline.start_after needs a UTC offset")

    templates_path = PROJECT_ROOT / str(sec.get("templates_path",
                                                "config/applicant_decline_templates.yml"))
    with templates_path.open("r", encoding="utf-8") as handle:
        templates = yaml.safe_load(handle) or {}

    return DeclineConfig(
        enabled=bool(sec.get("enabled", True)),
        start_after=start_after,
        drafted_label=str(sec.get("drafted_label", "거절초안")),
        checked_label=str(sec.get("checked_label", "ApplicantChecked")),
        excluded_domains=tuple(str(d).lower().lstrip("@")
                               for d in (sec.get("excluded_domains") or [])),
        prompt_path=PROJECT_ROOT / str(sec.get("prompt_path",
                                               "config/applicant_decline_prompt.md")),
        templates=templates,
        model=str(sec.get("model", llm.get("model", "gemini-2.5-flash"))),
        fallback_models=[str(m) for m in (sec.get("fallback_models")
                                          or llm.get("fallback_models") or [])],
        min_confidence=int(sec.get("min_confidence", 7)),
        body_chars=int(sec.get("body_chars", llm.get("body_chars", 3000))),
        parallel=int(sec.get("parallel_requests", llm.get("parallel_requests", 4))),
        timeout=int(sec.get("timeout_seconds", llm.get("timeout_seconds", 30))),
        max_messages_per_run=int(sec.get("max_messages_per_run", 100)),
        max_drafts_per_run=int(sec.get("max_drafts_per_run", 10)),
    )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def _in_domain(domain: str, excluded: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in excluded)


def _one_line(value: str) -> str:
    """Unfold a header value — Gmail can hand back folded CRLF headers, which
    EmailMessage refuses to re-emit."""
    return " ".join(value.split())


def reply_address(msg: GmailMessage) -> str:
    """Where a reply goes: the Reply-To address if set, else From."""
    reply_to = parseaddr(_one_line(msg.headers.get("Reply-To", "")))[1].lower()
    return reply_to if "@" in reply_to else msg.sender_address


def prefilter(msg: GmailMessage, *, excluded_domains: tuple[str, ...],
              start_after_ms: int = 0) -> str | None:
    """Reason to skip without spending a model call, or None to classify."""
    if start_after_ms and msg.internal_date_ms < start_after_ms:
        return "received before start_after"
    if not msg.sender_address:
        return "no sender address"
    # Check every address the draft could involve: the From that the rules
    # are about and the Reply-To the draft would actually go to.
    for address in {msg.sender_address, reply_address(msg)}:
        domain = address.rsplit("@", 1)[-1]
        if _in_domain(domain, excluded_domains):
            return f"excluded domain {domain}"
        if _AUTOMATED_LOCALPART.match(address.split("@", 1)[0]):
            return "automated sender"
    if msg.headers.get("List-Unsubscribe"):
        return "bulk mail (List-Unsubscribe)"
    return None


def decide_inquiry(inq: Inquiry, *, min_confidence: int) -> tuple[DeclineAction, str]:
    if inq.is_error:
        return DeclineAction.RETRY, f"classification failed — {inq.reason}"
    if not inq.is_applicant:
        return DeclineAction.SKIP, f"not applicant: {inq.reason}"
    if inq.ku_affiliated:
        return DeclineAction.SKIP, "Korea University applicant — left to him"
    if inq.is_referral:
        return DeclineAction.SKIP, "referral by a third party — left to him"
    if inq.role not in ROLES:
        return DeclineAction.SKIP, f"applicant with unclear role: {inq.reason}"
    if inq.confidence < min_confidence:
        return DeclineAction.SKIP, (
            f"confidence {inq.confidence} < {min_confidence} ({inq.role}: {inq.reason})"
        )
    return DeclineAction.DRAFT, f"{inq.role}/{inq.language}: {inq.reason}"


# ---------------------------------------------------------------------------
# Draft text
# ---------------------------------------------------------------------------
def _fill(template: str, values: dict[str, str]) -> str:
    """Single-pass placeholder fill — inserted names are never re-expanded."""
    return re.sub(r"\{(\w+)\}",
                  lambda m: values.get(m.group(1), m.group(0)), template)


def salutation(inq: Inquiry, templates: dict[str, Any]) -> str:
    forms = templates["salutation"][inq.language]
    names = {"full_name": inq.full_name, "given_name": inq.given_name,
             "family_name": inq.family_name}
    if inq.language == "ko":
        if inq.full_name:
            key = "doctor" if inq.has_doctorate else "named"
            return _fill(forms[key], names)
        return forms["fallback"]
    if inq.has_doctorate and inq.family_name:
        return _fill(forms["doctor"], names)
    if inq.given_name:
        return _fill(forms["named"], names)
    return forms["fallback"]


def render_decline(inq: Inquiry, templates: dict[str, Any]) -> str:
    """Plain-text body (without signature) for an applicant."""
    kind = templates["role_template"][inq.role]
    body = templates["templates"][inq.language][kind]
    return _fill(body, {
        "salutation": salutation(inq, templates),
        "role": templates["role_phrases"][inq.language][inq.role],
    }).strip() + "\n"


def _html_to_text(fragment: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</div>|</p>", "\n", fragment)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", "", text))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def build_draft_mime(msg: GmailMessage, body_text: str, signature_html: str) -> str:
    """base64url RFC 822 reply to `msg`, plain + HTML, signature appended."""
    subject = _one_line(msg.headers.get("Subject", ""))
    if not re.match(r"(?i)^re:", subject):
        subject = f"Re: {subject}".strip()

    mime = EmailMessage()
    mime["To"] = reply_address(msg)
    mime["Subject"] = subject
    original_id = _one_line(msg.message_id_header)
    if original_id:
        mime["In-Reply-To"] = original_id
        references = _one_line(msg.headers.get("References", ""))
        mime["References"] = f"{references} {original_id}".strip()

    sig_text = _html_to_text(signature_html) if signature_html else ""
    plain = body_text + (f"\n{sig_text}\n" if sig_text else "")
    body_html = "".join(
        f"<div>{html.escape(line)}</div>" if line else "<div><br></div>"
        for line in body_text.rstrip("\n").split("\n")
    )
    if signature_html:
        body_html += ("<div><br></div><div dir=\"ltr\" class=\"gmail_signature\" "
                      f"data-smartmail=\"gmail_signature\">{signature_html}</div>")
    mime.set_content(plain)
    mime.add_alternative(f"<div dir=\"ltr\">{body_html}</div>", subtype="html")
    return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeclineDecision:
    message: GmailMessage
    action: DeclineAction
    note: str
    inquiry: Inquiry | None = None


@dataclass
class DeclineSummary:
    scanned: int = 0
    drafted: int = 0
    skipped: int = 0
    retry: int = 0
    aborted: bool = False
    abort_reason: str = ""

    @property
    def error_ratio(self) -> float:
        return self.retry / self.scanned if self.scanned else 0.0


def _log(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _short(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _already_handled(client: GmailClient, msg: GmailMessage) -> str | None:
    """Reason the thread needs no draft — he already engaged with it."""
    for labels in client.thread_label_sets(msg.thread_id):
        if "SENT" in labels:
            return "he already replied in this thread"
        if "DRAFT" in labels:
            return "a draft already exists in this thread"
    for address in {msg.sender_address, reply_address(msg)}:
        if client.has_sent_to(address):
            return f"he has written to {address} before"
    return None


def _screen_drafts(client: GmailClient,
                   decisions: list[DeclineDecision]) -> list[DeclineDecision]:
    """Second pass over would-be drafts: history checks + one draft per person.

    The history checks only see state from before this run, so a mass-mailer's
    two letters (or a letter and its follow-up) would both pass them; the
    in-run dedupe by thread and by address closes that gap.
    """
    seen_threads: set[str] = set()
    seen_addresses: set[str] = set()
    screened = []
    for d in decisions:
        if d.action is not DeclineAction.DRAFT:
            screened.append(d)
            continue
        addresses = {d.message.sender_address, reply_address(d.message)}
        reason = None
        if d.message.thread_id in seen_threads or addresses & seen_addresses:
            reason = "duplicate of another inquiry drafted in this run"
        else:
            try:
                reason = _already_handled(client, d.message)
            except GmailError as exc:
                screened.append(replace(d, action=DeclineAction.RETRY,
                                        note=f"history check failed — {exc}"))
                continue
        if reason:
            screened.append(replace(d, action=DeclineAction.SKIP, note=reason))
            continue
        seen_threads.add(d.message.thread_id)
        seen_addresses |= addresses
        screened.append(d)
    return screened


def _signature(client: GmailClient, cfg: DeclineConfig) -> str:
    try:
        live = client.default_signature_html().strip()
    except GmailError as exc:
        _log(f"[decline] could not read Gmail signature ({exc}); using fallback")
        live = ""
    return live or str(cfg.templates.get("signature_fallback", "")).strip()


def _fetch(client: GmailClient, ids: list[str], body_chars: int) -> list[GmailMessage]:
    """Fetch messages, dropping any that vanish mid-run (retried next run)."""
    messages = []
    for mid in ids:
        try:
            messages.append(client.get_message(mid, body_chars=body_chars))
        except GmailError as exc:
            _log(f"[decline] could not fetch {mid}: {exc}")
    return messages


def _classifier(cfg: DeclineConfig, api_key: str):
    template = cfg.prompt_path.read_text(encoding="utf-8")
    start_ms = int(cfg.start_after.timestamp() * 1000)

    def judge(msg: GmailMessage) -> DeclineDecision:
        skip = prefilter(msg, excluded_domains=cfg.excluded_domains,
                         start_after_ms=start_ms)
        if skip:
            return DeclineDecision(msg, DeclineAction.SKIP, skip)
        prompt = render_prompt(template, msg, body_chars=cfg.body_chars)
        inq = _inquiry_error("(no model attempted)")
        for model in [cfg.model, *cfg.fallback_models]:
            text, err = generate_text(prompt, model, api_key, cfg.timeout)
            inq = parse_inquiry(text) if text is not None else _inquiry_error(err)
            if not inq.is_error:
                break
        action, note = decide_inquiry(inq, min_confidence=cfg.min_confidence)
        return DeclineDecision(msg, action, note, inq)

    return judge


def _apply(client: GmailClient, cfg: DeclineConfig, decisions: list[DeclineDecision],
           *, checked_id: str, drafted_id: str, write_drafts: bool) -> int:
    """Write drafts and labels; each message isolated. Returns failure count.

    With `write_drafts` False (circuit breaker tripped) only SKIP verdicts are
    marked, so those never cost another model call; DRAFT candidates stay
    unmarked for a human to look at.
    """
    wants_draft = write_drafts and any(d.action is DeclineAction.DRAFT for d in decisions)
    signature = _signature(client, cfg) if wants_draft else ""
    failures = 0
    for d in decisions:
        try:
            if d.action is DeclineAction.SKIP:
                client.modify(d.message.id, add=[checked_id])
            elif d.action is DeclineAction.DRAFT and write_drafts and d.inquiry:
                body = render_decline(d.inquiry, cfg.templates)
                raw = build_draft_mime(d.message, body, signature)
                client.create_draft(raw, thread_id=d.message.thread_id)
                client.modify(d.message.id, add=["UNREAD", drafted_id, checked_id])
        except (GmailError, ValueError) as exc:
            failures += 1
            _log(f"[decline] {d.action.value} failed for {d.message.id}: {exc}")
    return failures


def run_decline(client: GmailClient, cfg: DeclineConfig, *, api_key: str,
                dry_run: bool = False) -> DeclineSummary:
    """Draft declines for new applicant inquiries in the inbox."""
    summary = DeclineSummary()
    if not cfg.enabled:
        _log("[decline] disabled in config")
        return summary

    checked_id = client.ensure_label(cfg.checked_label, hidden=True)
    drafted_id = client.ensure_label(cfg.drafted_label)

    query = (f"in:inbox after:{int(cfg.start_after.timestamp())} "
             f"-from:me -label:{cfg.checked_label}")
    ids = client.search_message_ids(query, max_results=cfg.max_messages_per_run)
    _log(f"[decline] {len(ids)} unchecked inbox message(s)")
    messages = _fetch(client, ids, cfg.body_chars * 2)
    if not messages:
        return summary

    with ThreadPoolExecutor(max_workers=cfg.parallel) as pool:
        decisions = _screen_drafts(client, list(pool.map(_classifier(cfg, api_key),
                                                         messages)))

    summary.scanned = len(decisions)
    summary.drafted = sum(d.action is DeclineAction.DRAFT for d in decisions)
    summary.skipped = sum(d.action is DeclineAction.SKIP for d in decisions)
    summary.retry = sum(d.action is DeclineAction.RETRY for d in decisions)
    for d in sorted(decisions, key=lambda d: d.action.value):
        _log(f"  {d.action.value:<5} {_short(d.message.sender, 42):<42} "
             f"{_short(d.message.subject, 52):<52} | {d.note}")

    if dry_run:
        _log("[decline:dry-run] no drafts written, no labels changed")
        return summary

    write_drafts = summary.drafted <= cfg.max_drafts_per_run
    if not write_drafts:
        summary.aborted = True
        summary.abort_reason = (
            f"{summary.drafted} drafts exceeds max_drafts_per_run="
            f"{cfg.max_drafts_per_run}; no drafts written (non-applicants were "
            f"still marked checked)."
        )
        _log(f"[decline:abort] {summary.abort_reason}")

    failures = _apply(client, cfg, decisions, checked_id=checked_id,
                      drafted_id=drafted_id, write_drafts=write_drafts)
    summary.retry += failures
    _log(f"[decline:done] drafted={summary.drafted if write_drafts else 0} "
         f"skipped={summary.skipped} retry={summary.retry} failures={failures} "
         f"(scanned {summary.scanned})")
    return summary

