"""Structurally suspect state is REJECTED, not coerced into a working watcher (#188).

WHAT #188 FOUND
    The #182 guard is confirmed and is not what this file is about: a state file
    whose shape pydantic REFUSES already takes the starting-fresh path and
    nothing crashes. The residue is the file pydantic ACCEPTS. With no
    constraints on the fields, the model's job was only to name types, and
    pydantic's ordinary coercion then made a suspect file into a valid object:

      * ``pass_volume`` ``{'c1': ['7']}`` loads as ``[7]``
      * ``generations`` ``-3`` loads as ``-3`` -- a resync counter that ran
        backwards, which no code path can produce
      * ``attempts`` ``-5`` loads as ``-5``, handing a poison message a budget
        of ten rounds instead of five
      * ``WatchCursor.token`` ``''`` loads and is PRESERVED, which contradicts
        ``record_cursor``'s own docstring -- a cursor is REMOVED rather than
        blanked, precisely so nothing replays an empty string as a token
      * a non-string key in ``seen`` loads under its stringified form, so the
        window is addressed by a key no caller will ever look up

    None of these crash. Every one of them is a watcher running on numbers that
    no correct write produced, which is the definition of state worth refusing.

THE RULE THESE TESTS PIN
    A structurally suspect file takes the SAME path as a structurally corrupt
    one: ``ValidationError``, caught by the existing ``load`` guard, one WARNING,
    fresh state, full resync. That path is already first-class and already
    tested (``test_watchstate_corrupt_load.py``); this widens what reaches it
    rather than adding a second recovery.

    Rejecting is right rather than clamping because a negative count is not a
    number to be repaired -- nothing knows what it SHOULD have been, and a
    clamp to 0 silently invents a budget that is just as unfounded as the -5.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from loguru import logger
from pydantic import ValidationError

from inbox_zero_connector.config import PROVIDER_NAMES
from inbox_zero_connector.services.watch.state import WatchCursor, WatchState, state_path

INBOX = "AAMkAGI2-inbox"

GOOGLE, _MICROSOFT = PROVIDER_NAMES

SUSPECT_PAYLOADS: dict[str, object] = {
    "negative pass volume": {"pass_volume": {INBOX: [-4]}},
    "stringified pass volume": {"pass_volume": {INBOX: ["7"]}},
    "negative generation": {"generations": {INBOX: -3}},
    "negative attempt count": {"attempts": {INBOX: {"msg-1": -5}}},
    "stringified attempt count": {"attempts": {INBOX: {"msg-1": "5"}}},
    "empty cursor token": {
        "cursors": {INBOX: {"container_id": INBOX, "token": "", "cursor_generation": 1}}
    },
    "negative cursor generation": {
        "cursors": {INBOX: {"container_id": INBOX, "token": "t", "generation": -1}}
    },
    "negative schema version": {"schema_version": -1},
}
"""Files that PARSE and are objects, and that the un-constrained model accepted."""


def _write_state(payload: object) -> None:
    """Put ``payload`` on disk as this provider's state file."""
    target = state_path(GOOGLE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("shape", sorted(SUSPECT_PAYLOADS))
def test_a_structurally_suspect_state_file_starts_fresh(shape: str) -> None:
    """THE #188 REPRO: values no correct write produces must not load as valid.

    Each payload is well-formed JSON and an object, so it clears both of the
    older guards and reaches ``model_validate``. Before the constraints, every
    one of them produced a usable ``WatchState`` carrying the suspect value. The
    required outcome is the empty state the corrupt path already returns.

    Compared against a DEFAULT-CONSTRUCTED state rather than field by field, so
    a payload whose suspect value lives in a field the assertions forgot cannot
    pass by omission -- which is how the ``schema_version`` case first passed
    while still loading as -1.
    """
    _write_state(SUSPECT_PAYLOADS[shape])

    state = WatchState.load(GOOGLE)

    assert state.model_dump() == WatchState().model_dump()


def test_a_non_string_seen_key_is_rejected() -> None:
    """An integer key in ``seen`` is refused rather than stringified into place.

    Driven at ``model_validate`` rather than through a file, because JSON cannot
    carry a non-string object key at all -- ``json.dumps`` stringifies it before
    the parser ever sees it. The dict here is what a hand-edit or a non-JSON
    writer produces, and it must not load: a window addressed by a key
    ``_store_key`` will never generate is a window whose ids are unreachable and
    therefore silently unguarded.

    This one holds without a new constraint and is pinned anyway, because it is
    a property of the declared key type that a later ``ConfigDict`` change could
    quietly relax.
    """
    with pytest.raises(ValidationError):
        WatchState.model_validate({"seen": {7: ["msg-1"]}})


def test_a_negative_schema_version_is_rejected() -> None:
    """A version that ran backwards is refused like any other impossible count.

    Nothing reads ``schema_version`` for a decision today, which is exactly why
    it is worth constraining now rather than after something does: a file
    claiming version -1 was written by nothing this connector ships, and that
    provenance is the whole signal.
    """
    with pytest.raises(ValidationError):
        WatchState.model_validate({"schema_version": -1})


def test_a_negative_pass_volume_is_rejected() -> None:
    """The named case from the issue, driven at the model rather than the file.

    A pass cannot have delivered a negative number of messages, so the value can
    only come from a corrupt write -- and it feeds ``_window_target`` directly,
    where ``max(SEEN_LIMIT, ...)`` would swallow it and report a healthy target
    from a broken history.
    """
    with pytest.raises(ValidationError):
        WatchState.model_validate({"pass_volume": {INBOX: [-1]}})


def test_a_negative_attempt_count_is_rejected() -> None:
    """A negative retry budget is the one with teeth: it EXTENDS the bound.

    ``round._count_attempt`` compares the running total against
    ``MAX_FILING_ATTEMPTS``, so an attempts entry of -5 gives a permanently
    unfileable message ten rounds of cursor pinning instead of five -- the #158
    defect, running for twice as long, from a value nothing legitimate writes.
    """
    with pytest.raises(ValidationError):
        WatchState.model_validate({"attempts": {INBOX: {"msg-1": -5}}})


def test_an_empty_cursor_token_is_rejected() -> None:
    """``record_cursor`` says a cursor is REMOVED rather than blanked. Enforce it.

    ``resync_required`` pops the cursor precisely so ``cursor_for`` reports the
    honest "no cursor" instead of an empty string a caller might replay as a
    token. A file carrying ``token=''`` reintroduced the blanked cursor the code
    refuses to create, and the model was the only thing that could refuse it.
    """
    with pytest.raises(ValidationError):
        WatchCursor(token="", container_id=INBOX)


def test_the_rejection_is_announced_like_any_other_corruption() -> None:
    """Same WARNING and same wording as the #182 path, because it IS that path.

    The value of routing here rather than adding a recovery is that operators
    already know this line. A second, quieter mechanism for "the state was
    wrong" is a second thing to learn and a second thing to miss.
    """
    _write_state({"attempts": {INBOX: {"msg-1": -5}}})

    records: list[Any] = []
    handle = logger.add(records.append, level="WARNING", format="{message}")
    try:
        WatchState.load(GOOGLE)
    finally:
        logger.remove(handle)

    messages = [record.record["message"] for record in records]
    assert any("starting fresh" in message for message in messages)
    assert any(str(state_path(GOOGLE)) in message for message in messages)


def test_every_value_the_connector_itself_writes_still_loads() -> None:
    """The constraints must refuse only what no correct write produces.

    A guard that also refuses the connector's own output is worse than none: it
    would throw away a healthy cursor on every restart and full-resync forever.
    So this drives the real writers -- a stored cursor, a marked id, a counted
    attempt, a recorded pass volume, a resync -- saves, and reloads.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_seen(INBOX, "msg-1")
    state.record_attempt(INBOX, "msg-2")
    state.record_cursor(INBOX, "delta-token-1")
    state.resync_required(INBOX)
    state.record_cursor(INBOX, "delta-token-2")
    state.save(GOOGLE)

    loaded = WatchState.load(GOOGLE)

    assert loaded.cursor_for(INBOX) is not None
    assert loaded.seen_message(INBOX, "msg-1")
    assert loaded.attempts_for(INBOX, "msg-2") == 1
    assert loaded.generation_for(INBOX) == 1
    assert loaded.pass_volume[INBOX] == [1]


def test_zero_is_a_legitimate_value_everywhere_it_appears() -> None:
    """The bound is ``ge=0``, not ``gt=0``, and every zero here is real.

    ``_record_quiet_streams`` files a literal 0 for a stream that delivered
    nothing, a never-resynced stream reports generation 0, and a cursor minted
    before the generation stamp existed carries ``cursor_generation`` 0 -- which
    ``CURSOR_GENERATION``'s docstring calls load-bearing. Excluding zero would
    reject the connector's own honest output.
    """
    state = WatchState.model_validate({
        "pass_volume": {INBOX: [0]},
        "generations": {INBOX: 0},
        "attempts": {INBOX: {"msg-1": 0}},
        "cursors": {
            INBOX: {
                "container_id": INBOX,
                "token": "t",
                "generation": 0,
                "cursor_generation": 0,
            }
        },
    })

    assert state.pass_volume[INBOX] == [0]
    assert state.generations[INBOX] == 0
