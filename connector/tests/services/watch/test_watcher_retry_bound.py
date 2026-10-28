"""The #158 retry bound -- a permanently unfileable message stops pinning the cursor.

Split from ``test_watcher.py`` when the retry-bound tests pushed that file past
the 500-code-line ceiling. Same subject, same fakes, same register: the round-2
repro (a poison message pinning the cursor forever) and the bound that ends it
at ``MAX_FILING_ATTEMPTS``, receipted as ``retry_exhausted`` and marked seen so
the cursor may advance.
"""

from __future__ import annotations

from loguru import logger

from _watch_harness import (
    FINAL_CURSOR,
    CountingClassifier,
    FakeProvider,
    _context,
    _message,
)
from inbox_zero_connector.providers.base import ChangeSet, ProviderError
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.audit import trace as trace
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.state import WatchState


class _FlakyProvider(FakeProvider):
    """Refuses the first ``fail_times`` filings of each message, then accepts.

    The transient case #158's withholding was designed for. ``fail_times``
    counts per message id, so a message that has already succeeded is never
    refused again on a later round.
    """

    def __init__(self, changes: ChangeSet, *, fail_times: int) -> None:
        super().__init__(changes)
        self.fail_times = fail_times
        self.attempts: dict[str, int] = {}

    def file_message(self, message_id: str, folder: str) -> str:
        self.filed.append((message_id, folder))
        self.attempts[message_id] = self.attempts.get(message_id, 0) + 1
        if self.attempts[message_id] <= self.fail_times:
            raise ProviderError("412")
        return folder


def _round_with_a_permanent_failure(state: WatchState) -> watcher.RoundSummary:
    """One round in which ``m1`` is offered again and refused again."""
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR),
        file_error=ProviderError("412"),
    )
    return watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))


def test_an_unfileable_message_pins_the_cursor_only_until_the_attempt_bound() -> None:
    """THE #158 round-2 repro: a bound is what stops the cursor pinning forever.

    Withholding the cursor with no attempt limit means one permanently
    unfileable message re-reads the whole window every round for the life of
    the deployment, and once the window's carryover bound is exceeded the
    messages past it are RE-FILED -- unbounded provider writes and ledger rows.
    """
    state = WatchState()

    for _ in range(watcher.MAX_FILING_ATTEMPTS - 1):
        summary = _round_with_a_permanent_failure(state)
        assert summary.cursor_stored is False
        assert summary.state is watcher.RoundState.PAGED
        assert state.seen_message(None, "m1") is False

    final = _round_with_a_permanent_failure(state)

    assert final.failed == 1
    assert final.cursor_stored is True
    assert final.state is watcher.RoundState.COMMITTED
    assert state.seen_message(None, "m1") is True
    assert state.attempts_for(None, "m1") == 0


def _drain_ordinary_traffic(state: WatchState, *, ids: int) -> None:
    """Healthy committed rounds of 200 fresh ids each, through the real round.

    What a normal mailbox does between the abandonment and the re-offer. Every
    round files everything and stores its token, so the #161-round-3 commitment
    floor releases each time and ``begin_pass`` trims the window back to the
    bound -- which is what evicts the oldest entry in it.

    ``begin_pass`` is called here for the same reason ``loop.run_pass`` calls it
    on every pass: the trim is the pass boundary's job, so a drain that skipped
    it would never evict anything and the test would pass on an artifact.
    """
    per_round = 200
    for offset in range(0, ids, per_round):
        state.begin_pass()
        messages = tuple(
            _message(f"ok-{offset + index}") for index in range(min(per_round, ids - offset))
        )
        provider = FakeProvider(ChangeSet(messages=messages, cursor=f"history-3{offset:04d}"))
        watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))
    state.begin_pass()


def test_the_exhausting_round_receipts_retry_exhausted_and_never_sees_it_again() -> None:
    """The receipt is the record; the mail stays in the inbox, unfiled.

    "NEVER AGAIN" IS ASSERTED ACROSS EVICTION (#185 point 2). Asserting it only
    on the very next round proves nothing about the bound: the abandoned id is
    still inside the replay window at that point, so the skip is the window's
    doing and not the record's. This test therefore pushes 800 ordinary ids --
    four polls of a normal mailbox -- through the real round first, so the
    window has provably let the id go before the provider offers it again.
    """
    state = WatchState()
    for _ in range(watcher.MAX_FILING_ATTEMPTS):
        _round_with_a_permanent_failure(state)

    codes = [row.error_code for row in ledger.read_all()]
    assert codes[-1] == watcher.RETRY_EXHAUSTED
    assert codes.count(watcher.RETRY_EXHAUSTED) == 1
    assert all(row.status is ledger.Status.FAILED for row in ledger.read_all())

    _drain_ordinary_traffic(state, ids=800)
    assert state.seen_message(None, "m1") is False, "the window must genuinely evict it"

    sixth = FakeProvider(
        ChangeSet(messages=(_message("m1"),), cursor="history-2002"),
        file_error=ProviderError("412"),
    )
    summary = watcher.poll_once(_context(sixth, state=state, classifier=CountingClassifier()))

    assert summary.skipped_already_seen == 1
    assert summary.failed == 0
    assert sixth.filed == []


def test_eviction_never_re_arms_the_attempt_budget() -> None:
    """A SECOND full budget after eviction is the #185.2 defect, stated directly.

    The probe measured it end to end: 800 ordinary ids evicted the abandoned id,
    the provider re-offered it, and the round handed it a complete second
    ``MAX_FILING_ATTEMPTS`` -- a second ``retry_exhausted`` ledger row and four
    more rounds of a pinned cursor, repeating every ``SEEN_LIMIT`` messages for
    the life of the deployment.

    The ruling is that eviction never re-arms; only a corrupt-state reset does.
    So five further poison rounds must produce no attempt, no receipt, and no
    withheld cursor at all.
    """
    state = WatchState()
    for _ in range(watcher.MAX_FILING_ATTEMPTS):
        _round_with_a_permanent_failure(state)
    _drain_ordinary_traffic(state, ids=800)
    assert state.seen_message(None, "m1") is False

    for _ in range(watcher.MAX_FILING_ATTEMPTS):
        summary = _round_with_a_permanent_failure(state)
        assert summary.skipped_already_seen == 1
        assert summary.failed == 0
        assert summary.cursor_stored is True

    codes = [row.error_code for row in ledger.read_all()]
    assert codes.count(watcher.RETRY_EXHAUSTED) == 1
    assert state.attempts_for(None, "m1") == 0


def _stream_context(
    provider: FakeProvider, *, state: WatchState, container_id: str | None
) -> watcher.RoundContext:
    """A round context for ONE named folder stream.

    Built here rather than through the shared ``_context`` helper because that
    one pins ``container_id=None`` -- Gmail's only shape -- and the cross-stream
    question is by definition about two DIFFERENT container ids on one provider.
    Everything else is the harness's own defaults, so what differs between the
    two streams in these tests is the folder and nothing else.
    """
    base = _context(provider, state=state, classifier=CountingClassifier())
    return base.model_copy(update={"container_id": container_id})


def _poison_round_on(state: WatchState, *, container_id: str | None) -> watcher.RoundSummary:
    """One round on ``container_id`` in which ``m1`` is offered and refused."""
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR),
        file_error=ProviderError("412"),
    )
    return watcher.poll_once(_stream_context(provider, state=state, container_id=container_id))


def test_a_sibling_folder_stream_does_not_restart_an_abandoned_message() -> None:
    """THE CROSS-STREAM RE-ARM (#185.2 round 5): abandon on folder-A, offer on folder-B.

    Graph runs one stream per folder and a message can be offered by more than
    one of them, so a verdict readable only under the stream that WROTE it left
    folder-B free to spend a complete fresh ``MAX_FILING_ATTEMPTS`` on the same
    provider message id -- the issue's own "an exhausted id restarts at attempt
    0", reached with no window eviction and no corrupt file involved at all.

    Driven through the real round on both streams, because the defect is in what
    ``_process_message`` asks the state, not in the state's storage shape.
    """
    state = WatchState()
    for _ in range(watcher.MAX_FILING_ATTEMPTS):
        _poison_round_on(state, container_id="folder-A")
    before = [row.error_code for row in ledger.read_all()].count(watcher.RETRY_EXHAUSTED)
    assert before == 1

    other = FakeProvider(
        ChangeSet(messages=(_message("m1"),), cursor="history-B-1"),
        file_error=ProviderError("412"),
    )
    summary = watcher.poll_once(_stream_context(other, state=state, container_id="folder-B"))

    # No attempt is spent: the provider is never asked to file it again.
    assert other.filed == []
    assert summary.skipped_already_seen == 1
    assert summary.failed == 0
    assert summary.cursor_stored is True
    assert state.attempts_for("folder-B", "m1") == 0
    assert [row.error_code for row in ledger.read_all()].count(watcher.RETRY_EXHAUSTED) == 1


def test_the_mailbox_wide_stream_also_honours_a_folder_stream_s_verdict() -> None:
    """The mailbox-wide key is a stream too, and it is Gmail's ONLY one.

    A verdict written under a folder must be visible to the mailbox-wide reader
    and the reverse, or the default provider is the shape left uncovered -- the
    same reasoning the per-stream storage test applies to the write side.
    """
    state = WatchState()
    for _ in range(watcher.MAX_FILING_ATTEMPTS):
        _poison_round_on(state, container_id="folder-A")

    wide = FakeProvider(
        ChangeSet(messages=(_message("m1"),), cursor="history-wide-1"),
        file_error=ProviderError("412"),
    )
    summary = watcher.poll_once(_stream_context(wide, state=state, container_id=None))

    assert wide.filed == []
    assert summary.skipped_already_seen == 1
    assert summary.failed == 0


def test_the_permanent_skip_is_logged_once_per_stream_and_message() -> None:
    """The round must not be SILENTLY healthy about a permanent skip (#185.2 r5).

    Abandonment is permanent until a state reset, so a message whose refusal
    later clears is never filed by this watcher again: it stays in the inbox,
    the summary counts it ``skipped_already_seen``, and the round reports
    healthy. That is the deliberate trade -- see ``MAX_FILING_ATTEMPTS`` -- but
    an operator asking "why is this message still here?" needs an answer that is
    not "read the ledger."

    ONCE per stream and message, not once per round: a permanently abandoned
    message is offered by every poll, so an unlatched line is the same sentence
    a thousand times a day, which is how a real signal gets filtered out.
    """
    state = WatchState()
    for _ in range(watcher.MAX_FILING_ATTEMPTS):
        _poison_round_on(state, container_id="folder-A")

    lines: list[str] = []
    sink_id = logger.add(lines.append, level="DEBUG", format="{message}")
    try:
        for _ in range(3):
            _poison_round_on(state, container_id="folder-A")
        after_one_stream = [line for line in lines if "filing was abandoned" in line]
        _poison_round_on(state, container_id="folder-B")
        after_two_streams = [line for line in lines if "filing was abandoned" in line]
    finally:
        logger.remove(sink_id)

    assert len(after_one_stream) == 1, f"expected one line for three rounds, got {after_one_stream}"
    assert "folder-A" in after_one_stream[0]
    # A second stream reaching the same skip is its own fact, not a duplicate.
    assert len(after_two_streams) == 2, after_two_streams
    assert "folder-B" in after_two_streams[1]


def test_a_transient_failure_files_normally_and_leaves_no_attempt_entry() -> None:
    """Two refusals then a success: the bound never fires and nothing is pruned late."""
    state = WatchState()
    provider = _FlakyProvider(
        ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR),
        fail_times=2,
    )

    for _ in range(2):
        failing = watcher.poll_once(
            _context(provider, state=state, classifier=CountingClassifier())
        )
        assert failing.failed == 1
        assert failing.cursor_stored is False
    assert state.attempts_for(None, "m1") == 2

    summary = watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))

    assert summary.applied == 1
    assert summary.failed == 0
    assert summary.cursor_stored is True
    assert state.attempts_for(None, "m1") == 0
    assert state.seen_message(None, "m1") is True
    assert watcher.RETRY_EXHAUSTED not in [row.error_code for row in ledger.read_all()]


def test_a_fresh_failure_beside_an_exhausted_one_still_withholds_the_cursor() -> None:
    """An UNEXHAUSTED failure governs the round: ``m2`` has retries left."""
    state = WatchState()
    for _ in range(watcher.MAX_FILING_ATTEMPTS - 1):
        provider = FakeProvider(
            ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR),
            file_error=ProviderError("412"),
        )
        watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))

    mixed = FakeProvider(
        ChangeSet(messages=(_message("m1"), _message("m2")), cursor=FINAL_CURSOR),
        file_error=ProviderError("412"),
    )
    summary = watcher.poll_once(_context(mixed, state=state, classifier=CountingClassifier()))

    assert summary.failed == 2
    assert summary.cursor_stored is False
    assert summary.state is watcher.RoundState.PAGED
    assert state.seen_message(None, "m1") is True
    assert state.seen_message(None, "m2") is False
    assert state.attempts_for(None, "m2") == 1
