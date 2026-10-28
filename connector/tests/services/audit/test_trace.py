"""Decision-trace behaviour -- the content-free diagnostic row per classified message."""

from __future__ import annotations

import json

from _watch_harness import (
    FINAL_CURSOR,
    INBOX_ID,
    CountingClassifier,
    FakeProvider,
    _context,
)
from inbox_zero_connector.providers.base import ChangeSet, Message
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.audit import trace
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.utils.paths import ledger_path

PLANTED_SUBJECT = "PLANTED-SUBJECT-7781"
"""A string that exists only to be looked for in the raw trace file."""

DECISION = Classification(
    folder="Review",
    confidence=0.9,
    reason="rule: sender_domain_is example.com",
    deterministic=True,
)


def _subject(**overrides: str) -> trace.Subject:
    fields = {
        "provider": "google",
        "message_id": "m1",
        "thread_id": "t1",
        "from_domain": "example.com",
        "folder_before": "INBOX",
    }
    fields.update(overrides)
    return trace.Subject(**fields)


def _lines() -> list[str]:
    return trace.trace_path().read_text(encoding="utf-8").strip().splitlines()


def test_append_creates_the_file_beside_the_ledger_and_one_row() -> None:
    assert trace.trace_path().parent == ledger_path().parent
    trace.append(_subject(), DECISION)
    assert trace.trace_path().exists()
    rows = _lines()
    assert len(rows) == 1
    parsed = json.loads(rows[0])
    assert parsed["schema_version"] == 1
    assert parsed["provider"] == "google"
    assert parsed["message_id"] == "m1"
    assert parsed["thread_id"] == "t1"


def test_rows_accumulate_and_never_overwrite() -> None:
    trace.append(_subject(message_id="a"), DECISION)
    trace.append(_subject(message_id="b"), DECISION)
    rows = trace.read_all()
    assert [row.message_id for row in rows] == ["a", "b"]
    assert len(_lines()) == 2


def test_failed_result_keeps_folder_before_and_records_error_code() -> None:
    row = trace.append(_subject(), DECISION, error_code="ProviderError")
    assert row.result is trace.Result.FAILED
    assert row.folder_after == "INBOX"
    assert row.error_code == "ProviderError"


def test_applied_result_moves_to_the_decision_folder() -> None:
    row = trace.append(_subject(), DECISION)
    assert row.result is trace.Result.APPLIED
    assert row.folder_after == DECISION.folder
    assert row.error_code == ""


def test_reason_is_capped() -> None:
    long_reason = Classification(
        folder="Review", confidence=0.5, reason="x" * 1000, deterministic=False
    )
    row = trace.append(_subject(), long_reason)
    assert len(row.decision_reason) == trace.MAX_REASON_CHARS


def test_from_domain_never_returns_the_local_part() -> None:
    domain = trace.from_domain("Alice.Smith@Example.COM")
    assert domain == "example.com"
    assert "alice" not in domain
    assert trace.from_domain("") == ""


def test_row_carries_no_mail_content() -> None:
    planted = trace.from_domain("planted.person@example.com")
    row = trace.append(_subject(from_domain=planted), DECISION)
    serialized = row.to_json()
    for banned in ("subject", "body", "snippet"):
        assert f'"{banned}"' not in serialized
    assert "planted.person" not in serialized
    assert "example.com" in serialized


def test_read_all_roundtrips_and_missing_file_is_empty() -> None:
    assert trace.read_all() == []
    written = trace.append(_subject(), DECISION)
    assert trace.read_all() == [written]


# ---------------------------------------------------------------------------
# The watch round writes the trace -- integration over the shared fakes
# ---------------------------------------------------------------------------
#
# The unit tests above prove ``append`` writes what it was handed. These prove
# the ROUND hands it the right things: one row per classified message, the same
# ids the ledger receipted, and nothing a message body could reach.


def _round_messages() -> tuple[Message, ...]:
    """Three in-scope messages with distinct sender domains and a planted subject.

    The subject and the local parts are the assertion surface for the
    content-free rule: a planted string that appears nowhere in the file is
    proof the round never handed it over.
    """
    senders = ("alice@clients.example", "bob@vendors.example", "carol@partners.example")
    return tuple(
        Message(
            id=f"m{index}",
            thread_id=f"t-m{index}",
            sender=sender,
            subject=PLANTED_SUBJECT,
            received="2026-08-18T10:00:00Z",
            snippet="attached",
            containers=(INBOX_ID,),
        )
        for index, sender in enumerate(senders, start=1)
    )


def test_round_writes_one_trace_row_per_classified_message_with_ledger_matching_ids() -> None:
    """One row per classified message, keyed to the ids the ledger receipted."""
    messages = _round_messages()
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))

    summary = watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    rows = trace.read_all()
    assert len(rows) == summary.classified == 3
    filed = {entry.message_id for entry in ledger.read_all() if entry.action == ledger.ACTION_FILE}
    assert {row.message_id for row in rows} == filed
    domains = {sender.split("@")[1] for sender in (m.sender for m in messages)}
    assert {row.from_domain for row in rows} == domains
    for row in rows:
        assert "@" not in row.from_domain
        assert row.folder_before == "Inbox"
        assert row.result is trace.Result.APPLIED
        assert row.folder_after == row.decision_folder


def test_refused_filing_traces_failed_with_the_error_type_and_unchanged_folder() -> None:
    """A refused move leaves the message where it was, and the row says so."""
    provider = FakeProvider(
        ChangeSet(messages=(_round_messages()[0],), cursor=FINAL_CURSOR),
        file_error=RuntimeError("boom"),
    )

    watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    rows = trace.read_all()
    assert len(rows) == 1
    assert rows[0].result is trace.Result.FAILED
    assert rows[0].error_code == "RuntimeError"
    assert rows[0].folder_after == rows[0].folder_before == "Inbox"


def test_out_of_scope_messages_leave_no_trace_row() -> None:
    """Never classified means never traced -- the filter working is not an event."""
    provider = FakeProvider(ChangeSet(messages=(_round_messages()[0],), cursor=FINAL_CURSOR))
    scope = MailboxScope(mode="all_except", excluded_folders=("Inbox",))

    summary = watcher.poll_once(_context(provider, scope=scope, classifier=CountingClassifier()))

    assert summary.classified == 0
    assert trace.read_all() == []


def test_trace_rows_never_carry_subject_or_sender_address() -> None:
    """The planted subject and every local part are absent from the raw file."""
    messages = _round_messages()
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))

    watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    text = trace.trace_path().read_text(encoding="utf-8")
    assert PLANTED_SUBJECT not in text
    for message in messages:
        assert message.sender.split("@")[0] not in text
        assert message.sender not in text
