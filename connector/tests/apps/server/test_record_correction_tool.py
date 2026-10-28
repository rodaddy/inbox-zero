"""The correction SURFACE -- the MCP tool an owner correction arrives through.

Ticket 3.4, interactive capture only. A learned rule is written by exactly one
path in this process: an owner calling ``record_correction`` through the host.
Nothing here infers a correction from mailbox state, and nothing here lets the
model layer write a rule on its own initiative.

The tests assert on the tool's OWN OUTPUT -- the response dict, the saved
store, the ledger rows, and the provider call log -- rather than on the shape of
the source file. A source-scanning assertion for "this never happens" fires on
the docstring that explains why it never happens, which makes the honest
explanation the thing that breaks the suite.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.apps.server import main as server
from inbox_zero_connector.providers.base import (
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
from inbox_zero_connector.services.triage.taxonomy import UnknownFolderError

CORRESPONDENT = "partner@acme.example"


class CorrectionSpy(MailProvider):
    """A provider that records every call and mutates nothing real.

    ``mutations`` is kept separate from ``calls`` because the taxonomy test
    needs to prove that a refused folder cost ZERO provider mutations, and
    "zero calls of any kind" is a weaker claim that a future header read would
    silently break.
    """

    name = "google"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.mutations: list[tuple[str, tuple[object, ...]]] = []
        self.sender = CORRESPONDENT
        self.containers: tuple[str, ...] = ("INBOX",)

    def authenticate(self, interactive: bool = False) -> str:
        return "owner@example.com"

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[Message]:
        self.calls.append(("list_inbox", (limit, unread_only)))
        return [
            Message(
                id="m-corr",
                thread_id="t-corr",
                sender=self.sender,
                subject="Invoice 4417",
                received="2026-08-19",
                snippet="attached",
                containers=self.containers,
            )
        ]

    def get_thread(self, thread_id: str) -> Thread:
        self.calls.append(("get_thread", (thread_id,)))
        raise NotImplementedError

    def list_containers(self) -> list[Container]:
        raise NotImplementedError

    def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
        raise NotImplementedError

    def file_message(self, message_id: str, folder: str) -> str:
        self.calls.append(("file_message", (message_id, folder)))
        self.mutations.append(("file_message", (message_id, folder)))
        return f"InboxZero/{folder}"

    def poll_changes(self, cursor, *, page_token=None, container_id=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def tag_message(self, message_id: str, label: str) -> str:
        self.mutations.append(("tag_message", (message_id, label)))
        return label

    def set_important(self, message_id: str, important: bool = True) -> str:
        self.mutations.append(("set_important", (message_id, important)))
        return "IMPORTANT"

    def create_draft(self, thread_id: str, body: str) -> DraftResult:
        self.mutations.append(("create_draft", (thread_id, body)))
        raise NotImplementedError

    def create_self_draft(self, subject: str, body: str) -> str:
        self.mutations.append(("create_self_draft", (subject, body)))
        raise NotImplementedError


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None) -> CorrectionSpy:
    provider = CorrectionSpy()
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_owner", "owner@example.com")
    return provider


def test_correction_files_the_message_and_writes_a_rule(spy: CorrectionSpy) -> None:
    """One owner correction: the mail moves AND the table learns."""
    result = server.record_correction("m-corr", "Receipts")

    assert result["ok"] is True
    assert result["folder"] == "Receipts"
    assert result["outcome"] == learned.CorrectionOutcome.CREATED.value

    assert ("file_message", ("m-corr", "Receipts")) in spy.mutations

    rows = learned.load_rules()
    assert len(rows) == 1
    assert rows[0].predicate is learned.Predicate.SENDER_IS
    assert rows[0].value == CORRESPONDENT
    assert rows[0].folder == "Receipts"
    assert rows[0].source_message_id == "m-corr"
    assert result["rule_id"] == rows[0].id

    actions = [entry.action for entry in ledger.read_all()]
    assert actions.count(ledger.ACTION_FILE) == 1
    assert actions.count(ledger.ACTION_CORRECTION) == 1

    correction = next(e for e in ledger.read_all() if e.action == ledger.ACTION_CORRECTION)
    assert correction.detail == (
        f"folder=Receipts rule_id={rows[0].id} outcome={learned.CorrectionOutcome.CREATED.value}"
    )


def test_a_folder_outside_the_taxonomy_is_refused_before_any_provider_call(
    spy: CorrectionSpy,
) -> None:
    """Closed vocabulary, same discipline as ``file_message``: no API call, no rule."""
    with pytest.raises(UnknownFolderError):
        server.record_correction("m-corr", "Invented-Folder")

    assert spy.mutations == []
    assert learned.load_rules() == ()
    assert ledger.read_all() == []


def test_the_same_correction_twice_reinforces_and_adds_no_row(spy: CorrectionSpy) -> None:
    """The owner told us something we already knew: count up, no duplicate rule."""
    first = server.record_correction("m-corr", "Receipts")
    second = server.record_correction("m-corr", "Receipts")

    assert first["outcome"] == learned.CorrectionOutcome.CREATED.value
    assert second["outcome"] == learned.CorrectionOutcome.REINFORCED.value

    rows = learned.load_rules()
    assert len(rows) == 1
    assert rows[0].correction_count == 2
    assert second["rule_id"] == rows[0].id


def test_the_response_shows_the_owner_the_rule_that_was_learned(spy: CorrectionSpy) -> None:
    """Q5: the owner SEES the rule at the moment it is learned, not later."""
    result = server.record_correction("m-corr", "Receipts")

    rendered = result["learned_rule"]
    assert CORRESPONDENT in rendered
    assert "Receipts" in rendered
    assert learned.Predicate.SENDER_IS.value in rendered

    consent = result["what_this_means"]
    assert "matches" in consent.lower()
    assert "filed by your rule" not in consent.lower()


# ---------------------------------------------------------------------------
# #169: the correction's FILE receipt must not record `None` either
# ---------------------------------------------------------------------------


def test_correction_receipts_the_unknown_sentinel_when_graph_names_no_id(
    spy: CorrectionSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same defect as the MCP tool boundary, in the correction's own move.

    A correction files the message AND writes a rule. The FILE row reached the
    ledger through `getattr` on an `Any`, so a Graph `/move` that reported no
    post-move id wrote the literal string ``"None"`` into an audit row.
    """
    from inbox_zero_connector.providers.outlook.provider import FiledMessage

    def _move(message_id: str, folder: str) -> FiledMessage:
        spy.mutations.append(("file_message", (message_id, folder)))
        return FiledMessage(f"InboxZero/{folder}", message_id=None)

    monkeypatch.setattr(spy, "file_message", _move)

    result = server.record_correction("m-corr", "Receipts")

    assert result["ok"] is True
    filed = next(e for e in ledger.read_all() if e.action == ledger.ACTION_FILE)
    assert filed.message_id == ledger.MESSAGE_ID_UNKNOWN
    assert filed.message_id != "None"
    assert filed.message_id != "m-corr"
    # The CORRECTION row still names the message the owner corrected: that id
    # was live when they named it, and it is the key the learned rule is about.
    correction = next(e for e in ledger.read_all() if e.action == ledger.ACTION_CORRECTION)
    assert correction.message_id == "m-corr"


def test_correction_receipts_the_post_move_id_when_graph_reports_one(
    spy: CorrectionSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sentinel is for the absent case only; a reported id still wins."""
    from inbox_zero_connector.providers.outlook.provider import FiledMessage

    def _move(message_id: str, folder: str) -> FiledMessage:
        spy.mutations.append(("file_message", (message_id, folder)))
        return FiledMessage(f"InboxZero/{folder}", message_id="AAMk-moved")

    monkeypatch.setattr(spy, "file_message", _move)

    server.record_correction("m-corr", "Receipts")

    filed = next(e for e in ledger.read_all() if e.action == ledger.ACTION_FILE)
    assert filed.message_id == "AAMk-moved"
