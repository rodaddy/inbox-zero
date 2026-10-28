"""A structurally corrupt state file starts fresh instead of killing the watcher (#182).

WHAT THE GUARD USED TO MISS
    ``WatchState.load`` caught ``OSError`` and ``json.JSONDecodeError``, which
    between them cover "the file will not open" and "the bytes are not JSON."
    Neither covers the third and more likely corruption: bytes that ARE valid
    JSON and an object, but whose SHAPE the model rejects -- a cursor whose
    ``token`` is a number, a ``seen`` map holding a string where a list belongs,
    a field truncated away by a half-written upgrade. ``model_validate`` raises
    ``pydantic.ValidationError``, which is neither of the caught types, so it
    travelled straight past the starting-fresh guard and out of ``load``.

    The blast radius is the whole watcher: ``load`` runs at the top of every
    round, so an unattended deployment stops polling entirely and the only
    signal is a traceback in a log nobody is reading. The recovery it refused
    to take -- start fresh, full resync -- is the SAME recovery the module
    already performs for the other two corruption shapes and for an expired
    provider token. There is no state here worth crashing to protect.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from loguru import logger

from inbox_zero_connector.config import PROVIDER_NAMES
from inbox_zero_connector.services.watch.state import WatchState, state_path

INBOX = "AAMkAGI2-inbox"

GOOGLE, _MICROSOFT = PROVIDER_NAMES

CORRUPT_PAYLOADS: dict[str, object] = {
    "cursor is not an object": {"cursors": {INBOX: "a-bare-string-token"}},
    "cursor field has the wrong type": {
        "cursors": {INBOX: {"container_id": INBOX, "token": 12345, "cursor_generation": 1}}
    },
    "seen window is not a list": {"seen": {INBOX: "msg-1"}},
    "attempt count is not a number": {"attempts": {INBOX: {"msg-1": "lots"}}},
    "generations map holds a string": {"generations": {INBOX: "two"}},
    "top level field is the wrong container": {"cursors": ["not", "a", "mapping"]},
}


def _write_state(payload: object) -> None:
    target = state_path(GOOGLE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("shape", sorted(CORRUPT_PAYLOADS))
def test_a_structurally_corrupt_state_file_starts_fresh(shape: str) -> None:
    """THE #182 REPRO: valid JSON, wrong shape, ``ValidationError`` out of ``load``.

    Each payload parses as JSON and is an object, so it clears both existing
    guards and reaches ``model_validate``. The required outcome is the empty
    state every other corruption path returns -- never an exception.
    """
    _write_state(CORRUPT_PAYLOADS[shape])

    state = WatchState.load(GOOGLE)

    assert state.cursors == {}
    assert state.cursor_for(INBOX) is None
    assert state.seen == {}


def test_the_corrupt_shape_is_announced_with_the_same_log_shape() -> None:
    """Same level and same "starting fresh" wording as the unparseable path.

    A quieter log here would make the more likely corruption the harder one to
    diagnose: the operator sees a full resync with no stated cause.
    """
    _write_state({"seen": {INBOX: "msg-1"}})

    records: list[Any] = []
    handle = logger.add(records.append, level="WARNING", format="{message}")
    try:
        WatchState.load(GOOGLE)
    finally:
        logger.remove(handle)

    assert records, "a corrupt state file must be announced, not swallowed"
    messages = [record.record["message"] for record in records]
    assert any("starting fresh" in message for message in messages)
    assert any(str(state_path(GOOGLE)) in message for message in messages)


def test_a_valid_state_file_still_loads_after_the_widened_guard() -> None:
    """The widened except must not swallow a healthy file into a fresh start."""
    healthy = WatchState()
    healthy.record_cursor(INBOX, "delta-token-1")
    healthy.mark_seen(INBOX, "msg-1")
    healthy.save(GOOGLE)

    loaded = WatchState.load(GOOGLE)

    assert loaded.cursor_for(INBOX) is not None
    assert loaded.seen_message(INBOX, "msg-1")
