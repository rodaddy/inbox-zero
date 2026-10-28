"""The uncommitted replay-window floor has a CEILING, and says when it hits it (#194).

WHAT #194 FOUND
    The #161-round-3 commitment floor (``WatchState._window_target``) is
    self-releasing only if ``record_cursor`` eventually runs. It floors the trim
    target at current occupancy for as long as ``uncommitted`` is set, and
    ``uncommitted`` is cleared by exactly one event: a completed round storing
    its token. A stream whose commit can never happen -- the provider refusing
    the cursor write with a 4xx, a state file that went read-only, a round that
    aborts before ``_settle_cursor`` on every pass -- therefore raises the floor
    on every round and never lowers it. Measured on the round-3 probe: 50 rounds
    x 200 marks with no commit gave window occupancy 10,000 against a target of
    10,000. That is the unbounded growth ``SEEN_LIMIT`` exists to refuse,
    reached through a different door, and it grew silently.

WHAT THIS FILE PINS
    A hard ceiling at ``UNCOMMITTED_WINDOW_CAP`` (4 x ``SEEN_LIMIT``), and ONE
    WARNING per stream when a stream first reaches it, naming the stream key and
    the fact that its cursor has not committed.

    The floor's semantics BELOW the cap are untouched, and these tests say so
    explicitly: a stream under the ceiling must still keep every id it marked
    since its last commit, because evicting one of those is the permanent replay
    ring round 3 closed. The cap is a second-order bound on a stream that is
    already broken -- past it, the honest trade flips, because a window that
    grows without limit costs the file, the memory, and every process that loads
    it, and the operator has no signal that anything is wrong.

    The cap is a CARRYOVER target, like the bound it caps. ``mark_seen`` may
    never evict an id the current pass recorded (#161b), so occupancy may sit
    one pass above the cap until the next ``begin_pass``, and these tests measure
    at the pass boundary for that reason.

WHAT THE FIRST PASS OF #194 MISSED -- THE CAP WAS A FLOOR (round 5)
    ``_window_target`` returned ``max(target, UNCOMMITTED_WINDOW_CAP)``, and
    ``target`` already carried ``max(pass_volume)``. That keeps the LARGER of the
    two, so any pass bigger than the cap raised the target to the pass size: the
    constant behaved as a minimum and never as a maximum.

    Every test below used to drive ``_uncommitted_rounds``, which marks
    ``per_round`` genuinely FRESH ids each round and never re-offers what the cap
    evicted. On that shape ``pass_volume`` stays at ``per_round``, ``target``
    stays at ``SEEN_LIMIT``, and the cap appears to hold -- so the suite was green
    against a constant that did not bound anything.

    A WITHHELD CURSOR MEANS RE-OFFER, and that is the shape the defect needs. An
    uncommitted stream is by definition one the provider replays from the last
    committed position, so ids the cap evicted come back next round, are marked
    AGAIN, inflate ``pass_volume``, and raise the target that permits a bigger
    window. Measured at HEAD before the clamp, marking 200 fresh ids per round on
    top of a re-offered backlog: 25 rounds settled at 2,600, 50 at 5,000, 100 at
    10,000, 200 at 20,000 -- ten times the cap, growing O(backlog) rather than
    O(cap), and with exactly ONE warning across the whole run because the latch
    fires at first crossing and says nothing as the window goes on to multiply it.

    ``test_the_cap_holds_when_evicted_ids_are_re_offered`` and its siblings pin
    that shape, modelled on ``test_watchstate_window_commitment._run_pass``. The
    fresh-id tests are KEPT beside them: they are the same claim on the easier
    input, and dropping them would lose the regression that first proved the cap
    exists at all.

    THE SIZES ARE THE SMALLEST THAT ACTUALLY REPRODUCE, and they were chosen by
    measurement rather than by eye. The re-offer harness rescans a growing backlog
    each round, so it costs O(rounds^2 x per_round) in the TEST while the code
    under test is unchanged -- 200 x 200 took 259 seconds for one assertion. Below
    a certain size the defect does not appear at all: with the clamp reverted,
    60 x 50 and 80 x 50 both settle at exactly the cap and pass. 60 x 100 settles
    at 3,000 and fails in 1.4 seconds, so that is what these tests use. A test
    tuned under the reproducing threshold is worse than no test, because it is
    green for the wrong reason.

    THE REPROCESSING COST GOES UP, NOT DOWN, and that is the trade rather than a
    regression. The unbounded window was buying fewer reprocessings with an
    unbounded file; past the cap ``UNCOMMITTED_WINDOW_CAP`` says that trade
    flips, because a bounded backlog reprocessed is recoverable and an unbounded
    state file in an unattended process is not.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from _watch_harness import CountingClassifier, FakeProvider, _context, _message
from inbox_zero_connector.config import PROVIDER_NAMES
from inbox_zero_connector.providers.base import ChangeSet
from inbox_zero_connector.services.watch import loop as watchloop
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import (
    SEEN_LIMIT,
    UNCOMMITTED_WINDOW_CAP,
    WatchState,
)

INBOX = "AAMkAGI2-inbox"

ARCHIVE = "AAMkAGI2-archive"

GOOGLE, _MICROSOFT = PROVIDER_NAMES

MAILBOX_WIDE = "\x00mailbox-wide"
"""The store key a mailbox-wide stream (``container_id=None``) writes under.

Gmail exposes one stream for the whole mailbox, so the state file keys it by the
module's own ``MAILBOX_WIDE_KEY`` rather than by a provider container id. Spelled
here as the literal the file actually carries, because a loop-shaped test that
looked the key up from the module under test could agree with a wrong one.
"""


@pytest.fixture
def captured_warnings() -> Iterator[list[str]]:
    """Every WARNING-or-above line loguru emitted, as plain strings.

    A loguru SINK rather than pytest's ``caplog``, because this package logs
    through loguru and loguru does not propagate to the stdlib ``logging`` tree
    ``caplog`` reads -- under ``caplog`` these assertions would pass or fail on
    an always-empty list, which is worse than no assertion.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING", format="{message}")
    yield lines
    logger.remove(sink_id)


@pytest.fixture
def captured_info() -> Iterator[list[str]]:
    """Every INFO-or-above line, for the drain bound (#82).

    A SEPARATE fixture from ``captured_warnings`` rather than a level change on
    it, because the level is the claim: pausing a drain on a stream that is
    behind is ordinary backpressure, not a warning. The pathology it defends
    against -- a never-committing stream -- already has its own WARNING, and
    raising this one would make every deep-backlog round look like a fault.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="INFO", format="{message}")
    yield lines
    logger.remove(sink_id)


def _uncommitted_rounds(state: WatchState, *, rounds: int, per_round: int, key: str) -> None:
    """Poll ``rounds`` times, marking ``per_round`` fresh ids, never committing.

    The shape of a stream whose cursor write always fails: real work is done and
    recorded every round, and ``record_cursor`` is never reached, so
    ``uncommitted`` stays set from the first mark onward.
    """
    for round_index in range(rounds):
        state.begin_pass()
        for offset in range(per_round):
            state.mark_seen(key, f"{key}-r{round_index}-m{offset}")


def _reoffered_rounds(state: WatchState, *, rounds: int, per_round: int, key: str) -> int:
    """Poll ``rounds`` times over a GROWING backlog the provider keeps re-offering.

    The honest shape of a withheld cursor, and the one the fresh-id helper above
    cannot express. ``round._settle_cursor`` keeps the old position whenever a
    message could not be filed, so the next poll is offered the whole page again
    -- including every id the window has since evicted. Each round therefore
    re-marks what fell out, which counts toward ``pass_volume`` a second time.

    Modelled on ``test_watchstate_window_commitment._run_pass``: skip what the
    window still guards, mark what it does not, never commit. ``per_round`` fresh
    ids arrive on top each round, so the backlog grows the way a stuck stream's
    does rather than staying a fixed page.

    Returns the total number of ids treated as NEW across the run -- in
    production one classifier call and one provider write each.
    """
    backlog: list[str] = []
    reprocessed = 0
    for round_index in range(rounds):
        backlog.extend(f"{key}-r{round_index}-m{offset}" for offset in range(per_round))
        state.begin_pass()
        guarded = set(state.seen.get(key, []))
        fresh = [message_id for message_id in backlog if message_id not in guarded]
        reprocessed += len(fresh)
        for message_id in fresh:
            state.mark_seen(key, message_id)
    return reprocessed


def test_the_cap_is_four_times_the_bound() -> None:
    """The ceiling is stated as a multiple of the bound it is a ceiling on.

    Pinned as its own assertion so a later edit that retunes ``SEEN_LIMIT``
    cannot leave the cap at a number that no longer relates to it.
    """
    assert UNCOMMITTED_WINDOW_CAP == 4 * SEEN_LIMIT


def test_a_never_committing_stream_stops_growing_at_the_cap() -> None:
    """THE #194 REPRO: 50 rounds x 200 marks with no commit must not reach 10,000.

    Every round marks 200 genuinely new ids and never stores a token, which is
    what a permanently failing cursor write looks like from this module. Before
    the cap the window held all 10,000; the required outcome is that it settles
    at the ceiling and stays there.
    """
    state = WatchState()

    _uncommitted_rounds(state, rounds=50, per_round=200, key=INBOX)
    state.begin_pass()

    assert len(state.seen[INBOX]) <= UNCOMMITTED_WINDOW_CAP


def test_the_cap_holds_across_many_further_rounds() -> None:
    """Reaching the ceiling once is not the claim; staying there is.

    A cap applied only on the round that crosses it would let the floor start
    climbing again from the capped occupancy on the next round, which is the
    same unbounded growth with one flat spot in it. Measured at the pass
    BOUNDARY both times, because the cap is a carryover target and not a
    mid-pass occupancy limit -- see ``test_the_cap_is_a_carryover_target``.
    """
    state = WatchState()

    _uncommitted_rounds(state, rounds=20, per_round=200, key=INBOX)
    state.begin_pass()
    first = len(state.seen[INBOX])
    _uncommitted_rounds(state, rounds=20, per_round=200, key=INBOX)
    state.begin_pass()

    assert first <= UNCOMMITTED_WINDOW_CAP
    assert len(state.seen[INBOX]) <= UNCOMMITTED_WINDOW_CAP


def test_the_cap_is_a_carryover_target_like_the_bound_it_caps() -> None:
    """Mid-pass the window may sit one pass ABOVE the cap, and that is the rule.

    ``SEEN_LIMIT`` is documented as a carryover target rather than a hard
    occupancy limit, because ``mark_seen`` may never evict an id the CURRENT
    pass recorded -- that eviction is the #161b self-defeat, where a page larger
    than the bound evicted every mark it had just made and replayed in full. The
    cap inherits that rule rather than overriding it: capping mid-pass would
    reintroduce exactly the defect at a new number.

    So the honest invariant is bounded overshoot, reasserted at the next pass
    boundary: at most one pass's marks above the cap, never unbounded growth.
    """
    state = WatchState()

    _uncommitted_rounds(state, rounds=30, per_round=200, key=INBOX)
    mid_pass = len(state.seen[INBOX])
    state.begin_pass()

    assert mid_pass <= UNCOMMITTED_WINDOW_CAP + 200
    assert len(state.seen[INBOX]) <= UNCOMMITTED_WINDOW_CAP


def test_hitting_the_cap_warns_once_naming_the_stream_and_the_cause(
    captured_warnings: list[str],
) -> None:
    """ONE warning, naming the stream key and the uncommitted cursor.

    Silence was half the defect: the growth was a deliberate trade nobody could
    observe. One line per stream is the whole budget -- ``_window_target`` runs
    on every mark and every trim, so a line per call would emit thousands per
    round and bury the signal it exists to raise.
    """
    state = WatchState()

    _uncommitted_rounds(state, rounds=40, per_round=200, key=INBOX)
    state.begin_pass()

    capped = [line for line in captured_warnings if "replay window" in line and "cap" in line]
    assert len(capped) == 1, f"expected exactly one cap warning, got {capped}"
    assert INBOX in capped[0]
    assert "has not committed" in capped[0]


def test_the_warning_is_per_stream(captured_warnings: list[str]) -> None:
    """Two stuck streams are two facts, so they are two lines.

    Graph runs one stream per folder with independently-settled cursors. A
    once-per-process latch would report the first stuck folder and hide every
    other, which is the wrong half of the trade the once-per-stream rule makes.
    """
    state = WatchState()

    _uncommitted_rounds(state, rounds=40, per_round=200, key=INBOX)
    _uncommitted_rounds(state, rounds=40, per_round=200, key=ARCHIVE)
    state.begin_pass()

    capped = [line for line in captured_warnings if "replay window" in line and "cap" in line]
    assert len(capped) == 2, f"expected one cap warning per stream, got {capped}"
    assert {INBOX, ARCHIVE} == {INBOX if INBOX in line else ARCHIVE for line in capped}


def test_a_healthy_stream_is_never_warned_about(captured_warnings: list[str]) -> None:
    """A stream that commits every round must never see the cap or the line.

    The floor is self-releasing for a working stream, so it never approaches the
    ceiling -- and an operator who is warned about a healthy mailbox learns to
    ignore the line before the broken mailbox arrives.
    """
    state = WatchState()

    for round_index in range(50):
        state.begin_pass()
        for offset in range(200):
            state.mark_seen(INBOX, f"ok-r{round_index}-m{offset}")
        state.record_cursor(INBOX, f"delta-token-{round_index}")
    state.begin_pass()

    assert len(state.seen[INBOX]) <= SEEN_LIMIT
    assert [line for line in captured_warnings if "cap" in line] == []


def test_the_floor_below_the_cap_is_unchanged() -> None:
    """UNDER the ceiling the #161-round-3 guarantee still holds in full.

    One uncommitted page of ``SEEN_LIMIT`` + 100 ids, then enough trickle rounds
    to age its volume out of the history: with the cursor still withheld the
    provider re-offers the whole page, so not one of those ids may have been
    evicted. This is the test the cap must not break -- it is the defect round 3
    closed, and a cap applied too eagerly reopens it.
    """
    page = [f"page-{index}" for index in range(SEEN_LIMIT + 100)]
    state = WatchState()
    state.begin_pass()
    for message_id in page:
        state.mark_seen(INBOX, message_id)
    for round_index in range(20):
        state.begin_pass()
        state.mark_seen(INBOX, f"trickle-{round_index}")

    state.begin_pass()

    assert all(state.seen_message(INBOX, message_id) for message_id in page)


def test_committing_after_the_cap_releases_the_floor_as_before() -> None:
    """A stuck stream that recovers goes back to the ordinary history rule.

    The cap bounds the damage while the cursor is stuck; it does not latch the
    stream into a permanent state. Once ``record_cursor`` runs, ``uncommitted``
    clears and the window trims to the history target like any other stream.
    """
    state = WatchState()

    _uncommitted_rounds(state, rounds=40, per_round=200, key=INBOX)
    state.record_cursor(INBOX, "delta-token-recovered")
    for round_index in range(20):
        state.begin_pass()
        state.mark_seen(INBOX, f"quiet-{round_index}")
        state.record_cursor(INBOX, f"delta-token-quiet-{round_index}")
    state.begin_pass()

    assert len(state.seen[INBOX]) <= SEEN_LIMIT


def test_a_pass_bigger_than_the_cap_cannot_raise_the_target_above_it() -> None:
    """THE ROUND-5 REPRO, at its smallest: one pass of 3,000 must not target 3,000.

    ``_window_target`` used to return ``max(target, UNCOMMITTED_WINDOW_CAP)`` with
    ``target`` already carrying ``max(pass_volume)``, so a single pass larger than
    the ceiling raised the trim target to the pass size and the constant acted as
    a minimum. Measured at HEAD before the clamp: 3,000.

    Asserted on ``_window_target`` rather than on occupancy because this is the
    exact arithmetic that was wrong, and a test that only watched the window
    could be satisfied by a trim that happened to run before the next mark.
    """
    state = WatchState()
    state.begin_pass()
    for offset in range(UNCOMMITTED_WINDOW_CAP + 1000):
        state.mark_seen(INBOX, f"backlog-{offset}")

    assert state._window_target(INBOX) == UNCOMMITTED_WINDOW_CAP


def test_the_cap_holds_when_evicted_ids_are_re_offered() -> None:
    """THE ROUND-5 REPRO: a re-offering run must not settle above the cap.

    A withheld cursor re-offers what the window evicted, so the ids the cap drops
    come straight back, are marked again, and inflate ``pass_volume`` -- which is
    what used to raise the target that permitted a bigger window. Measured at HEAD
    before the clamp, at the sizes used here: 3,000 against a cap of 2,000, and at
    the full 200 x 200 shape 20,000 -- ten times the cap, growing with the backlog
    rather than settling.

    The fresh-id helper cannot show this, and that is why the shipped suite was
    green: it never re-offers, so ``pass_volume`` stays at ``per_round`` and the
    target never exceeds ``SEEN_LIMIT``.
    """
    state = WatchState()

    _reoffered_rounds(state, rounds=60, per_round=100, key=INBOX)
    state.begin_pass()

    assert len(state.seen[INBOX]) <= UNCOMMITTED_WINDOW_CAP


def test_the_re_offered_window_does_not_grow_with_the_backlog() -> None:
    """A longer run must not settle at a bigger window.

    The pre-clamp growth was O(backlog), not O(cap) -- at 200 fresh ids per round,
    50 rounds gave 5,000 and 100 gave 10,000 -- so a single-length measurement
    could be passed by any number that happened to be large enough. Comparing two
    lengths is what pins the SHAPE: a real ceiling gives the same window for both,
    and at the sizes here the pre-clamp pair was 2,000 against 3,000.
    """
    short = WatchState()
    long = WatchState()

    _reoffered_rounds(short, rounds=40, per_round=100, key=INBOX)
    short.begin_pass()
    _reoffered_rounds(long, rounds=60, per_round=100, key=INBOX)
    long.begin_pass()

    assert len(long.seen[INBOX]) == len(short.seen[INBOX])
    assert len(long.seen[INBOX]) <= UNCOMMITTED_WINDOW_CAP


def test_the_re_offered_window_survives_save_and_load_bounded() -> None:
    """The bound is on the FILE, so it has to hold across a round trip.

    #194's cost is an unbounded state file, an unbounded parse on every round,
    and unbounded memory in an unattended process. The pre-clamp window persisted
    and reloaded at its full 20,000, so measuring only the in-memory list would
    miss the thing the cap exists to bound.

    THE ORDERING IS THE CALLER'S, AND IT USED TO BE THE TEST'S (round 6). This
    test ran ``state.begin_pass()`` immediately before ``state.save()``, and no
    caller anywhere performs that sequence: ``loop.run_pass`` opens the pass with
    ``begin_pass`` and then runs the checkpoint in the per-stream ``finally``
    INSIDE it, so every real save is mid-pass. The inserted trim was the sole
    reason the file measured bounded -- remove it at HEAD and the same workload
    persisted 6,000 ids / 193 KB against a cap of 2,000. A test keyed on a shape
    its caller cannot produce is green for the wrong reason, so this now marks,
    then saves, exactly as the loop does.
    """
    state = WatchState()

    _reoffered_rounds(state, rounds=60, per_round=100, key=INBOX)
    state.save(GOOGLE)

    reloaded = WatchState.load(GOOGLE)

    assert len(reloaded.seen[INBOX]) <= UNCOMMITTED_WINDOW_CAP


def test_a_mid_pass_save_persists_no_more_than_the_next_begin_pass_would_keep() -> None:
    """The file holds what a reader would keep, not what the pass is carrying.

    The reload's FIRST statement is ``begin_pass``, which trims to
    ``_window_target`` -- so any id written beyond that target is discarded before
    anything reads it. Writing it is pure cost: an unbounded file, an unbounded
    parse every round, in an unattended process.

    Asserted against the in-memory window in the same breath, because the claim is
    not merely "the file is small": it is that the file is smaller than the live
    pass BY the amount the reload would have thrown away.
    """
    state = WatchState()

    _reoffered_rounds(state, rounds=60, per_round=100, key=INBOX)
    mid_pass = len(state.seen[INBOX])
    state.save(GOOGLE)

    reloaded = WatchState.load(GOOGLE)

    assert mid_pass > UNCOMMITTED_WINDOW_CAP, "the workload must actually overshoot"
    assert len(reloaded.seen[INBOX]) == UNCOMMITTED_WINDOW_CAP
    assert len(state.seen[INBOX]) == mid_pass, "saving must not evict live ids"


def test_the_persisted_window_keeps_the_most_recent_ids() -> None:
    """The file's trim takes from the same end ``begin_pass`` does: oldest first.

    Newest-first would be catastrophic rather than merely wasteful -- it discards
    exactly the ids the next poll is most likely to re-offer while keeping the ones
    furthest from being replayed, inverting what the window is for. Pinned on
    identity rather than on length, because a count cannot tell the two apart.
    """
    state = WatchState()
    state.begin_pass()
    for offset in range(UNCOMMITTED_WINDOW_CAP + 700):
        state.mark_seen(INBOX, f"backlog-{offset:06d}")

    state.save(GOOGLE)
    reloaded = WatchState.load(GOOGLE)

    assert reloaded.seen[INBOX] == state.seen[INBOX][-UNCOMMITTED_WINDOW_CAP:]


def test_the_file_stops_growing_with_the_backlog() -> None:
    """A longer stuck run must not write a bigger file.

    The measured shape at HEAD was O(backlog): 3,000 ids at 15 rounds, 6,000 at
    30, 12,000 at 60 -- doubling the rounds doubled the file, to six times the cap.
    Comparing two lengths is what pins O(cap); a single measurement is passed by
    any number that happens to be big enough.
    """
    short = WatchState()
    long = WatchState()

    _reoffered_rounds(short, rounds=30, per_round=100, key=INBOX)
    short.save(GOOGLE)
    short_persisted = len(WatchState.load(GOOGLE).seen[INBOX])
    _reoffered_rounds(long, rounds=60, per_round=100, key=INBOX)
    long.save(GOOGLE)
    long_persisted = len(WatchState.load(GOOGLE).seen[INBOX])

    assert long_persisted == short_persisted
    assert long_persisted <= UNCOMMITTED_WINDOW_CAP


def test_a_committed_stream_is_persisted_untouched() -> None:
    """The file's trim is for UNCOMMITTED streams only, and that is #161b.

    A committed stream's target comes from history alone and can legitimately sit
    BELOW what the running pass has marked -- one 900-id pass after a run of quiet
    rounds targets ``SEEN_LIMIT``. Trimming that at the write would drop the
    current pass's own marks into the file, which is the self-defeat
    ``mark_seen``'s carve-out refuses, relocated from the window to the disk.
    """
    state = WatchState()
    state.begin_pass()
    for offset in range(SEEN_LIMIT + 400):
        state.mark_seen(INBOX, f"page-{offset}")
    state.record_cursor(INBOX, "delta-token-committed")

    state.save(GOOGLE)
    reloaded = WatchState.load(GOOGLE)

    assert reloaded.seen[INBOX] == state.seen[INBOX]
    assert len(reloaded.seen[INBOX]) == SEEN_LIMIT + 400


def test_a_healthy_state_round_trips_byte_identical() -> None:
    """A save that changes nothing must write what the object holds.

    The bound is a repair for a broken stream, not a filter every write runs
    through: an ordinary state -- committed cursors, an in-flight uncommitted
    stream nowhere near the ceiling, retry budgets, abandonment records -- has to
    survive the round trip unchanged, or the file has quietly become a lossy view
    of the object.
    """
    state = WatchState()
    state.begin_pass()
    for offset in range(120):
        state.mark_seen(INBOX, f"inbox-{offset}")
    state.record_cursor(INBOX, "delta-token-inbox")
    for offset in range(40):
        state.mark_seen(ARCHIVE, f"archive-{offset}")
    state.record_attempt(ARCHIVE, "archive-stuck")
    state.mark_exhausted(ARCHIVE, "archive-given-up")

    path = state.save(GOOGLE)
    first = path.read_text()
    WatchState.load(GOOGLE).save(GOOGLE)

    assert path.read_text() == first
    assert WatchState.load(GOOGLE).seen == state.seen


def test_saving_does_not_evict_ids_from_the_live_window() -> None:
    """A checkpoint is a copy of the state, never an event in the pass.

    If a save evicted live ids, the replay guard would depend on checkpoint
    TIMING: ``loop`` runs the checkpoint in the per-stream ``finally``, so an
    id marked before one stream finished would vanish from the window the next
    stream in the same pass consults. That is the #161b harm through a third door.
    """
    state = WatchState()
    state.begin_pass()
    marked = [f"live-{offset:06d}" for offset in range(UNCOMMITTED_WINDOW_CAP + 500)]
    for message_id in marked:
        state.mark_seen(INBOX, message_id)

    state.save(GOOGLE)

    assert state.seen[INBOX] == marked
    assert all(state.seen_message(INBOX, message_id) for message_id in marked)


def test_the_cap_is_enforced_at_every_pass_boundary_not_only_the_last() -> None:
    """The bound holds on EVERY round of a re-offering run, not just at the end.

    A clamp applied only where the final assertion happens to look would let the
    window climb between boundaries and be trimmed once at the end, which reads
    as a ceiling and is not one. Checking at the top of each of 30 rounds is what
    makes "settles at the cap and stays there" a measured claim.

    MID-PASS OCCUPANCY IS NOT ASSERTED HERE because it is a different bound with
    a different mechanism -- the per-pass DRAIN, not this clamp -- and it has its
    own test: ``test_mid_pass_occupancy_is_bounded_by_the_cap_because_the_drain_is``.
    """
    state = WatchState()
    per_round = 200

    for round_index in range(30):
        state.begin_pass()
        assert len(state.seen.get(INBOX, [])) <= UNCOMMITTED_WINDOW_CAP, (
            f"round {round_index} opened above the cap"
        )
        backlog = [
            f"{INBOX}-r{earlier}-m{offset}"
            for earlier in range(round_index + 1)
            for offset in range(per_round)
        ]
        guarded = set(state.seen.get(INBOX, []))
        for message_id in backlog:
            if message_id not in guarded:
                state.mark_seen(INBOX, message_id)


class _PagingBacklogProvider(FakeProvider):
    """A stuck stream: a growing backlog, served one page at a time, never final.

    The shape #82 is about. Every poll re-offers the WHOLE backlog from the top,
    because the cursor was never stored, and the backlog grows by ``per_round``
    each poll. Pages carry ``next_page_token`` and never a cursor, which is the
    partial-read state ``ChangeSet`` already models -- so the drain is what
    decides how deep one pass goes, not the provider.
    """

    def __init__(self, *, page_size: int, per_round: int) -> None:
        super().__init__(ChangeSet(messages=(), cursor=None))
        self.page_size = page_size
        self.per_round = per_round
        self.rounds = 0

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        self.polled.append(cursor)
        self.polled_containers.append(container_id)
        if page_token is None:
            self.rounds += 1
            start = 0
        else:
            start = int(page_token)
        backlog = self.rounds * self.per_round
        end = min(start + self.page_size, backlog)
        return ChangeSet(
            messages=tuple(_message(f"m{offset:06d}") for offset in range(start, end)),
            cursor=None,
            next_page_token=str(end) if end < backlog else None,
        )


def test_mid_pass_occupancy_is_bounded_by_the_cap_because_the_drain_is() -> None:
    """THE #82 FIX: the DRAIN is bounded per pass, so mid-pass occupancy is too.

    The cap is a CARRYOVER target and can only BIND at ``begin_pass``:
    ``mark_seen`` may never evict an id the CURRENT pass recorded (#161b),
    because a page larger than the bound that evicts its own marks replays in
    full. So mid-pass occupancy is cap + (ids this pass marked), and the fix is
    to bound the second term rather than to touch the carve-out.

    ON A STUCK STREAM THAT SECOND TERM USED TO BE THE WHOLE BACKLOG.
    ``loop.run_pass`` opens ONE pass per poll and ``round.poll_once`` drained the
    provider to exhaustion inside it, so a pass WAS the entire re-offered
    backlog: this workload measured 6,000 and climbing against a cap of 2,000.
    ``round._drain_bound_reached`` now stops opening new pages once the round has
    processed ``UNCOMMITTED_WINDOW_CAP`` messages, so the pass marks at most that
    many ids and the remainder waits for the next poll.

    MEASURED AT EXACTLY THE CAP, ASSERTED AT TWICE IT, and the gap is deliberate.
    The observed peak on this workload is 2,000 against a cap of 2,000: the
    stream re-offers the same ids from the top every poll, ``mark_seen`` returns
    early on an id already in the window, and the drain stops at the cap's worth
    of PROCESSED messages -- so nothing fresh is added and the window sits at the
    carryover it opened with. The assertion allows one further cap because the
    ARITHMETIC, not this workload, is the bound: a pass whose re-offer is
    entirely NEW ids opens at the cap (carryover, trimmed by ``begin_pass``) and
    adds at most one drain's worth on top before the drain stops. Driving that to
    exactly the cap would require evicting this pass's own marks, which is #161b.
    Either way the unbounded ``+ backlog`` term is gone, which is the claim.

    Driven through ``poll_once`` rather than ``WatchState`` directly, because the
    drain is what is being asserted and it lives in the round.
    """
    per_round = 200
    provider = _PagingBacklogProvider(page_size=per_round, per_round=per_round)
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier())
    boundaries: list[int] = []
    peak = 0

    for _ in range(30):
        state.begin_pass()
        boundaries.append(len(state.seen.get(MAILBOX_WIDE, [])))
        watchloop.poll_once(context)
        peak = max(peak, len(state.seen[MAILBOX_WIDE]))

    assert max(boundaries) == UNCOMMITTED_WINDOW_CAP
    assert peak <= 2 * UNCOMMITTED_WINDOW_CAP, (
        f"mid-pass occupancy {peak} exceeded cap + one drain's worth of marks; "
        "the drain bound in round._drain_bound_reached is not holding"
    )


def test_the_drain_bound_says_so_once_and_stores_no_cursor(
    captured_info: list[str],
) -> None:
    """Pausing a drain is backpressure, and silence is what would make it loss.

    Two halves of the same claim. The line names the stream and the counts so an
    operator can see the stream is behind, and NO cursor is stored -- which is
    what makes the unfetched remainder a re-offer rather than mail that vanished
    with the position moved past it.
    """
    per_round = 200
    provider = _PagingBacklogProvider(page_size=per_round, per_round=per_round)
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier())

    for _ in range(20):
        state.begin_pass()
        summary = watchloop.poll_once(context)

    assert summary.cursor_stored is False
    assert state.cursor_for(None) is None
    bounded = [line for line in captured_info if "drain bound reached" in line]
    assert bounded, "a paused drain must leave a line saying so"
    assert f"cap={UNCOMMITTED_WINDOW_CAP}" in bounded[0]


def test_an_empty_volume_history_targets_the_bound_instead_of_crashing() -> None:
    """An EMPTY ``pass_volume`` list must read as "no observed pass", not TypeError.

    ``max(SEEN_LIMIT, *history)`` collapses to ``max(500)`` on an empty history,
    and ``max`` of one non-iterable argument raises ``TypeError``. The ``[0]``
    default covered a MISSING key, which is a different shape: the key is present
    and its value is ``[]``. ``list[Count]`` permits that, so the file passes
    ``load``'s validation and the corrupt-state fresh start never sees it.

    500, not 0, because no observed pass is exactly the state a brand-new stream
    is in, and that state keeps the bound.
    """
    state = WatchState()
    state.pass_volume[INBOX] = []

    assert state._window_target(INBOX) == SEEN_LIMIT


def test_an_empty_volume_history_does_not_take_down_begin_pass() -> None:
    """The crash's real blast radius: ``begin_pass`` is the first line of a round.

    ``_window_target`` runs once per stream inside the trim, so a ``TypeError``
    there does not degrade one stream -- it aborts the pass before any mail is
    polled, on every round, for as long as the file says ``[]``. A watcher that
    cannot start is worse than one that keeps a window it did not need to.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_seen(INBOX, "one-id")
    state.pass_volume[INBOX] = []

    state.begin_pass()

    assert state.seen_message(INBOX, "one-id")


def test_an_empty_volume_history_survives_a_round_trip_through_the_file() -> None:
    """The shape arrives from DISK, so the round trip is the path that matters.

    Nothing in this module writes an empty list -- ``_append_volume`` appends
    before it trims -- so ``[]`` reaches the code only from a hand-edited or
    foreign-written state file. That is the input the fresh-start recovery cannot
    catch, because the file is valid: the guard has to be in the arithmetic.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_seen(INBOX, "one-id")
    state.pass_volume[INBOX] = []
    state.save(GOOGLE)

    reloaded = WatchState.load(GOOGLE)
    reloaded.begin_pass()

    assert reloaded.pass_volume[INBOX] == []
    assert reloaded._window_target(INBOX) == SEEN_LIMIT


def test_the_target_never_exceeds_the_cap_while_uncommitted() -> None:
    """THE ROUND-6 PROXY REPRO: at occupancy == cap the target was still 3,000.

    The guard read ``if occupancy > UNCOMMITTED_WINDOW_CAP``, a PROXY for "the
    value about to be returned exceeds the ceiling". They differ on exactly the
    state the shipped loop produces: ``begin_pass`` trims a stuck stream to
    EXACTLY the cap, so occupancy is 2,000, the ``>`` is False, and the
    fall-through returned ``max(target, occupancy)`` with ``target`` still
    carrying a 3,000-id pass. ``mark_seen`` sizes its mid-pass overflow off that
    return, so the over-cap value was live.

    Asserted right after the trim, which is the moment the proxy and the answer
    disagree -- a test that measured only mid-pass would never see it.
    """
    state = WatchState()
    state.begin_pass()
    for offset in range(UNCOMMITTED_WINDOW_CAP + 1000):
        state.mark_seen(INBOX, f"backlog-{offset}")

    state.begin_pass()

    assert len(state.seen[INBOX]) == UNCOMMITTED_WINDOW_CAP
    assert state.pass_volume[INBOX] == [UNCOMMITTED_WINDOW_CAP + 1000]
    assert state._window_target(INBOX) == UNCOMMITTED_WINDOW_CAP


def test_the_cap_warning_fires_when_the_clamp_binds_not_when_the_window_is_long(
    captured_warnings: list[str],
) -> None:
    """The signal follows the same answer the clamp does.

    A stream whose recorded volume alone outranks the ceiling is a stream the
    operator needs told about, whether or not its window happens to have been
    trimmed to the cap a moment ago. Keying the warning on occupancy meant the
    line was owed on the round the target first exceeded the cap and could be
    withheld on later rounds where the clamp was still doing the work.
    """
    state = WatchState()
    state.begin_pass()
    for offset in range(UNCOMMITTED_WINDOW_CAP + 1000):
        state.mark_seen(INBOX, f"backlog-{offset}")
    captured_warnings.clear()
    state.begin_pass()

    state._capped_windows.clear()
    state._window_target(INBOX)

    capped = [line for line in captured_warnings if "replay window hit the cap" in line]
    assert len(capped) == 1, f"expected the clamp to announce itself, got {capped}"
    assert INBOX in capped[0]


class _CursorlessBacklogProvider(FakeProvider):
    """A mailbox that re-offers a growing backlog and never returns a cursor.

    The honest shape of a stream whose commit can never happen, expressed at the
    PROVIDER boundary instead of by calling ``WatchState`` by hand. A ``ChangeSet``
    with ``cursor=None`` and no page token is a valid final page that
    ``round._store_cursor`` refuses to store -- so ``record_cursor`` is never
    reached, ``uncommitted`` stays set from the first mark, and the provider keeps
    handing back everything it handed back last round plus ``per_round`` more.
    """

    def __init__(self, *, per_round: int) -> None:
        super().__init__(ChangeSet())
        self.per_round = per_round
        self.round_index = 0

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        self.polled.append(cursor)
        self.polled_containers.append(container_id)
        backlog = [
            _message(f"backlog-r{earlier}-m{offset:04d}")
            for earlier in range(self.round_index + 1)
            for offset in range(self.per_round)
        ]
        self.round_index += 1
        return ChangeSet(messages=tuple(backlog), cursor=None)


def test_the_loop_checkpoint_never_writes_more_than_the_cap(tmp_path: Path) -> None:
    """THE ROUND-6 REPRO, DRIVEN THROUGH ``run_pass`` WITH ITS OWN CHECKPOINT.

    Every other test in this file calls ``WatchState`` directly, and that is what
    let the file bound be pinned on an ordering no caller performs: the previous
    version of ``test_the_re_offered_window_survives_save_and_load_bounded`` ran
    ``begin_pass()`` immediately before ``save()``, and ``loop.run_pass`` calls
    ``begin_pass`` as its FIRST statement and then runs the checkpoint in the
    per-stream ``finally`` inside that same pass. So the real write is always
    mid-pass, and mid-pass on a stuck stream is the whole re-offered backlog.

    This drives the shipped call graph -- ``run_pass(context, checkpoint)`` with
    ``checkpoint`` bound to ``state.save`` exactly as ``apps.cli.main`` binds it --
    over a provider that never returns a cursor, and measures the FILE after each
    round. Measured at HEAD before the write-side trim: 6,000 ids / 193 KB against
    a cap of 2,000, growing with the backlog.

    ``resolve()`` on the temp path because ``save`` writes through
    ``Path.replace``: a symlinked temp root (macOS ``/var`` -> ``/private/var``)
    would otherwise leave the assertion reading a path the writer did not use.
    """
    path = (tmp_path / "watch-state.json").resolve()
    state = WatchState()
    context = _context(
        _CursorlessBacklogProvider(per_round=200),
        scope=MailboxScope(mode="all_folders"),
        state=state,
        classifier=CountingClassifier(),
    )
    persisted: list[int] = []

    for _ in range(20):
        watchloop.run_pass(context, lambda: state.save(GOOGLE, path))
        persisted.append(len(WatchState.load(GOOGLE, path).seen[MAILBOX_WIDE]))

    assert max(persisted) <= UNCOMMITTED_WINDOW_CAP, (
        f"the loop's own checkpoint wrote {max(persisted)} ids past a cap of "
        f"{UNCOMMITTED_WINDOW_CAP}"
    )
    assert len(state.seen[MAILBOX_WIDE]) > UNCOMMITTED_WINDOW_CAP, (
        "the workload must actually overshoot in memory, or the file bound is "
        "being asserted against a state that never reached the cap"
    )
