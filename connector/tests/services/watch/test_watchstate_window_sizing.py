"""The replay window must be big enough to hold the largest recent pass (#161 A1).

WHAT ROUND 2 FOUND, AND WHY THE FIRST FIX WAS NOT ENOUGH
    Wave 2 stopped ``mark_seen`` evicting the marks of the pass that made them,
    so a 550-id page no longer ate itself DURING the pass. It moved the defect
    one pass later instead. ``begin_pass`` trimmed every window back to a flat
    ``SEEN_LIMIT``, so the 50 ids over the bound were dropped the instant the
    next pass started -- and then that next pass, offered the same 550-id page
    again, marked its own 550 and dropped them again on the pass after.

    Measured cliff: a 500-id round reprocesses 0%, a 501-id round reprocesses
    100%, EVERY round, for as long as the backlog is that size. On a 600-id
    backlog that is 600 billed classifier calls and 600 provider writes per
    round.

THE SIZING RULE THIS FILE PINS
    The trim target is not a bigger constant -- a constant is the same cliff
    moved to a new number. It is sized from OBSERVED round volume: the window
    keeps at least as many ids as the largest recent pass recorded, so one
    round can never evict its own ids before the next poll sees them. Quiet
    rounds pull the target back down to ``SEEN_LIMIT``, so the growth is
    temporary and bounded by what the mailbox actually delivered.

AMENDED BY ROUND 3: THE DECAY IS CONDITIONAL ON A COMMITTED CURSOR
    Sizing from history alone was refuted -- the target decays while the window
    still holds an uncommitted pass, and the trim then evicts exactly the ids a
    withheld cursor re-offers. The rule these tests pin is unchanged in the
    direction they check (a spike is not held forever); what changed is that a
    round must STORE ITS TOKEN before its ids become evictable, which is why the
    decay test commits. The withheld half lives in
    ``test_watchstate_window_commitment.py``.
"""

from __future__ import annotations

import pytest

from inbox_zero_connector.config import PROVIDER_NAMES
from inbox_zero_connector.services.watch.state import (
    PASS_VOLUME_HISTORY,
    SEEN_LIMIT,
    WatchState,
)

INBOX = "AAMkAGI2-inbox"

GOOGLE, _MICROSOFT = PROVIDER_NAMES


def _run_pass(state: WatchState, ids: list[str]) -> list[str]:
    """One polling pass over ``ids``: skip what is guarded, mark what is not.

    Returns the ids the pass treated as NEW -- which is exactly the set a real
    round would have paid a classifier call and a provider write for.
    """
    state.begin_pass()
    processed = [message_id for message_id in ids if not state.seen_message(INBOX, message_id)]
    for message_id in processed:
        state.mark_seen(INBOX, message_id)
    return processed


@pytest.mark.parametrize("page_size", [SEEN_LIMIT + 1, SEEN_LIMIT + 100])
def test_a_round_larger_than_the_window_does_not_reprocess_itself(page_size: int) -> None:
    """THE A1 REPRO: >500 ids reprocessed 100% of themselves on every round.

    The same page is offered three times, which is what a provider replay or a
    withheld cursor produces. Rounds two and three must process NOTHING: every
    id on the page was marked in round one and the window is not allowed to
    evict them before the page is offered again.
    """
    page = [f"page-{index}" for index in range(page_size)]
    state = WatchState()

    first = _run_pass(state, page)
    second = _run_pass(state, page)
    third = _run_pass(state, page)

    assert len(first) == page_size
    assert second == []
    assert third == []


def test_the_cliff_at_the_bound_is_gone() -> None:
    """500 -> 0% and 501 -> 100% was the measured cliff. Both must be 0%."""
    at_bound = [f"at-{index}" for index in range(SEEN_LIMIT)]
    over_bound = [f"over-{index}" for index in range(SEEN_LIMIT + 1)]

    quiet = WatchState()
    _run_pass(quiet, at_bound)
    loud = WatchState()
    _run_pass(loud, over_bound)

    assert _run_pass(quiet, at_bound) == []
    assert _run_pass(loud, over_bound) == []


def test_a_big_round_still_guards_itself_after_a_restart() -> None:
    """The sizing must live in the persisted state, not in a process-local field.

    A watcher restarted between rounds inherits the file and nothing else. If
    the observed-volume figure were a ``PrivateAttr`` the reloaded state would
    trim back to the flat bound on its first ``begin_pass`` and replay the whole
    page -- the defect, surviving a restart.
    """
    page = [f"page-{index}" for index in range(SEEN_LIMIT + 100)]
    state = WatchState()
    _run_pass(state, page)
    state.save(GOOGLE)

    reloaded = WatchState.load(GOOGLE)

    assert _run_pass(reloaded, page) == []


def test_the_window_shrinks_back_to_the_bound_once_the_big_round_is_history() -> None:
    """The growth is temporary. A quiet mailbox must not keep paying for a spike.

    After the burst, ordinary small rounds pull the trim target back down, so
    the window returns to ``SEEN_LIMIT`` instead of holding the high-water mark
    for the life of the deployment.

    THE DECAY IS NOW CONDITIONED ON THE CURSOR COMMITTING (#161 round 3), which
    is why each round here stores a token. History sizing alone was refuted: the
    target decayed while the window still held an UNCOMMITTED pass, so the trim
    evicted ids the provider was about to re-offer. A committed round is what
    turns those ids into ordinary carryover, and the decay this test pins is the
    behaviour AFTER that -- see ``test_watchstate_window_commitment.py`` for the
    withheld half.
    """
    state = WatchState()
    _run_pass(state, [f"burst-{index}" for index in range(SEEN_LIMIT + 200)])
    state.record_cursor(INBOX, "burst-committed")

    for round_index in range(PASS_VOLUME_HISTORY + 2):
        _run_pass(state, [f"quiet-{round_index}"])
        state.record_cursor(INBOX, f"quiet-committed-{round_index}")
    # Measured at the boundary. Mid-pass the window carries the running pass's
    # own marks on top of the target, by design -- see ``SEEN_LIMIT``.
    state.begin_pass()

    assert len(state.seen[INBOX]) <= SEEN_LIMIT


def test_the_window_never_trims_below_the_bound_on_a_quiet_mailbox() -> None:
    """Sizing from volume must raise the target, never lower it below the floor."""
    state = WatchState()
    for index in range(SEEN_LIMIT):
        _run_pass(state, [f"msg-{index}"])

    assert len(state.seen[INBOX]) == SEEN_LIMIT
    assert state.seen_message(INBOX, "msg-0")
