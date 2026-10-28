"""The write-ahead intent journal -- the ordering guard in front of the ledger.

The defect these cover: the mailbox used to change before anything recorded
that a receipt was owed, so a crash in between left moved mail with no row and
nothing that knew one was missing.
"""

from __future__ import annotations

import pytest
from loguru import logger

from inbox_zero_connector.services.audit import intents, ledger


class _FakeProvider:
    """A provider whose ``file_message`` records the call and returns a plain id."""

    name = "google"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def file_message(self, message_id: str, folder: str) -> str:
        self.calls.append((message_id, folder))
        return message_id


def test_happy_path_leaves_zero_unsettled() -> None:
    provider = _FakeProvider()
    applied, entry = intents.file_with_receipt(
        provider, "m1", "Action", outcome=ledger.Outcome(deterministic=False)
    )
    assert applied == "m1"
    assert entry.message_id == "m1"
    assert entry.detail == "Action"
    assert provider.calls == [("m1", "Action")]
    assert intents.unsettled() == []


def test_receipt_failure_leaves_the_intent_unsettled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mutation landed and the row did not -- exactly what must be reported."""
    provider = _FakeProvider()

    def _boom(**_kwargs: object) -> None:
        raise OSError("ledger unwritable")

    monkeypatch.setattr(intents.ledger, "append", _boom)
    with pytest.raises(OSError, match="ledger unwritable"):
        intents.file_with_receipt(
            provider, "m2", "Action", outcome=ledger.Outcome(deterministic=False)
        )

    assert provider.calls == [("m2", "Action")], "the mailbox DID change"
    pending = intents.unsettled()
    assert len(pending) == 1
    assert pending[0]["message_id"] == "m2"
    assert pending[0]["provider"] == "google"
    assert pending[0]["folder"] == "Action"


def test_reconcile_reports_then_compacts() -> None:
    """A loguru SINK, not ``caplog``: this package logs through loguru, which
    does not propagate into the stdlib tree ``caplog`` reads."""
    intents.begin("google", "orphan", "Action")
    intents.file_with_receipt(
        _FakeProvider(), "clean", "Action", outcome=ledger.Outcome(deterministic=False)
    )

    lines: list[str] = []
    sink_id = logger.add(lines.append, level="ERROR", format="{message}")
    try:
        reported = intents.reconcile()
    finally:
        logger.remove(sink_id)

    assert [row["message_id"] for row in reported] == ["orphan"]
    logged = "".join(lines)
    assert "orphan" in logged
    assert "NO ledger receipt" in logged
    # Compacted: the settled pair is gone, the unsettled begin is kept, and a
    # second reconcile still names it rather than losing the gap.
    rows = intents.intents_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    assert [row["message_id"] for row in intents.unsettled()] == ["orphan"]


def test_compact_empties_the_file_when_everything_settled() -> None:
    for index in range(3):
        intents.file_with_receipt(
            _FakeProvider(), f"m{index}", "Action", outcome=ledger.Outcome(deterministic=False)
        )
    intents.compact()
    assert intents.intents_path().read_text(encoding="utf-8") == ""
    assert intents.unsettled() == []


def test_unsettled_is_empty_before_anything_is_written() -> None:
    assert not intents.intents_path().exists()
    assert intents.unsettled() == []
