"""A corrupt-state reset re-arms the poison-message budget, and SAYS so (#185).

WHAT #185 FOUND
    The #158 attempt bound is confirmed and is not what this file is about: a
    permanently unfileable message spends ``round.MAX_FILING_ATTEMPTS`` and then
    stops pinning the cursor. The residue is that the budget has no re-arm path
    short of clearing state -- and the #182 fresh-start recovery IS clearing
    state. ``WatchState.load`` discards ``attempts`` and ``seen`` together, so a
    corrupt state file hands an already-exhausted message a budget of 0 again
    and the round re-offers it. Repeated corruption is therefore repeated
    five-round cursor-pin cycles.

WHY THE RE-ARM IS KEPT, NOT REMOVED
    Because the alternative is worse and the re-arm is bounded. Persisting the
    budget across the discard would mean salvaging counts out of a file the
    module has ALREADY judged untrustworthy -- the same bytes that failed
    validation, read selectively, and believed. There is no second source: the
    budget lives inside the very object being thrown away, and a separate file
    for it would need its own corruption story and could itself disagree with
    the state it describes.

    And carrying a STALE bound the other way orphans mail. An attempts entry is
    the record of a provider refusing a specific message; after a state loss
    nothing knows whether that provider is still refusing it. Re-arming retries
    a message that may now file, and costs at most one more bounded cycle -- five
    rounds, receipted, mail never lost. Refusing to retry it abandons a message
    that would have filed, on the strength of a count from a corrupt file.

    So the re-arm is the correct behaviour and the DEFECT was that it happened
    silently: an operator seeing a second five-round pin cycle had nothing
    connecting it to the reset that caused it.

WHAT THIS FILE PINS
    The re-arm itself (behaviour, unchanged), and the WARNING that already fires
    on the reset naming the attempt budget among what was discarded.

    The line names the budget but not the NUMBER, deliberately.
    ``MAX_FILING_ATTEMPTS`` lives in ``watch.round`` because "how many tries are
    enough" is a policy of the thing doing the trying, and ``round`` imports
    ``state`` -- so quoting the figure here would either invert that layering or
    duplicate the constant into the module that must not own it. The state layer
    reports what it discarded; the round layer owns what the bound is.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from inbox_zero_connector.config import PROVIDER_NAMES
from inbox_zero_connector.services.watch.round import MAX_FILING_ATTEMPTS
from inbox_zero_connector.services.watch.state import WatchState, state_path

INBOX = "AAMkAGI2-inbox"

POISON = "msg-that-never-files"

GOOGLE, _MICROSOFT = PROVIDER_NAMES


def _exhausted_state() -> WatchState:
    """A state whose poison message has spent its whole budget and not been marked.

    Stops one short of ``mark_seen`` deliberately: ``mark_seen`` prunes the
    attempts entry, so a state that had gone all the way through ``_exhaust``
    would carry no budget to lose and could not show the re-arm at all.
    """
    state = WatchState()
    for _ in range(MAX_FILING_ATTEMPTS):
        state.record_attempt(INBOX, POISON)
    return state


def _write_corrupt_state() -> None:
    """Valid JSON, valid object, shape the model refuses -- the #182 fresh-start path."""
    target = state_path(GOOGLE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"seen": {INBOX: "not-a-list"}}), encoding="utf-8")


def _warnings_from_load() -> list[str]:
    """Load the state file, returning every WARNING-or-above message loguru emitted.

    A loguru SINK rather than ``caplog``, because this package logs through
    loguru and loguru does not propagate to the stdlib ``logging`` tree
    ``caplog`` reads -- these assertions would otherwise pass or fail on an
    always-empty list.
    """
    records: list[Any] = []
    handle = logger.add(records.append, level="WARNING", format="{message}")
    try:
        WatchState.load(GOOGLE)
    finally:
        logger.remove(handle)
    return [record.record["message"] for record in records]


def test_the_budget_survives_an_ordinary_save_and_reload() -> None:
    """The #158 persistence guarantee, restated as the baseline this contrasts with.

    A healthy restart must NOT re-arm: that is the whole point of ``attempts``
    being a serialized field rather than a ``PrivateAttr``. Without this here,
    the re-arm test below could pass against a build that had simply stopped
    persisting the budget at all.
    """
    _exhausted_state().save(GOOGLE)

    loaded = WatchState.load(GOOGLE)

    assert loaded.attempts_for(INBOX, POISON) == MAX_FILING_ATTEMPTS


def test_a_corrupt_state_file_re_arms_the_budget() -> None:
    """The re-arm is REAL and is the documented behaviour, not an accident.

    Pinned as an assertion so a later change cannot half-fix it -- salvaging the
    counts out of a rejected file would flip this, and the reasoning against
    that is in this module's docstring.
    """
    _exhausted_state().save(GOOGLE)
    _write_corrupt_state()

    loaded = WatchState.load(GOOGLE)

    assert loaded.attempts_for(INBOX, POISON) == 0


def test_the_reset_warning_names_the_attempt_budget() -> None:
    """THE #185 FIX: the line that already fires must say the budget went too.

    Before this, the reset announced "state is structurally corrupt, starting
    fresh" and nothing more. An operator then watched a message they had already
    seen abandoned start a second five-round cursor-pin cycle with no stated
    cause, because the one line that could have connected the two did not
    mention the budget it had just discarded.
    """
    _exhausted_state().save(GOOGLE)
    _write_corrupt_state()

    messages = _warnings_from_load()

    assert messages, "a corrupt state file must be announced, not swallowed"
    reset = [message for message in messages if "starting fresh" in message]
    assert len(reset) == 1, f"expected one reset line, got {reset}"
    assert "attempt budget" in reset[0]
    assert "cursors" in reset[0] and "replay window" in reset[0]


def test_the_warning_says_what_the_re_armed_budget_costs() -> None:
    """The line must be actionable, not merely accurate.

    Naming the discarded budget without saying what follows from it leaves the
    operator with a fact and no reading. What follows is bounded and worth
    stating in the same breath: an already-abandoned message may be retried, for
    at most one more bounded cycle, and no mail is lost.
    """
    _exhausted_state().save(GOOGLE)
    _write_corrupt_state()

    reset = [message for message in _warnings_from_load() if "starting fresh" in message]

    assert "retried" in reset[0]


def test_an_unreadable_file_carries_the_same_reset_line() -> None:
    """The other corruption shape resets the same things and must say the same thing.

    ``load`` has two fresh-start doors -- bytes that will not parse, and bytes
    whose shape the model refuses -- and both discard the identical state. A
    line on only one of them would make the diagnosis depend on which way the
    file happened to break.
    """
    _exhausted_state().save(GOOGLE)
    state_path(GOOGLE).write_text("{not json at all", encoding="utf-8")

    reset = [message for message in _warnings_from_load() if "starting fresh" in message]

    assert len(reset) == 1, f"expected one reset line, got {reset}"
    assert "attempt budget" in reset[0]


def test_a_healthy_load_says_nothing_about_the_budget() -> None:
    """A restart that kept the budget must not print a line saying it lost one.

    The line is the signal that a bounded pin cycle is about to recur. Emitting
    it on every ordinary restart is how an operator learns to skip it before the
    round that matters.
    """
    _exhausted_state().save(GOOGLE)

    assert [message for message in _warnings_from_load() if "attempt budget" in message] == []
