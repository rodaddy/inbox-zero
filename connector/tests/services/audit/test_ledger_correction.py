"""The correction receipt -- a ledger row for a change to the LEARNED-RULE STORE.

A correction never touches the mailbox, which is exactly why it needs a row: it
changes what the connector will do to the mailbox tomorrow, and the owner must
be able to audit that the same way they audit a move.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.utils.paths import ledger_path


def _detail(folder: str, rule_id: str, outcome: str) -> str:
    return f"folder={folder} rule_id={rule_id} outcome={outcome}"


def test_correction_row_round_trips_through_append_and_read() -> None:
    written = ledger.append(
        "google",
        ledger.ACTION_CORRECTION,
        "m-corr-1",
        _detail("Newsletters", "rule-7", "new-rule"),
    )
    assert written.action == ledger.ACTION_CORRECTION

    entries = ledger.read_all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action == ledger.ACTION_CORRECTION
    assert entry.message_id == "m-corr-1"
    assert entry.detail == _detail("Newsletters", "rule-7", "new-rule")
    assert entry.status is ledger.Status.APPLIED

    parsed = json.loads(ledger_path().read_text(encoding="utf-8").strip())
    assert parsed["action"] == "record_correction"


def test_correction_is_a_mutating_action() -> None:
    assert ledger.ACTION_CORRECTION in ledger.MUTATING_ACTIONS


def test_digest_renders_a_corrections_section_when_rows_exist() -> None:
    ledger.append(
        "google",
        ledger.ACTION_CORRECTION,
        "m-corr-1",
        _detail("Newsletters", "rule-7", "new-rule"),
    )
    ledger.append(
        "google",
        ledger.ACTION_CORRECTION,
        "m-corr-2",
        _detail("Receipts", "rule-8", "disabled"),
    )
    digest = ledger.daily_digest()
    assert "Learned-rule corrections recorded: 2" in digest
    assert "  m-corr-1: folder=Newsletters rule_id=rule-7 outcome=new-rule" in digest
    assert "  m-corr-2: folder=Receipts rule_id=rule-8 outcome=disabled" in digest


def test_digest_omits_the_corrections_section_when_there_are_none() -> None:
    """Absent, not a zero-count heading -- matching every other section here."""
    ledger.append("google", ledger.ACTION_FILE, "m1", "Newsletters")
    digest = ledger.daily_digest()
    assert "correction" not in digest.lower()


def test_an_old_ledger_without_correction_rows_still_reads_clean() -> None:
    """A v1-shaped file predating this action reads back with no skips."""
    target = ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).date().isoformat()
    rows = [
        {
            "timestamp": f"{today}T09:00:00+00:00",
            "provider": "google",
            "action": "file_message",
            "message_id": "old-1",
            "detail": "Newsletters",
            "schema": 1,
        },
        {
            "timestamp": f"{today}T09:05:00+00:00",
            "provider": "google",
            "action": "mark_important",
            "message_id": "old-2",
            "detail": "important",
            "schema": 1,
        },
    ]
    target.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )

    entries = ledger.read_all()
    assert [e.message_id for e in entries] == ["old-1", "old-2"]
    assert all(e.status is ledger.Status.APPLIED for e in entries)
    digest = ledger.daily_digest()
    assert "Filed 1 message(s):" in digest
    assert "correction" not in digest.lower()


def test_correction_rows_are_not_counted_as_filings_or_flags() -> None:
    ledger.append("google", ledger.ACTION_FILE, "m1", "Newsletters")
    ledger.append(
        "google",
        ledger.ACTION_CORRECTION,
        "m-corr-1",
        _detail("Newsletters", "rule-7", "info-update"),
    )
    digest = ledger.daily_digest()
    assert "Filed 1 message(s):" in digest
    assert "  Newsletters: 1" in digest
    assert "Learned-rule corrections recorded: 1" in digest


def test_correction_detail_carries_no_message_content() -> None:
    """The receipt names a rule and a folder. Never a subject, never a body."""
    entry = ledger.append(
        "microsoft",
        ledger.ACTION_CORRECTION,
        "m-corr-9",
        _detail("Archive", "rule-1", "disabled"),
    )
    assert entry.detail.startswith("folder=")

    # The rendered row is EXACTLY the id plus the structured detail. Asserting
    # the whole line, not just its parts, is what would catch a subject line
    # being appended to it later.
    rendered = [line for line in ledger.daily_digest().splitlines() if "m-corr-9" in line]
    assert rendered == ["  m-corr-9: folder=Archive rule_id=rule-1 outcome=disabled"]


def test_entries_for_day_includes_corrections() -> None:
    ledger.append(
        "google",
        ledger.ACTION_CORRECTION,
        "m-corr-1",
        _detail("Newsletters", "rule-7", "new-rule"),
    )
    today: date = datetime.now(UTC).date()
    found = ledger.entries_for_day(today)
    assert [e.action for e in found] == [ledger.ACTION_CORRECTION]
