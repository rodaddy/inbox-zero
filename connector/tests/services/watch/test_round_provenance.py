"""One round's receipts carry the run and the source folder, and ``undo`` reads them (#66).

THE ROUND TRIP IS THE TEST. Every other test in this change exercises one half:
``tests/services/audit/test_undo.py`` builds ledger rows by hand and reverses
them, and the ledger's own tests prove the fields serialise. Neither would catch
the failure that actually matters -- the receipt boundary writing a source
folder the reversal cannot use, or minting a run id per message instead of per
round. So this drives a REAL ``poll_once`` against a fake mailbox and then plans
the reversal from the rows it wrote, with nothing hand-built in between.
"""

from __future__ import annotations

from _watch_harness import FOLDER, CountingClassifier, FakeProvider, _context, _message
from inbox_zero_connector.providers.base import ChangeSet
from inbox_zero_connector.services.audit import ledger, undo
from inbox_zero_connector.services.watch import round as watcher


def _filed_round(message_count: int = 2) -> watcher.RoundSummary:
    """Run ONE real round that files ``message_count`` messages out of the inbox."""
    provider = FakeProvider(
        ChangeSet(
            messages=[_message(f"m{index}") for index in range(message_count)],
            cursor="c1",
        )
    )
    return watcher.poll_once(_context(provider, classifier=CountingClassifier(FOLDER)))


def test_every_receipt_in_one_round_shares_one_run_id() -> None:
    """MINTED ONCE PER ROUND, not per message.

    A per-message id would leave ``undo --run`` nothing to select on: the ids
    would be unique per row and no group would name "the run that just filed".
    """
    _filed_round()
    runs = {row.run_id for row in ledger.read_all() if row.action == ledger.ACTION_FILE}
    assert len(runs) == 1
    assert runs != {""}


def test_two_rounds_get_different_run_ids() -> None:
    """A second round must be separately reversible, or ``--last`` reverses both."""
    _filed_round(message_count=1)
    first = undo.last_run_id()
    _filed_round(message_count=1)
    assert undo.last_run_id() != first


def test_the_receipt_records_the_folder_the_message_came_from() -> None:
    """THE FIELD THE WHOLE TICKET IS ABOUT.

    ``detail`` is the destination and always was. ``source_container`` is the
    origin, and without it a reversal has to guess -- which is why ``undo``
    shipped preview-only until this landed.
    """
    _filed_round(message_count=1)
    filed = [row for row in ledger.read_all() if row.action == ledger.ACTION_FILE]
    assert [(row.detail, row.source_container) for row in filed] == [(FOLDER, "Inbox")]


def test_undo_plans_a_real_round_from_its_own_receipts() -> None:
    """End to end: the round files, and the reversal is read back off the ledger.

    ``folder_to`` being the inbox is the assertion that matters -- it is the folder
    the round actually read the message out of, carried through the receipt into
    the plan without the decision trace being consulted at any point.
    """
    summary = _filed_round()
    plan = undo.plan(undo.last_run_id())
    assert summary.applied == 2
    assert [(step.folder_from, step.folder_to) for step in plan.steps] == [
        (FOLDER, "Inbox"),
        (FOLDER, "Inbox"),
    ]
