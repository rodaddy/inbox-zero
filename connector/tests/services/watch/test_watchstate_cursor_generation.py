"""Cursors minted before the #143 fix are dropped, not replayed.

WHY A GENERATION AND NOT A MIGRATION. The cursors written by every build before
the container-id fix are not merely stale, they are WRONG: on Microsoft Graph a
per-folder key holds an ``me/mailFolders/inbox/messages/delta`` link, because
``poll_once`` polled with no container. Replaying one keeps the defect running
forever after the code is fixed -- the stored link already encodes the inbox, so
no amount of correctness downstream re-points it. There is also nothing to
migrate TO: the token is opaque, shared code may not parse or rewrite it, and
the only honest position for a stream in that state is "no position."

Dropping them is safe because the recovery is a path the watcher already
supports. A missing cursor is the ordinary state of every stream before its
first round and after every token expiry, so the drop produces exactly ONE cold
start per stream, and the replay guard (``seen``, deliberately preserved across
a resync) suppresses the re-read that cold start returns.

The counter is ``CURSOR_GENERATION`` -- a fact about the CODE that minted a
cursor -- and it is not ``schema_version``: the file's LAYOUT did not change and
a reader of the old file parses it perfectly. The defect is in the VALUES.
"""

from __future__ import annotations

import json

import pytest

from inbox_zero_connector.config import PROVIDER_NAMES
from inbox_zero_connector.services.watch.state import (
    CURSOR_GENERATION,
    MAILBOX_WIDE_KEY,
    WatchState,
    state_path,
)

GOOGLE, _MICROSOFT = PROVIDER_NAMES
INBOX = "AAMkAGI2-inbox"
ARCHIVE = "AAMkAGI2-archive"
POISONED = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=old"
"""An INBOX delta link -- which is the poison. Filed under a FOLDER's key by a
pre-fix build, it resumes the inbox forever no matter how correct the poller
becomes."""

FRESH = "https://graph.microsoft.com/v1.0/me/mailFolders/AAMkAGI2-archive/messages/delta?$deltatoken=new"
"""A link naming the folder it was actually opened on, as the fixed poll mints."""


def test_the_cursor_generation_is_past_the_pre_fix_default() -> None:
    """0 is what an unstamped pre-fix cursor reads as, so the live value is above it."""
    assert CURSOR_GENERATION >= 1


def test_a_freshly_recorded_cursor_carries_the_current_generation() -> None:
    state = WatchState()

    cursor = state.record_cursor(INBOX, FRESH)

    assert cursor.cursor_generation == CURSOR_GENERATION


def _write_pre_fix_state(container_key: str) -> None:
    """A state file exactly as a pre-fix build wrote it: no ``cursor_generation``."""
    payload = {
        "schema_version": 1,
        "cursors": {
            container_key: {
                "token": POISONED,
                "container_id": None if container_key == MAILBOX_WIDE_KEY else container_key,
                "stored_at": "2026-08-01T00:00:00Z",
                "generation": 3,
            }
        },
        "generations": {container_key: 3},
        "seen": {container_key: ["m1", "m2"]},
        "attempts": {},
    }
    state_path(GOOGLE).write_text(json.dumps(payload), encoding="utf-8")


def test_a_pre_fix_cursor_loads_as_no_cursor() -> None:
    """The poisoned token is not replayable; the stream cold-starts once."""
    _write_pre_fix_state(ARCHIVE)

    reloaded = WatchState.load(GOOGLE)

    assert reloaded.cursor_for(ARCHIVE) is None, (
        "a cursor minted against the wrong delta path must never be replayed"
    )


def test_a_pre_fix_state_file_is_not_an_error() -> None:
    """Old state LOADS. It does not raise, and it is not discarded wholesale."""
    _write_pre_fix_state(ARCHIVE)

    reloaded = WatchState.load(GOOGLE)

    assert reloaded.seen_message(ARCHIVE, "m1"), (
        "the replay guard is what makes the forced cold start safe -- dropping it "
        "would guarantee the reprocessing it exists to prevent"
    )
    assert reloaded.generation_for(ARCHIVE) == 4, (
        "the forced drop is a resync and moves the resync counter like any other"
    )


def test_the_mailbox_wide_cursor_is_dropped_the_same_way() -> None:
    """Gmail's single cursor is stamped and dropped by the same rule."""
    _write_pre_fix_state(MAILBOX_WIDE_KEY)

    reloaded = WatchState.load(GOOGLE)

    assert reloaded.cursor_for(None) is None


def test_the_drop_happens_once_and_the_next_save_reloads_clean() -> None:
    """ONE clean resync per stream, not a cold start on every restart."""
    _write_pre_fix_state(ARCHIVE)
    reloaded = WatchState.load(GOOGLE)
    reloaded.record_cursor(ARCHIVE, FRESH)
    reloaded.save(GOOGLE)

    again = WatchState.load(GOOGLE)
    cursor = again.cursor_for(ARCHIVE)

    assert cursor is not None
    assert cursor.token == FRESH


@pytest.mark.parametrize("stamped", [CURSOR_GENERATION, CURSOR_GENERATION + 1])
def test_a_current_or_newer_cursor_survives_the_load(stamped: int) -> None:
    """Only cursors from BEFORE the fix are dropped."""
    state = WatchState()
    state.record_cursor(ARCHIVE, FRESH)
    state.cursors[ARCHIVE] = state.cursors[ARCHIVE].model_copy(
        update={"cursor_generation": stamped}
    )
    state.save(GOOGLE)

    cursor = WatchState.load(GOOGLE).cursor_for(ARCHIVE)

    assert cursor is not None
    assert cursor.token == FRESH
