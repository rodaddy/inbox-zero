"""Eviction is tied to cursor COMMITMENT, not to pass history alone (#161 round 3).

WHAT ROUND 3 REFUTED
    Sizing the trim target from the last ``PASS_VOLUME_HISTORY`` pass volumes
    fixed the 501-id cliff for a mailbox that keeps delivering big rounds, and
    left it wide open for the one that stops. The target DECAYS while the
    window still holds the big pass: one 501-id pass followed by ten one-id
    trickle rounds ages the 501 out of the history, drops the target back to
    ``SEEN_LIMIT``, and the next ``begin_pass`` evicts eleven live ids. With a
    cursor withheld on an unfiled message (``round._settle_cursor`` re-offers
    the whole page on any unexhausted FAILED message) those eleven are
    re-offered immediately, marked again, and evicted again on the round after
    -- a permanent 100-110-ids-per-round replay ring on a 600-id backlog.

    A mid-pass crash reaches the same ring by a different door. ``mark_seen``
    checkpoints the running pass's volume as it goes, so a watcher killed a
    hundred ids into a 600-id page persists ``pass_volume`` = 100 next to a
    window holding 100 ids of an UNCOMMITTED round. Nothing about that figure
    authorises trimming, because the cursor never moved: the provider will
    offer all 600 again.

THE INVARIANT THESE TESTS PIN
    An id may age out of the window only once the stream's cursor has COMMITTED
    past the round that produced it. While a cursor is withheld the window must
    not shrink below its current occupancy -- those ids are exactly the set the
    provider is about to re-offer. Once ``record_cursor`` runs, the history rule
    resumes and a spike decays as before.
"""

from __future__ import annotations

from inbox_zero_connector.config import PROVIDER_NAMES
from inbox_zero_connector.services.watch.state import (
    PASS_VOLUME_HISTORY,
    SEEN_LIMIT,
    WatchState,
)

INBOX = "AAMkAGI2-inbox"

GOOGLE, _MICROSOFT = PROVIDER_NAMES


def _run_pass(state: WatchState, ids: list[str], *, commit: bool = False) -> list[str]:
    """One polling pass over ``ids``: skip what is guarded, mark what is not.

    ``commit=False`` is the WITHHELD cursor -- the round found a message it
    could not file, so ``round._settle_cursor`` kept the old position and the
    same page comes back next round. ``commit=True`` is the clean round that
    stores the provider's new token.

    Returns the ids the pass treated as NEW, which is exactly the set a real
    round would have paid a classifier call and a provider write for.
    """
    state.begin_pass()
    processed = [message_id for message_id in ids if not state.seen_message(INBOX, message_id)]
    for message_id in processed:
        state.mark_seen(INBOX, message_id)
    if commit:
        state.record_cursor(INBOX, f"token-after-{len(state.seen.get(INBOX, []))}")
    return processed


def test_a_withheld_cursor_survives_the_history_decaying_under_it() -> None:
    """REFUTATION (a): big pass + trickle rounds must not evict the big pass.

    The 501-id page is marked once and never committed, because one message on
    it will not file. Ten one-id trickle rounds then push the 501 out of the
    volume history. The target computed from history alone is back at 500 --
    but the cursor has not moved, so the provider re-offers all 501 and every
    one of them must still be guarded.
    """
    page = [f"page-{index}" for index in range(SEEN_LIMIT + 1)]
    state = WatchState()

    assert len(_run_pass(state, page)) == SEEN_LIMIT + 1
    for round_index in range(PASS_VOLUME_HISTORY + 2):
        _run_pass(state, [f"trickle-{round_index}"])

    assert _run_pass(state, page) == []


def test_the_withheld_ring_does_not_reopen_round_after_round() -> None:
    """The ring is permanent, so one re-offer is not enough to prove it closed.

    A 600-id backlog re-offered on three consecutive withheld rounds after the
    history has decayed must reprocess nothing on any of them.
    """
    page = [f"backlog-{index}" for index in range(SEEN_LIMIT + 100)]
    state = WatchState()

    _run_pass(state, page)
    for round_index in range(PASS_VOLUME_HISTORY + 2):
        _run_pass(state, [f"trickle-{round_index}"])

    assert _run_pass(state, page) == []
    assert _run_pass(state, page) == []
    assert _run_pass(state, page) == []


def test_a_partial_pass_volume_from_a_crash_does_not_authorise_a_trim() -> None:
    """REFUTATION (b): a mid-pass crash checkpoints a volume it must not be trusted for.

    The watcher is killed partway into a 600-id page: ``mark_seen`` has already
    persisted a partial ``pass_volume`` and the ids it covers, and the cursor
    never moved. Once trickle rounds age that partial figure out of the history
    the target is back at ``SEEN_LIMIT`` and ``begin_pass`` is free to evict the
    overshoot. It must not: the provider is about to re-offer all 600, and the
    partial window is the only guard the ids it holds have.
    """
    page = [f"crash-{index}" for index in range(SEEN_LIMIT + 100)]
    marked = page[: SEEN_LIMIT + 50]
    state = WatchState()
    state.begin_pass()
    for message_id in marked:
        state.mark_seen(INBOX, message_id)
    state.save(GOOGLE)

    reloaded = WatchState.load(GOOGLE)
    for round_index in range(PASS_VOLUME_HISTORY + 2):
        _run_pass(reloaded, [f"trickle-{round_index}"])

    reprocessed = _run_pass(reloaded, page)

    assert [message_id for message_id in reprocessed if message_id in marked] == []


def test_the_window_may_decay_once_the_cursor_commits() -> None:
    """The floor is a WITHHELD-cursor guarantee, not a new high-water mark.

    Same spike, but the round that produced it stored its token: the provider
    will never offer that page again, so the ids are ordinary carryover and the
    history rule is free to age them out. Without this the fix would be the
    never-decaying high-water mark ``PASS_VOLUME_HISTORY`` exists to refuse.
    """
    state = WatchState()
    _run_pass(state, [f"burst-{index}" for index in range(SEEN_LIMIT + 200)], commit=True)

    for round_index in range(PASS_VOLUME_HISTORY + 2):
        _run_pass(state, [f"quiet-{round_index}"], commit=True)
    state.begin_pass()

    assert len(state.seen[INBOX]) <= SEEN_LIMIT


def test_the_floor_is_per_stream() -> None:
    """One container withholding its cursor must not pin another's window.

    Graph runs one stream per folder with independently-settled cursors, so a
    folder stuck on an unfileable message is routine while its neighbours keep
    committing. Pinning the neighbour's window off the stuck one's state would
    make one bad message hold the whole mailbox's memory open.
    """
    stuck = "folder-stuck"
    healthy = "folder-healthy"
    state = WatchState()

    state.begin_pass()
    for index in range(SEEN_LIMIT + 50):
        state.mark_seen(stuck, f"stuck-{index}")
    for index in range(SEEN_LIMIT + 50):
        state.mark_seen(healthy, f"healthy-{index}")
    state.record_cursor(healthy, "healthy-token")

    for round_index in range(PASS_VOLUME_HISTORY + 2):
        state.begin_pass()
        state.mark_seen(healthy, f"quiet-{round_index}")
        state.record_cursor(healthy, "healthy-token")
    state.begin_pass()

    assert len(state.seen[stuck]) == SEEN_LIMIT + 50
    assert len(state.seen[healthy]) <= SEEN_LIMIT
