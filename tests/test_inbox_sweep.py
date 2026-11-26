"""Inbox → spam sweep: the part that could hide real mail from him."""
from __future__ import annotations

from dataclasses import replace

from synbee_bot.spam_rescue.gmail import GmailMessage
from synbee_bot.spam_rescue.rescue import match_sender
from synbee_bot.spam_rescue.sweep import (
    InboxSweep, SweepConfig, from_clauses, learn_verdict, registry_address,
    sweep_verdict,
)

OWN = ("prof@uni.ac.kr", "prof.alias@gmail.com")

CFG = SweepConfig(
    enabled=True, seen_label="InboxSeen", learned_label="SpamLearned",
    swept_label="SpamSwept", undone_label="SpamSweepUndone",
    registry_prefix="SpamSenders", lookback_days=90, stamp_lookback_days=14,
    max_moves_per_run=3, max_learn_per_run=20,
    seed_senders=("vendor@ads.example", "@predatory.example"),
)


# --- learn_verdict ---------------------------------------------------------
def test_learns_a_stranger_he_reported():
    assert learn_verdict("promo@shop.example", own=OWN, known=(),
                         has_sent_to=False) == (True, "learned")


def test_never_learns_someone_he_has_written_to():
    learn, why = learn_verdict("colleague@uni.ac.kr", own=OWN, known=(),
                               has_sent_to=True)
    assert not learn and "sent mail" in why


def test_never_learns_his_own_address():
    assert not learn_verdict("prof.alias@gmail.com", own=OWN, known=(),
                             has_sent_to=False)[0]


def test_already_listed_sender_is_not_learned_twice():
    learn, why = learn_verdict("x@lab.predatory.example", own=OWN,
                               known=CFG.seed_senders, has_sent_to=False)
    assert not learn and "@predatory.example" in why


def test_missing_address_is_not_learned():
    assert not learn_verdict("", own=OWN, known=(), has_sent_to=False)[0]


# --- sweep_verdict ---------------------------------------------------------
def test_moves_listed_sender():
    move, _ = sweep_verdict("vendor@ads.example", ("INBOX",), [("INBOX",)],
                            senders=CFG.seed_senders)
    assert move


def test_display_name_match_alone_does_not_move():
    """Gmail's from: search also hits display names; the address must match."""
    move, why = sweep_verdict("student@uni.ac.kr", ("INBOX",), [("INBOX",)],
                              senders=CFG.seed_senders)
    assert not move and "not on the list" in why


def test_thread_he_replied_in_stays():
    move, why = sweep_verdict("vendor@ads.example", ("INBOX",),
                              [("INBOX",), ("SENT",)], senders=CFG.seed_senders)
    assert not move and "replied" in why


def test_starred_message_stays():
    assert not sweep_verdict("vendor@ads.example", ("INBOX", "STARRED"),
                             [("INBOX",)], senders=CFG.seed_senders)[0]


def test_domain_entry_does_not_spill_onto_lookalike_domain():
    assert match_sender("a@notpredatory.example", CFG.seed_senders) is None
    assert match_sender("a@mail.predatory.example", CFG.seed_senders)


# --- helpers ---------------------------------------------------------------
def test_registry_label_parsing():
    assert registry_address("SpamSenders/Promo@Shop.example", "SpamSenders") \
        == "promo@shop.example"
    assert registry_address("SpamSenders", "SpamSenders") is None
    assert registry_address("Other/a@b.c", "SpamSenders") is None


def test_from_clauses_chunk_and_strip_domain_marker():
    clauses = from_clauses(tuple(f"s{i}@x.example" for i in range(12))
                           + ("@dom.example",))
    assert len(clauses) == 2
    assert clauses[1] == "from:(s10@x.example OR s11@x.example OR dom.example)"


# --- stages against a fake mailbox ----------------------------------------
class FakeGmail:
    """Just enough of GmailClient: messages are dicts of id → (from, labels)."""

    def __init__(self, messages: dict[str, tuple[str, set[str]]],
                 sent_to: set[str] = frozenset()) -> None:
        self.messages = messages
        self.sent_to = sent_to
        self.labels: dict[str, str] = {}
        self.deleted: list[str] = []

    def own_addresses(self):
        return OWN

    def list_labels(self):
        return dict(self.labels)

    def ensure_label(self, name, **_):
        return self.labels.setdefault(name, f"L_{name}")

    def delete_label(self, label_id):
        self.deleted.append(label_id)
        self.labels = {k: v for k, v in self.labels.items() if v != label_id}

    def has_sent_to(self, address):
        return address in self.sent_to

    def _has(self, labels, name):
        return f"L_{name}" in labels or name in labels

    def search_message_ids(self, query, *, max_results=100, include_spam=False):
        terms = query.split()
        out = []
        for mid, (sender, labels) in self.messages.items():
            folder = "SPAM" if "in:spam" in terms else "INBOX"
            if folder not in labels:
                continue
            ok = True
            for t in terms:
                if t.startswith("label:") and not self._has(labels, t[6:]):
                    ok = False
                if t.startswith("-label:") and self._has(labels, t[7:]):
                    ok = False
            if "from:(" in query:
                wanted = query.split("from:(")[1].split(")")[0].split(" OR ")
                if not any(w in sender for w in wanted):
                    ok = False
            if ok:
                out.append(mid)
        return out[:max_results]

    def get_message(self, mid, body_chars=0):
        sender, labels = self.messages[mid]
        return GmailMessage(id=mid, thread_id=mid, label_ids=tuple(labels),
                            internal_date_ms=0,
                            headers={"From": sender, "Subject": "s"})

    def thread_label_sets(self, thread_id):
        return [tuple(self.messages[thread_id][1])]

    def modify(self, mid, *, add=None, remove=None):
        labels = self.messages[mid][1]
        labels.difference_update(remove or [])
        labels.update(add or [])

    def batch_modify(self, ids, *, add=None, remove=None):
        for mid in ids:
            self.modify(mid, add=add, remove=remove)


def _sweeper(gmail, cfg=CFG, dry_run=False):
    return InboxSweep(gmail, cfg, checked_label_id="L_checked", dry_run=dry_run)


def test_learn_registers_sender_and_shields_message_from_rescue():
    gmail = FakeGmail({"m1": ("Promo <promo@shop.example>", {"SPAM", "L_InboxSeen"})})
    s = _sweeper(gmail)
    s.learn()
    assert "promo@shop.example" in s.senders
    assert "SpamSenders/promo@shop.example" in gmail.labels
    assert {"L_SpamLearned", "L_checked"} <= gmail.messages["m1"][1]


def test_learn_ignores_spam_gmail_filed_itself():
    gmail = FakeGmail({"m1": ("promo@shop.example", {"SPAM"})})
    s = _sweeper(gmail)
    s.learn()
    assert s.summary.learned == []


def test_learn_skips_colleague_but_still_respects_his_move():
    gmail = FakeGmail({"m1": ("colleague@uni.ac.kr", {"SPAM", "L_InboxSeen"})},
                      sent_to={"colleague@uni.ac.kr"})
    s = _sweeper(gmail)
    s.learn()
    assert "colleague@uni.ac.kr" not in s.senders
    assert "L_checked" in gmail.messages["m1"][1]


def test_sweep_moves_listed_inbox_mail_to_spam():
    gmail = FakeGmail({"m1": ("vendor@ads.example", {"INBOX", "UNREAD"}),
                       "m2": ("friend@uni.ac.kr", {"INBOX"})})
    s = _sweeper(gmail)
    s.sweep()
    assert gmail.messages["m1"][1] == {"UNREAD", "SPAM", "L_SpamSwept", "L_checked"}
    assert gmail.messages["m2"][1] == {"INBOX"}
    assert s.summary.moved == 1


def test_sweep_circuit_breaker_moves_nothing():
    gmail = FakeGmail({f"m{i}": ("vendor@ads.example", {"INBOX"}) for i in range(4)})
    s = _sweeper(gmail)
    s.sweep()
    assert s.summary.aborted
    assert all("INBOX" in labels for _, labels in gmail.messages.values())


def test_dry_run_changes_no_labels():
    gmail = FakeGmail({"m1": ("vendor@ads.example", {"INBOX"}),
                       "m2": ("promo@shop.example", {"SPAM", "L_InboxSeen"})})
    s = _sweeper(gmail, dry_run=True)
    s.learn()
    s.sweep()
    s.stamp()
    assert gmail.messages["m1"][1] == {"INBOX"}
    assert gmail.messages["m2"][1] == {"SPAM", "L_InboxSeen"}
    assert "SpamSenders/promo@shop.example" not in gmail.labels


def test_moving_a_swept_message_back_unlearns_sender_and_blocks_resweep():
    gmail = FakeGmail({"m1": ("promo@shop.example", {"INBOX", "L_SpamSwept"})})
    gmail.labels["SpamSenders/promo@shop.example"] = "L_reg"
    s = _sweeper(gmail)
    assert "promo@shop.example" in s.senders
    s.honour_undos()
    assert "promo@shop.example" not in s.senders
    assert gmail.deleted == ["L_reg"]
    s.sweep()
    assert "INBOX" in gmail.messages["m1"][1]


def test_stamp_marks_inbox_mail_once():
    gmail = FakeGmail({"m1": ("a@b.example", {"INBOX"}),
                       "m2": ("c@d.example", {"INBOX", "L_InboxSeen"})})
    s = _sweeper(gmail)
    s.stamp()
    assert s.summary.stamped == 1
    assert "L_InboxSeen" in gmail.messages["m1"][1]


def test_learned_sender_is_swept_in_the_same_run():
    gmail = FakeGmail({"m1": ("promo@shop.example", {"SPAM", "L_InboxSeen"}),
                       "m2": ("promo@shop.example", {"INBOX"})})
    s = _sweeper(gmail, cfg=replace(CFG, max_moves_per_run=5))
    s.learn()
    s.sweep()
    assert "SPAM" in gmail.messages["m2"][1]
