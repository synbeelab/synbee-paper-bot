"""Decision logic and run orchestration for the spam-folder rescue."""
from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml

from .classify import Judgment, classify
from .gmail import GmailClient, GmailMessage

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "spam_rescue.yml"


class Action(str, Enum):
    RESCUE = "RESCUE"     # out of spam, into the inbox, tagged safe
    KEEP = "KEEP"         # stays in spam, marked so we never pay for it twice
    RETRY = "RETRY"       # classification failed — leave untouched for tomorrow


@dataclass(frozen=True)
class RescueConfig:
    safe_label: str
    checked_label: str
    safe_label_bg: str
    safe_label_fg: str
    model: str
    fallback_models: list[str]
    prompt_path: Path
    min_confidence: int
    body_chars: int
    parallel: int
    timeout: int
    max_messages_per_run: int
    max_rescues_per_run: int
    max_error_ratio: float


def load_rescue_config(path: Path | None = None) -> RescueConfig:
    cfg_path = path or DEFAULT_CONFIG
    with cfg_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    labels = raw.get("labels", {}) or {}
    llm = raw.get("llm", {}) or {}
    limits = raw.get("limits", {}) or {}

    return RescueConfig(
        safe_label=str(labels.get("safe", "안전함")),
        checked_label=str(labels.get("checked", "SpamRescueChecked")),
        safe_label_bg=str(labels.get("safe_background", "#b9e4d0")),
        safe_label_fg=str(labels.get("safe_text", "#0b804b")),
        model=str(llm.get("model", "gemini-2.5-flash")),
        fallback_models=[str(m) for m in (llm.get("fallback_models") or [])],
        prompt_path=PROJECT_ROOT / str(llm.get("prompt_path", "config/spam_rescue_prompt.md")),
        min_confidence=int(llm.get("min_confidence", 6)),
        body_chars=int(llm.get("body_chars", 3000)),
        parallel=int(llm.get("parallel_requests", 4)),
        timeout=int(llm.get("timeout_seconds", 30)),
        max_messages_per_run=int(limits.get("max_messages_per_run", 300)),
        max_rescues_per_run=int(limits.get("max_rescues_per_run", 40)),
        max_error_ratio=float(limits.get("max_error_ratio", 0.5)),
    )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def fails_all_authentication(msg: GmailMessage) -> bool:
    """True when SPF, DKIM and DMARC all explicitly failed.

    Any one of these can fail on legitimate mail — mailing lists break SPF,
    misconfigured university relays break DKIM — so a single failure must not
    veto a rescue or we would drop exactly the student inquiries this job
    exists to save. All three failing together is not something real
    correspondence does, and it is the signature of a spoofed sender.
    """
    auth = msg.auth_results.lower()
    if auth in ("", "(none)"):
        return False
    return all(
        re.search(rf"\b{mechanism}=(?:fail|softfail|permerror)\b", auth)
        for mechanism in ("spf", "dkim", "dmarc")
    )


def decide(msg: GmailMessage, judgment: Judgment, *, min_confidence: int) -> tuple[Action, str]:
    """Map a judgment onto an action. Returns (action, human-readable note)."""
    if judgment.is_error:
        return Action.RETRY, f"classification failed — {judgment.reason}"
    if not judgment.is_rescue:
        return Action.KEEP, f"{judgment.category}: {judgment.reason}"
    if fails_all_authentication(msg):
        return Action.KEEP, (
            f"spoof guard — SPF/DKIM/DMARC all fail, overriding "
            f"RESCUE({judgment.category})"
        )
    if judgment.confidence < min_confidence:
        return Action.KEEP, (
            f"confidence {judgment.confidence} < {min_confidence} "
            f"({judgment.category}: {judgment.reason})"
        )
    return Action.RESCUE, f"{judgment.category}: {judgment.reason}"


@dataclass(frozen=True)
class Decision:
    message: GmailMessage
    judgment: Judgment
    action: Action
    note: str


@dataclass
class RunSummary:
    scanned: int = 0
    rescued: int = 0
    kept: int = 0
    retry: int = 0
    aborted: bool = False
    abort_reason: str = ""

    @property
    def error_ratio(self) -> float:
        return self.retry / self.scanned if self.scanned else 0.0


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def _log(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _short(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def run_rescue(client: GmailClient, cfg: RescueConfig, *, api_key: str,
               dry_run: bool = False) -> RunSummary:
    """Screen the spam folder once and apply the resulting label changes."""
    summary = RunSummary()

    checked_label_id = client.ensure_label(cfg.checked_label)
    safe_label_id = client.ensure_label(
        cfg.safe_label, background=cfg.safe_label_bg, text=cfg.safe_label_fg
    )

    message_ids = client.list_spam_message_ids(
        exclude_label=cfg.checked_label, max_results=cfg.max_messages_per_run
    )
    _log(f"[scan] {len(message_ids)} unchecked spam message(s)")
    if not message_ids:
        return summary

    messages = [client.get_message(mid, body_chars=cfg.body_chars * 2)
                for mid in message_ids]

    template = cfg.prompt_path.read_text(encoding="utf-8")

    def judge(msg: GmailMessage) -> Decision:
        judgment = classify(
            msg, template, model=cfg.model,
            fallback_models=cfg.fallback_models, api_key=api_key,
            body_chars=cfg.body_chars, timeout=cfg.timeout,
        )
        action, note = decide(msg, judgment, min_confidence=cfg.min_confidence)
        return Decision(message=msg, judgment=judgment, action=action, note=note)

    with ThreadPoolExecutor(max_workers=cfg.parallel) as pool:
        decisions = list(pool.map(judge, messages))

    summary.scanned = len(decisions)
    summary.rescued = sum(d.action is Action.RESCUE for d in decisions)
    summary.kept = sum(d.action is Action.KEEP for d in decisions)
    summary.retry = sum(d.action is Action.RETRY for d in decisions)

    for decision in sorted(decisions, key=lambda d: d.action.value):
        _log(
            f"  {decision.action.value:<6} "
            f"{_short(decision.message.sender, 42):<42} "
            f"{_short(decision.message.subject, 52):<52} "
            f"| {decision.note}"
        )

    # A prompt or model malfunction should never be able to dump the whole
    # spam folder into the inbox. Refuse the entire run rather than truncating
    # the list — a partial apply would silently leave the rest unprocessed.
    if summary.rescued > cfg.max_rescues_per_run:
        summary.aborted = True
        summary.abort_reason = (
            f"{summary.rescued} rescues exceeds max_rescues_per_run="
            f"{cfg.max_rescues_per_run}; nothing was modified. Inspect with "
            f"--dry-run, then re-run with --max-rescues N if the list is right."
        )
        _log(f"[abort] {summary.abort_reason}")
        return summary

    if dry_run:
        _log("[dry-run] no labels changed")
        return summary

    for decision in decisions:
        if decision.action is Action.RETRY:
            continue  # untouched and unmarked → re-evaluated on the next run
        if decision.action is Action.RESCUE:
            client.modify(
                decision.message.id,
                add=["INBOX", safe_label_id, checked_label_id],
                remove=["SPAM"],
            )
        else:
            client.modify(decision.message.id, add=[checked_label_id])

    _log(
        f"[done] rescued={summary.rescued} kept={summary.kept} "
        f"retry={summary.retry} (scanned {summary.scanned})"
    )
    return summary
