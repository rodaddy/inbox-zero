"""``watch --once --dry-run``: classify the mail, touch nothing (#66 part 1).

THE DEFECT THIS CLOSES. The first real watch pass against a live mailbox
CREATES FOLDERS -- lazily, inside the providers' own filing call
(``providers/gmail/provider.py`` ``_create_label``,
``providers/outlook/provider.py`` ``create_folder``) -- so an operator running
the connector for the first time discovers the taxonomy by finding it already
built in their mail client. A preview pass is the warning that was missing.

A DRY ROUND IS A READ. That is the whole assertion surface here, and it is
wider than "no ``file_message`` call": a round that filed nothing but still
stored its cursor would move the stream past mail it never filed, and the next
REAL pass would never be offered it. So this module pins six separate
non-effects -- no filing, no ledger row, no intent row, no trace row, no stored
cursor, no replay mark -- plus the one thing a dry round DOES produce, which is
the planned filings it reports.

The planned filings carry ids and folder names only. Never a subject, sender,
or snippet: this preview is printed to a terminal and pasted into support
threads, and ``utils/logging_config.py``'s content rule does not stop caring
because the destination is stdout rather than a log sink.
"""

from __future__ import annotations

from _watch_harness import (
    CONFIDENTIAL_ID,
    FOLDER,
    INBOX_ID,
    CountingClassifier,
    FakeProvider,
    _context,
    _message,
)
from inbox_zero_connector.providers.base import ChangeSet
from inbox_zero_connector.services.audit import intents, ledger
from inbox_zero_connector.services.audit import trace as trace_mod
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState


def _dry_round(
    provider: FakeProvider,
    *,
    state: WatchState,
    classifier: CountingClassifier,
) -> watcher.RoundSummary:
    """One round with ``dry_run`` set. The flag is the thing under test."""
    context = _context(provider, state=state, classifier=classifier).model_copy(
        update={"dry_run": True}
    )
    return watcher.poll_once(context)


def test_a_dry_round_classifies_but_files_nothing() -> None:
    """The classifier ran; the provider's write did not.

    Both halves matter. A preview that skipped classification could not name a
    destination folder, and a preview that filed is not a preview.
    """
    provider = FakeProvider(
        ChangeSet(messages=[_message("m-1"), _message("m-2")], cursor="history-2001")
    )
    classifier = CountingClassifier()
    summary = _dry_round(provider, state=WatchState(), classifier=classifier)

    assert classifier.seen_ids == ["m-1", "m-2"], "a dry round still classifies"
    assert provider.filed == [], "a dry round must make no provider write"
    assert summary.applied == 0
    assert summary.failed == 0


def test_a_dry_round_writes_no_ledger_intent_or_trace_row() -> None:
    """No mutation happened, so nothing is owed to the audit trail.

    The INTENT is the one worth naming: it is a write-ahead row settled after
    the receipt, so a dry round that began an intent and never filed would leave
    an unsettled row the next daemon start reports as an interrupted move that
    never happened.
    """
    provider = FakeProvider(ChangeSet(messages=[_message("m-1")], cursor="history-2001"))
    _dry_round(provider, state=WatchState(), classifier=CountingClassifier())

    assert ledger.read_all() == [], "a dry round wrote a ledger receipt"
    assert not intents.intents_path().exists(), "a dry round wrote a write-ahead intent"
    assert not trace_mod.trace_path().exists(), "a dry round wrote a decision trace"


def test_a_dry_round_stores_no_cursor_and_marks_nothing_seen() -> None:
    """The next REAL pass must be offered exactly the same mail.

    A stored cursor or a replay mark would make the preview consume the mail it
    only looked at -- the operator reads WOULD CREATE, runs the real pass, and
    the mail is gone from the stream because the preview already spent it.
    """
    state = WatchState()
    provider = FakeProvider(ChangeSet(messages=[_message("m-1")], cursor="history-2001"))
    summary = _dry_round(provider, state=state, classifier=CountingClassifier())

    assert summary.cursor_stored is False
    assert state.cursor_for(None) is None, "a dry round stored a cursor"
    assert state.seen_message(None, "m-1") is False, "a dry round marked a message seen"


def test_a_dry_round_reports_the_planned_filings() -> None:
    """The preview's product: message id, decided folder, deterministic flag.

    Ids and folder names ONLY. A subject or sender on this record would reach
    the terminal the preview prints to.
    """
    provider = FakeProvider(
        ChangeSet(messages=[_message("m-1"), _message("m-2")], cursor="history-2001")
    )
    summary = _dry_round(provider, state=WatchState(), classifier=CountingClassifier())

    planned = summary.planned
    assert [entry.message_id for entry in planned] == ["m-1", "m-2"]
    assert {entry.folder for entry in planned} == {FOLDER}
    assert all(entry.deterministic is False for entry in planned)
    assert all(
        not hasattr(entry, field) for entry in planned for field in ("subject", "sender", "snippet")
    ), "the planned filing must carry no message content"


def test_a_real_round_reports_no_planned_filings() -> None:
    """``planned`` is empty when the round actually filed. It is preview-only.

    Without this the field could quietly become a second copy of the ledger,
    carried by every round and printed by anything that reads a summary.
    """
    provider = FakeProvider(ChangeSet(messages=[_message("m-1")], cursor="history-2001"))
    context = _context(provider, state=WatchState(), classifier=CountingClassifier())
    summary = watcher.poll_once(context)

    assert summary.applied == 1
    assert summary.planned == ()


def test_a_resynced_dry_round_reports_no_planned_filings() -> None:
    """A refused cursor invalidates the preview it was read under.

    The provider refused the position this round was reading from, so whatever
    the dry pass decided describes a stream state that no longer exists.
    Printing it would show the operator a preview of mail the next poll may
    never offer.
    """
    provider = FakeProvider(ChangeSet(messages=[_message("m-1")], resync_required=True))
    summary = _dry_round(provider, state=WatchState(), classifier=CountingClassifier())

    assert summary.resynced is True
    assert summary.planned == ()
    assert provider.filed == []


def test_the_dry_flag_is_off_by_default() -> None:
    """A context built without the flag files for real. The preview is opt-in."""
    provider = FakeProvider(ChangeSet(messages=[], cursor="history-2001"))
    assert _context(provider).dry_run is False


def test_the_scope_gate_still_precedes_classification_on_a_dry_round() -> None:
    """A preview does not buy permission to read out-of-scope mail.

    The dry flag skips the WRITE, not the gate: an out-of-scope message is still
    refused before ``classify`` sees its subject, which is the read the client
    cares about and cannot be taken back.
    """
    provider = FakeProvider(
        ChangeSet(
            messages=[
                _message("m-in", containers=(INBOX_ID,)),
                _message("m-out", containers=(CONFIDENTIAL_ID,)),
            ],
            cursor="history-2001",
        )
    )
    classifier = CountingClassifier()
    context = _context(
        provider,
        scope=MailboxScope(mode="only_listed", included_folders=("Inbox",)),
        state=WatchState(),
        classifier=classifier,
    ).model_copy(update={"dry_run": True})
    summary = watcher.poll_once(context)

    assert classifier.seen_ids == ["m-in"], "an out-of-scope message was classified"
    assert [entry.message_id for entry in summary.planned] == ["m-in"]
    assert summary.skipped_out_of_scope == 1
