"""The round summary's SHAPE -- what a completed round is allowed to carry.

Split out of ``test_watcher.py`` on 2026-09-01 when that module reached 508
code lines, past the repo's 500-line ceiling
(``scripts/check_code_size.py``). The seam is the concern, not an arbitrary
cut: every test here asserts a content-containment property of
``RoundSummary`` and ``PlannedFiling`` -- counts and identifiers only, frozen,
no extra fields, nothing of the message that was read -- rather than the
polling behaviour the parent module drives.

The shared watcher fakes still live in ``_watch_harness`` alongside the rest
of the watch suite; nothing here is new, and nothing here was renamed.
"""

from __future__ import annotations

import pytest

from _watch_harness import (
    FINAL_CURSOR,
    CountingClassifier,
    FakeProvider,
    _context,
    _message,
)
from inbox_zero_connector.providers.base import ChangeSet
from inbox_zero_connector.services.watch import round as watcher


def test_the_summary_model_can_hold_no_message_content() -> None:
    """Counts only. There is no field a subject or a sender could occupy."""
    fields = watcher.RoundSummary.model_fields
    counters = {
        "seen",
        "skipped_out_of_scope",
        "skipped_unenumerated_container",
        "skipped_missing",
        "skipped_already_seen",
        "classified",
        "applied",
        "failed",
    }
    assert counters <= set(fields)
    for name in counters:
        assert fields[name].annotation is int
    # ``planned`` is the ONE structured field, and it is admitted here only
    # because ``PlannedFiling`` is itself counts-and-identifiers: id, folder
    # name, and a bool. Its own content guard is the next assertion, so this
    # list growing again still requires a reviewer to say why.
    assert set(fields) - counters == {
        "container_id",
        "state",
        "cursor_stored",
        "resynced",
        "planned",
    }


def test_a_planned_filing_can_hold_no_message_content() -> None:
    """The dry-run preview is printed to a terminal. Identifiers only (#66)."""
    fields = watcher.PlannedFiling.model_fields
    assert set(fields) == {"message_id", "folder", "deterministic"}
    with pytest.raises(ValueError, match="Extra inputs"):
        watcher.PlannedFiling(
            message_id="m1",
            folder="Review",
            deterministic=False,
            subject="leaked",  # type: ignore[call-arg]
        )


def test_the_summary_is_frozen_and_forbids_extra_fields() -> None:
    """A completed round is a fact, and a field nobody declared is unreviewed."""
    summary = watcher.poll_once(_context(FakeProvider(ChangeSet())))
    with pytest.raises(ValueError, match="frozen"):
        summary.applied = 99  # type: ignore[misc]
    with pytest.raises(ValueError, match="Extra inputs"):
        watcher.RoundSummary(**{**summary.model_dump(), "subject": "leaked"})


def test_a_serialised_summary_carries_no_subject_sender_or_snippet() -> None:
    """The round that just ran read a message; its summary must not repeat it."""
    provider = FakeProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    summary = watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    rendered = summary.model_dump_json()
    assert "Quarterly review notes" not in rendered
    assert "someone@example.com" not in rendered
    assert "attached" not in rendered
