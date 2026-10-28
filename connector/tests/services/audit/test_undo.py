"""What ``undo`` can and cannot promise, given what the audit spine records (#66).

THE GAP THIS FILE USED TO GUARD IS CLOSED, AND THE NEW GUARD IS THE POINT.
``undo`` was specified to reverse a filing by calling ``file_message`` with the
ledger's source container, and the ledger did not record one -- so the plan was
read from the decision TRACE, a file its own module documents as a diagnostic,
and no ``--apply`` shipped. Schema 4 records ``run_id`` and ``source_container``
on the receipt itself, so the plan now comes from receipts and the reversal is
authorised by the same evidence that proves the filing happened.

What still refuses is a pre-schema-4 row: it carries no source, and
``test_a_row_without_a_source_is_never_reversed`` is what holds ``undo`` to
skipping it rather than guessing where its message came from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inbox_zero_connector.providers.base import MailProvider
from inbox_zero_connector.services.audit import ledger, undo
from inbox_zero_connector.utils.paths import ledger_path

RUN = "run-aaaa"
OTHER_RUN = "run-bbbb"


class FakeProvider:
    """A mailbox that records the moves asked of it. Never touches a network.

    Not a ``MailProvider`` subclass: ``apply`` reads exactly ``name`` and
    ``file_message``, so a stub with those two is the whole seam, and inheriting
    would mean implementing a dozen methods the reversal never calls.
    """

    def __init__(self, name: str = "google", refuse: frozenset[str] = frozenset()) -> None:
        self.name = name
        self.refuse = refuse
        self.moves: list[tuple[str, str]] = []

    def file_message(self, message_id: str, folder: str) -> str:
        if message_id in self.refuse:
            raise RuntimeError("provider said no")
        self.moves.append((message_id, folder))
        return message_id


def _file(
    message_id: str,
    destination: str,
    source: str,
    run_id: str = RUN,
    status: ledger.Status = ledger.Status.APPLIED,
) -> None:
    """Append one filing receipt of the shape ``round._receipt`` writes."""
    ledger.append(
        "google",
        ledger.ACTION_FILE,
        message_id,
        destination,
        outcome=ledger.APPLIED if status is ledger.Status.APPLIED else ledger.failed("403"),
        provenance=ledger.Provenance(run_id=run_id, source_container=source),
    )


@pytest.fixture
def filed_run() -> None:
    """One run that filed two messages, plus a refusal and another run's row."""
    _file("m1", "Receipts", "INBOX")
    _file("m2", "Newsletters", "INBOX")
    _file("m-refused", "Receipts", "INBOX", status=ledger.Status.FAILED)
    _file("m-other", "Receipts", "INBOX", run_id=OTHER_RUN)


def test_the_plan_reads_the_source_folder_off_the_receipt(filed_run: None) -> None:
    """The whole ticket: a reversal's destination comes from the LEDGER now.

    Before schema 4 this value existed only in the decision trace, which is why
    ``--apply`` could not exist. ``folder_to`` being the recorded source rather
    than a guess is what authorises the write.
    """
    plan = undo.plan(RUN)
    assert [(step.message_id, step.folder_from, step.folder_to) for step in plan.steps] == [
        ("m1", "Receipts", "INBOX"),
        ("m2", "Newsletters", "INBOX"),
    ]


def test_a_refused_filing_is_never_reversed(filed_run: None) -> None:
    """``Status.FAILED`` means the message never moved, so moving it back is wrong."""
    assert "m-refused" not in {step.message_id for step in undo.plan(RUN).steps}


def test_another_run_is_not_swept_in(filed_run: None) -> None:
    """``--run`` selects ONE run. A reversal that took neighbours would be a bug."""
    assert [step.message_id for step in undo.plan(OTHER_RUN).steps] == ["m-other"]


def test_last_run_id_is_the_most_recent_run_that_filed(filed_run: None) -> None:
    """Row order answers it: the ledger is append-only, so the last row is latest."""
    assert undo.last_run_id() == OTHER_RUN


def test_a_row_without_a_source_is_never_reversed_and_is_counted() -> None:
    """A pre-schema-4 row records no source, so undo refuses it rather than guessing.

    COUNTED, NOT DROPPED. An operator told "2 moved" about a run of three has
    been told the mailbox is restored when it is not; ``skipped_unreversible``
    is what keeps the report honest.
    """
    _file("m-new", "Receipts", "INBOX")
    ledger.append(
        "google",
        ledger.ACTION_FILE,
        "m-old",
        "Receipts",
        provenance=ledger.Provenance(run_id=RUN, source_container=""),
    )
    plan = undo.plan(RUN)
    assert [step.message_id for step in plan.steps] == ["m-new"]
    assert plan.skipped_unreversible == 1


def test_apply_moves_each_message_back_through_the_provider(filed_run: None) -> None:
    """``--apply``'s reason for existing: the mail actually goes back."""
    provider = FakeProvider()
    moved = undo.apply(provider, undo.plan(RUN))
    assert moved == 2
    assert provider.moves == [("m1", "INBOX"), ("m2", "INBOX")]


def test_every_reversal_writes_its_own_receipt(filed_run: None) -> None:
    """A reversal is a provider write, so it owes a row exactly as a filing does.

    The original filing rows are UNTOUCHED -- the filing did happen -- and the
    reversal is appended beside them. That is the append-only contract in
    ``services/audit/README.md``: a wrong row is corrected by a later row.
    """
    before = len(ledger.read_all())
    undo.apply(FakeProvider(), undo.plan(RUN))
    rows = ledger.read_all()
    undos = [row for row in rows if row.action == ledger.ACTION_UNDO]
    assert len(rows) == before + 2
    assert [(row.message_id, row.detail, row.source_container) for row in undos] == [
        ("m1", "INBOX", "Receipts"),
        ("m2", "INBOX", "Newsletters"),
    ]


def test_an_undo_row_is_not_itself_reversible(filed_run: None) -> None:
    """A second ``undo --last`` must not re-file what the first put back.

    Only ``ACTION_FILE`` rows are candidates, so the reversal's own receipt is
    invisible to the next plan -- otherwise undo would oscillate.
    """
    undo.apply(FakeProvider(), undo.plan(RUN))
    assert [step.message_id for step in undo.plan(RUN).steps] == ["m1", "m2"]


def test_a_refused_reversal_is_receipted_and_the_rest_continue(filed_run: None) -> None:
    """One contended message must not strand the rest of a reversal half-done."""
    provider = FakeProvider(refuse=frozenset({"m1"}))
    moved = undo.apply(provider, undo.plan(RUN))
    assert moved == 1
    assert provider.moves == [("m2", "INBOX")]
    failed = [
        row
        for row in ledger.read_all()
        if row.action == ledger.ACTION_UNDO and row.status is ledger.Status.FAILED
    ]
    assert [(row.message_id, row.error_code) for row in failed] == [("m1", "RuntimeError")]


def test_a_row_from_another_provider_is_refused(filed_run: None) -> None:
    """The ledger holds every account's rows; filing a Graph id via Gmail is a
    write against the wrong mailbox."""
    provider = FakeProvider(name="microsoft")
    assert undo.apply(provider, undo.plan(RUN)) == 0
    assert provider.moves == []


def test_planning_touches_no_mailbox_and_rewrites_no_receipt(filed_run: None) -> None:
    """Planning is a read. THE FILE COMPARISON IS THE ASSERTION THAT MATTERS."""
    before = ledger_path().read_bytes()
    undo.plan(RUN)
    assert ledger_path().read_bytes() == before


def test_old_schema_rows_still_load(tmp_path: Path) -> None:
    """A v3 row has neither new key and must read back without error.

    THE BACKWARD-COMPATIBLE READ THE SCHEMA BUMP PROMISES. Empty is the correct
    reading of both fields rather than a defaulted guess: the receipt boundary
    recorded neither value then, so no such row can be shown to name a run or a
    source -- which is exactly why ``undo`` refuses it.
    """
    target = tmp_path / "ledger.jsonl"
    target.write_text(
        json.dumps({
            "timestamp": "2026-08-01T00:00:00+00:00",
            "provider": "google",
            "action": ledger.ACTION_FILE,
            "message_id": "m-v3",
            "detail": "Receipts",
            "voice": "",
            "status": "applied",
            "error_code": "",
            "deterministic": False,
            "schema": 3,
        })
        + "\n",
        encoding="utf-8",
    )
    entries = ledger.read_all(target)
    assert [(e.message_id, e.run_id, e.source_container) for e in entries] == [("m-v3", "", "")]
    assert undo.plan("", target).steps == ()


def test_the_provider_seam_is_the_real_interface() -> None:
    """``FakeProvider`` stands in for a real adapter, so its shape must match.

    A stub that drifted from ``MailProvider.file_message`` would make every test
    above pass against a contract the real adapters do not have.
    """
    assert hasattr(MailProvider, "file_message")
    assert FakeProvider().file_message("m", "INBOX") == "m"
