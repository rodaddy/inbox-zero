"""Startup reconciliation: a filing left unreceipted by a crash is named out loud.

The daemon is the only thing that ever reads the intent journal, and it reads
it ONCE per start. An unsettled intent means a previous run may have moved mail
and died before its ledger row landed -- the audit gap the ledger's own fsync
cannot cover, because it protects a write that was never begun.
"""

from __future__ import annotations

from loguru import logger

from _watch_harness import FakeProvider, _context
from inbox_zero_connector.providers.base import ChangeSet
from inbox_zero_connector.services.audit import intents
from inbox_zero_connector.services.watch import loop as watchloop


def test_run_reports_and_compacts_an_unsettled_intent() -> None:
    intents.begin("google", "m-orphan", "Action")

    lines: list[str] = []
    sink_id = logger.add(lines.append, level="ERROR", format="{message}")
    try:
        watchloop.run(_context(FakeProvider(ChangeSet())), once=True, sleep=lambda _s: None)
    finally:
        logger.remove(sink_id)

    logged = "".join(lines)
    assert "m-orphan" in logged
    assert "NO ledger receipt" in logged
    # Still unsettled after compaction -- nothing was repaired, and the gap
    # survives so a second start does not silently lose it.
    assert [row["message_id"] for row in intents.unsettled()] == ["m-orphan"]


def test_a_clean_start_reports_nothing_and_empties_the_journal() -> None:
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="ERROR", format="{message}")
    try:
        watchloop.run(_context(FakeProvider(ChangeSet())), once=True, sleep=lambda _s: None)
    finally:
        logger.remove(sink_id)

    assert "NO ledger receipt" not in "".join(lines)
    assert intents.unsettled() == []
