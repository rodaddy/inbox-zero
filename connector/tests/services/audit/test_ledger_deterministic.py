"""The ledger records WHICH LAYER decided, so "free filings" is auditable.

``Classification.deterministic`` is computed by ``classify`` from which layer
actually answered, and before this field existed it was dropped at the receipt
boundary. That made the determinism-first claim unbackable after the process
exited: the one file the owner can audit could not answer "which filings cost a
model call?".

The four tests here cover the claim end to end rather than field-by-field:

1. The value round-trips through a real append and read.
2. A row already on disk WITHOUT the key reads back ``False`` and does not
   raise. This is the MIGRATION claim, tested rather than asserted -- a live
   ledger is being appended to by a running watch process with the older row
   shape, so "no migration needed" has to be a fact about the parser.
3. The digest states the split, so the share is readable without parsing JSONL.
4. A watcher round whose message a LEARNED rule settled writes
   ``deterministic=True`` -- through ``_receipt``, not by calling
   ``ledger.append`` directly, because the defect being fixed was precisely
   that the receipt boundary dropped the flag.
"""

from __future__ import annotations

from datetime import UTC, datetime

from inbox_zero_connector.providers.base import (
    ChangeSet,
    Container,
    DraftResult,
    MailProvider,
    Message,
    SentMessage,
    SentSelection,
    Thread,
)
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.triage import learned as learned
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.triage.taxonomy import default_taxonomy
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState
from inbox_zero_connector.utils.paths import ledger_path

INBOX_ID = "Label_INBOX"
VENDOR = "billing@somevendor.example"
"""Reserved ``.example`` domain, so no test can resolve it."""

RIGHT_FOLDER = "Receipts"


def _read_one() -> ledger.LedgerEntry:
    """The single row the test under way wrote."""
    rows = ledger.read_all()
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    return rows[0]


def test_deterministic_true_round_trips() -> None:
    """A row appended as deterministic reads back deterministic.

    Both halves matter: ``append`` must carry the value onto the row, and
    ``_entry_from_row`` must read it back rather than defaulting it away.
    """
    ledger.append(
        "google",
        ledger.ACTION_FILE,
        "m-rule",
        RIGHT_FOLDER,
        outcome=ledger.Outcome(deterministic=True),
    )
    assert _read_one().deterministic is True


def test_model_filing_round_trips_as_false() -> None:
    """The default carries the model case, and it is the honest reading.

    Asserted alongside the True case so a field that was hard-wired to one
    value could not pass this module.
    """
    ledger.append("google", ledger.ACTION_FILE, "m-model", RIGHT_FOLDER)
    assert _read_one().deterministic is False


def test_v3_row_without_the_key_reads_false_and_does_not_raise() -> None:
    """THE MIGRATION CLAIM. An existing row stays readable and reads ``False``.

    Written as raw JSON rather than through ``append`` on purpose: the point is
    a row produced by the PREVIOUS code, which is what the live ledger is full
    of right now. ``False`` is the correct reading and not a defaulted guess --
    before this field the receipt boundary dropped the flag entirely, so no
    existing row can be shown to have been rule-decided.
    """
    target = ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    old_row = (
        '{"action": "file_message", "detail": "Receipts", "error_code": "", '
        '"message_id": "m-old", "provider": "google", "schema": 3, '
        '"status": "applied", "timestamp": "2026-08-19T09:00:00+00:00", '
        '"voice": ""}'
    )
    with target.open("a", encoding="utf-8") as handle:
        handle.write(old_row + "\n")

    entry = _read_one()
    assert entry.deterministic is False
    assert entry.message_id == "m-old"
    # The row was READ, not skipped: a parser that rejected it would have
    # yielded nothing and `_read_one` would fail above, but assert the fields
    # that prove the rest of the row survived intact.
    assert entry.detail == RIGHT_FOLDER
    assert entry.status is ledger.Status.APPLIED


def test_digest_reports_the_deterministic_split() -> None:
    """The owner reads the free/paid share without parsing JSONL."""
    for index in range(3):
        ledger.append(
            "google",
            ledger.ACTION_FILE,
            f"m-rule-{index}",
            RIGHT_FOLDER,
            outcome=ledger.Outcome(deterministic=True),
        )
    ledger.append("google", ledger.ACTION_FILE, "m-model", RIGHT_FOLDER)

    digest = ledger.daily_digest()
    assert "4 message(s)" in digest
    assert "3 by rule (free)" in digest
    assert "1 by model" in digest


def test_refused_filing_is_left_out_of_the_split() -> None:
    """Only APPLIED rows count. A refusal filed nothing, free or otherwise."""
    ledger.append(
        "google",
        ledger.ACTION_FILE,
        "m-rule",
        RIGHT_FOLDER,
        outcome=ledger.Outcome(deterministic=True),
    )
    ledger.append(
        "google",
        ledger.ACTION_FILE,
        "m-refused",
        RIGHT_FOLDER,
        outcome=ledger.failed("403"),
    )

    digest = ledger.daily_digest()
    assert "1 message(s): 1 by rule (free), 0 by model" in digest


class _OneMessageProvider(MailProvider):
    """A mailbox holding one scripted message. Shape borrowed from the demo.

    The unused half of the interface raises, so a step that reached an
    unimplemented capability fails loudly instead of reading as a pass. The
    send-shaped members raise ``AssertionError`` specifically: this connector
    has no send capability and no test may appear to exercise one.
    """

    name = "google"

    def __init__(self, message: Message) -> None:
        self.changes = ChangeSet(messages=(message,), cursor="cursor-1")
        self.filed: list[tuple[str, str]] = []

    def authenticate(self, interactive: bool = False) -> str:
        raise NotImplementedError

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[Message]:
        raise NotImplementedError

    def get_thread(self, thread_id: str) -> Thread:
        raise NotImplementedError

    def list_containers(self) -> list[Container]:
        return [
            Container(
                id=INBOX_ID,
                name="Inbox",
                well_known="inbox",
                parent_id=None,
                exclusive=False,
            )
        ]

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        return self.changes

    def file_message(self, message_id: str, folder: str) -> str:
        self.filed.append((message_id, folder))
        return folder

    def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
        raise NotImplementedError

    def set_important(self, message_id: str, important: bool = True) -> str:
        raise AssertionError("this test files messages; it never flags them")

    def tag_message(self, message_id: str, label: str) -> str:
        raise AssertionError("this test files messages out of the inbox")

    def create_draft(self, thread_id: str, body: str) -> DraftResult:
        raise AssertionError("this test never drafts and this connector never sends")

    def create_self_draft(self, subject: str, body: str) -> str:
        raise AssertionError("this test never drafts and this connector never sends")


class _NeverCalledClassifier:
    """The model layer, which a learned-rule hit must never reach.

    Raising rather than recording: if the learned pass failed to settle the
    message, the test's premise is gone and a recorded call would let the
    receipt assertion below pass for the wrong reason.
    """

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        raise AssertionError(f"a learned rule should have settled {message.id}, with no model call")


def _vendor_message(message_id: str) -> Message:
    """A header projection no hand-written rule settles, so it reaches learned."""
    return Message(
        id=message_id,
        thread_id=f"t-{message_id}",
        sender=VENDOR,
        subject="Your July invoice is ready",
        received="2026-08-19T09:00:00Z",
        snippet="Invoice attached",
        containers=(INBOX_ID,),
    )


def test_watcher_receipt_records_a_learned_filing_as_deterministic() -> None:
    """END TO END through ``_receipt``: the flag survives the receipt boundary.

    This is the test that covers the actual defect. ``classify`` already set
    ``deterministic`` correctly and ``watcher._receipt`` already received the
    whole ``Classification``; what it did not do was write the flag down. So
    this goes through a real ``poll_once`` round rather than calling
    ``ledger.append``, because an append-level test would have passed against
    the broken tree.
    """
    message = _vendor_message("m-learned")
    provider = _OneMessageProvider(message)
    rules = (
        learned.LearnedRule(
            id=1,
            predicate=learned.Predicate.SENDER_IS,
            value=VENDOR,
            folder=RIGHT_FOLDER,
            learned_at=datetime(2026, 8, 19, 17, 0, 0, tzinfo=UTC).isoformat(),
            source_message_id="m-origin",
        ),
    )

    summary = watcher.poll_once(
        watcher.RoundContext(
            provider=provider,
            scope=MailboxScope(mode="all_folders"),
            taxonomy=default_taxonomy(),
            state=WatchState(),
            container_id=None,
            classifier=_NeverCalledClassifier(),
            learned_rules=rules,
        )
    )

    assert summary.applied == 1
    assert provider.filed == [("m-learned", RIGHT_FOLDER)]
    entry = _read_one()
    assert entry.action == ledger.ACTION_FILE
    assert entry.message_id == "m-learned"
    assert entry.deterministic is True
