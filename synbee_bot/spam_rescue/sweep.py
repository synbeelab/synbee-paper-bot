"""Inbox → spam: learn from his own "Report spam" clicks, then act on them.

The rescue job only ever moves mail *out* of spam. This module is the other
direction, and it is deliberately narrow: no model call decides that inbox
mail is spam. A message is moved only when its sender is on a list he has
vouched for —

* `never_rescue.senders` in config/spam_rescue.yml (institutional and domain
  entries; this repo is public, so no personal addresses go there), and
* the learned registry: one hidden Gmail label `SpamSenders/<address>` per
  sender he has himself moved from the inbox to spam.

Everything is stateless across runs — the state lives in Gmail labels:

    InboxSeen        stamped on inbox mail, so a later move to spam is
                     recognisable as *his* move rather than Gmail's filter
    SpamLearned      a manual move already processed
    SpamSwept        moved to spam by this job
    SpamSweepUndone  he pulled a swept message back out — never sweep it again
    SpamSenders/...  the registry; deleting one in Gmail unlearns that sender

Stage order in one run: learn → (rescue) → undo check → sweep → stamp.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .gmail import GmailClient
from .rescue import DEFAULT_CONFIG, match_sender

# Gmail search terms per from:(…) clause — long OR chains get truncated.
_QUERY_CHUNK = 10


@dataclass(frozen=True)
class SweepConfig:
    enabled: bool
    seen_label: str
    learned_label: str
    swept_label: str
    undone_label: str
    registry_prefix: str
    lookback_days: int
    stamp_lookback_days: int
    max_moves_per_run: int
    max_learn_per_run: int
    seed_senders: tuple[str, ...] = ()


def load_sweep_config(path: Path | None = None) -> SweepConfig:
    with (path or DEFAULT_CONFIG).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    sweep = raw.get("inbox_sweep", {}) or {}
    never = raw.get("never_rescue", {}) or {}
    return SweepConfig(
        enabled=bool(sweep.get("enabled", False)),
        seen_label=str(sweep.get("seen_label", "InboxSeen")),
        learned_label=str(sweep.get("learned_label", "SpamLearned")),
        swept_label=str(sweep.get("swept_label", "SpamSwept")),
        undone_label=str(sweep.get("undone_label", "SpamSweepUndone")),
        registry_prefix=str(sweep.get("registry_prefix", "SpamSenders")),
        lookback_days=int(sweep.get("lookback_days", 90)),
        stamp_lookback_days=int(sweep.get("stamp_lookback_days", 14)),
        max_moves_per_run=int(sweep.get("max_moves_per_run", 30)),
        max_learn_per_run=int(sweep.get("max_learn_per_run", 20)),
        seed_senders=tuple(str(x).strip().lower()
                           for x in (never.get("senders") or [])),
    )


@dataclass
class SweepSummary:
    learned: list[str] = field(default_factory=list)
    unlearned: list[str] = field(default_factory=list)
    moved: int = 0
    stamped: int = 0
    aborted: bool = False
    abort_reason: str = ""


def _log(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Pure decisions (unit-tested)
# ---------------------------------------------------------------------------
def learn_verdict(address: str, *, own: tuple[str, ...], known: tuple[str, ...],
                  has_sent_to: bool) -> tuple[bool, str]:
    """Should a sender he moved to spam be registered? Returns (learn, why).

    Conservative by construction: only an exact address is ever learned
    (never a domain), and never someone he has written to — one misclick on
    a colleague's mail must not silence that colleague for good.
    """
    if not address or "@" not in address:
        return False, "no usable From address"
    if address in own:
        return False, "his own address"
    hit = match_sender(address, known)
    if hit:
        return False, f"already listed ({hit})"
    if has_sent_to:
        return False, "he has sent mail to this address — not learning"
    return True, "learned"


def sweep_verdict(address: str, label_ids: tuple[str, ...],
                  thread_labels: list[tuple[str, ...]], *,
                  senders: tuple[str, ...]) -> tuple[bool, str]:
    """Should this inbox message move to spam? Returns (move, why)."""
    hit = match_sender(address, senders)
    if not hit:
        # Gmail's from: also matches display names; the header must agree.
        return False, "From address not on the list"
    if "STARRED" in label_ids:
        return False, "starred"
    if any("SENT" in labels for labels in thread_labels):
        return False, "he replied in this thread"
    return True, f"listed sender ({hit})"


def registry_address(label_name: str, prefix: str) -> str | None:
    head = f"{prefix}/"
    if not label_name.startswith(head):
        return None
    address = label_name[len(head):].strip().lower()
    return address if "@" in address else None


def from_clauses(senders: tuple[str, ...]) -> list[str]:
    """Gmail `from:(a OR b …)` clauses, `@domain` entries as bare domains."""
    terms = [s[1:] if s.startswith("@") else s for s in senders if s]
    return [
        "from:(" + " OR ".join(terms[i:i + _QUERY_CHUNK]) + ")"
        for i in range(0, len(terms), _QUERY_CHUNK)
    ]


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
class InboxSweep:
    def __init__(self, client: GmailClient, cfg: SweepConfig, *,
                 checked_label_id: str, dry_run: bool) -> None:
        self.client = client
        self.cfg = cfg
        self.checked_id = checked_label_id
        self.dry_run = dry_run
        self.summary = SweepSummary()
        self.own = client.own_addresses()
        self.ids = {
            name: client.ensure_label(name, hidden=True)
            for name in (cfg.seen_label, cfg.learned_label,
                         cfg.swept_label, cfg.undone_label, cfg.registry_prefix)
        }
        self.registry = self._read_registry()

    def _read_registry(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for name, label_id in self.client.list_labels().items():
            address = registry_address(name, self.cfg.registry_prefix)
            if address:
                found[address] = label_id
        return found

    @property
    def senders(self) -> tuple[str, ...]:
        """Seed list plus every learned sender (this run's included)."""
        return tuple(dict.fromkeys((*self.cfg.seed_senders, *self.registry)))

    # -- 1. learn ---------------------------------------------------------
    def learn(self) -> None:
        cfg = self.cfg
        query = (f"in:spam label:{cfg.seen_label} -label:{cfg.learned_label} "
                 f"-label:{cfg.swept_label}")
        ids = self.client.search_message_ids(
            query, max_results=cfg.max_learn_per_run, include_spam=True)
        _log(f"[learn] {len(ids)} message(s) he moved from inbox to spam")
        for mid in ids:
            msg = self.client.get_message(mid, body_chars=0)
            address = msg.sender_address
            sent = bool(address) and self.client.has_sent_to(address)
            learn, why = learn_verdict(address, own=self.own,
                                       known=self.senders, has_sent_to=sent)
            _log(f"  {'LEARN' if learn else 'SKIP':<6} {address or '?':<42} | {why}")
            if self.dry_run:
                if learn:
                    self.registry[address] = "(dry-run)"
                    self.summary.learned.append(address)
                continue
            if learn:
                name = f"{cfg.registry_prefix}/{address}"
                self.registry[address] = self.client.ensure_label(name, hidden=True)
                self.summary.learned.append(address)
            # His move is final either way: mark it so the rescue stage never
            # pulls it back out of spam behind his back.
            self.client.modify(mid, add=[self.ids[cfg.learned_label], self.checked_id])

    # -- 2. undo check ----------------------------------------------------
    def honour_undos(self) -> None:
        """A swept message back in the inbox means we got that sender wrong."""
        cfg = self.cfg
        ids = self.client.search_message_ids(
            f"in:inbox label:{cfg.swept_label}", max_results=50)
        for mid in ids:
            msg = self.client.get_message(mid, body_chars=0)
            address = msg.sender_address
            label_id = self.registry.get(address)
            if label_id:
                note = "unlearned"
            elif match_sender(address, cfg.seed_senders):
                note = "on the config list — remove it there by hand"
            else:
                note = "not listed"
            _log(f"[undo] {address or '?'} put back in inbox | {note}")
            if self.dry_run:
                continue
            if label_id:
                self.client.delete_label(label_id)
                self.registry.pop(address, None)
                self.summary.unlearned.append(address)
            self.client.modify(mid, add=[self.ids[cfg.undone_label]],
                               remove=[self.ids[cfg.swept_label]])

    # -- 3. sweep ---------------------------------------------------------
    def sweep(self) -> None:
        cfg = self.cfg
        senders = self.senders
        candidates: dict[str, None] = {}
        for clause in from_clauses(senders):
            query = (f"in:inbox newer_than:{cfg.lookback_days}d {clause} "
                     f"-label:{cfg.undone_label}")
            for mid in self.client.search_message_ids(query, max_results=200):
                candidates[mid] = None

        moves = []
        for mid in candidates:
            msg = self.client.get_message(mid, body_chars=0)
            thread = self.client.thread_label_sets(msg.thread_id)
            move, why = sweep_verdict(msg.sender_address, msg.label_ids, thread,
                                      senders=senders)
            _log(f"  {'MOVE' if move else 'STAY':<6} {msg.sender_address:<42} "
                 f"{msg.subject[:50]:<50} | {why}")
            if move:
                moves.append(mid)
        _log(f"[sweep] {len(candidates)} inbox candidate(s), {len(moves)} to move")

        if len(moves) > cfg.max_moves_per_run:
            self.summary.aborted = True
            self.summary.abort_reason = (
                f"{len(moves)} moves exceeds max_moves_per_run="
                f"{cfg.max_moves_per_run}; nothing was moved.")
            _log(f"[abort] {self.summary.abort_reason}")
            return
        if self.dry_run:
            return
        for mid in moves:
            self.client.modify(
                mid, add=["SPAM", self.ids[cfg.swept_label], self.checked_id],
                remove=["INBOX"])
        self.summary.moved = len(moves)

    # -- 4. stamp ---------------------------------------------------------
    def stamp(self) -> None:
        cfg = self.cfg
        ids = self.client.search_message_ids(
            f"in:inbox newer_than:{cfg.stamp_lookback_days}d -label:{cfg.seen_label}",
            max_results=2000)
        _log(f"[stamp] {len(ids)} inbox message(s) newly marked {cfg.seen_label}")
        if ids and not self.dry_run:
            self.client.batch_modify(ids, add=[self.ids[cfg.seen_label]])
        self.summary.stamped = len(ids)
