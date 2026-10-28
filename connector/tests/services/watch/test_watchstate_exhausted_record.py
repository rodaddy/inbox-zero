"""An EXHAUSTED message stays exhausted after the replay window evicts it (#185.2).

WHAT #185 POINT (2) FOUND
    ``round._exhaust`` recorded the abandonment by calling
    ``WatchState.mark_seen``, and ``mark_seen``'s FIRST statement is
    ``clear_attempts``. So an abandoned message carried zero attempts state and
    its ONLY record was its entry in the bounded FIFO replay window -- where, as
    the oldest entry in that window by construction, it was the first thing
    ``begin_pass`` evicted.

    Measured on the round-level probe: after 800 ordinary messages (four polls
    of a normal mailbox, or ONE oversized page of 501), the id left the window,
    the provider re-offered it, and it was handed a COMPLETE second
    ``MAX_FILING_ATTEMPTS`` budget -- a second ``retry_exhausted`` ledger row
    and four more rounds of a pinned cursor. That is the exact #158 harm the
    attempt bound exists to end, recurring on a period set by mailbox volume
    rather than by anything the bound controls, for the life of the deployment.

THE RULING THIS FILE PINS (#185 point 3)
    EVICTION NEVER RE-ARMS THE BUDGET. A corrupt-state reset is the only re-arm,
    and it is deliberate, bounded, and announced -- see ``WatchState.load`` and
    ``round.MAX_FILING_ATTEMPTS``. Abandonment is therefore recorded in a record
    of its OWN (``WatchState.exhausted``), persisted beside the cursors, bounded
    by its own generous cap, and consulted by the round BEFORE it classifies.

    The replay window's semantics are untouched. ``SEEN_LIMIT``, the #161b
    current-pass rule, the #161-round-3 commitment floor, and the #194 cap all
    behave exactly as their own tests say; this record simply does not live in
    that window.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger
from pydantic import ValidationError

from inbox_zero_connector.services.watch.state import (
    EXHAUSTED_LIMIT,
    MAILBOX_WIDE_KEY,
    SEEN_LIMIT,
    WatchState,
)

INBOX = "AAMkAGI2-inbox"

POISON = "poison-message"


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
    """Every INFO-or-above line loguru emitted, as plain strings.

    A separate sink from ``captured_warnings`` rather than a lower level on that
    one, because the two assert opposite things: the warning tests assert an
    EMPTY list for a healthy stream, and folding INFO into them would make that
    assertion fail on ordinary chatter that is not what they are about.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="INFO", format="{message}")
    yield lines
    logger.remove(sink_id)


def _ordinary_traffic(state: WatchState, *, key: str | None, count: int, tag: str) -> None:
    """Healthy committed rounds of 200 ids each, until ``count`` ids are pushed.

    The shape of a mailbox doing its ordinary work: every round marks fresh ids
    and stores its token, so the commitment floor releases every round and the
    window trims back to ``SEEN_LIMIT`` -- which is what evicts the oldest entry
    in it.
    """
    per_round = 200
    for offset in range(0, count, per_round):
        state.begin_pass()
        for index in range(min(per_round, count - offset)):
            state.mark_seen(key, f"{tag}-{offset + index}")
        state.record_cursor(key, f"delta-token-{tag}-{offset}")
    state.begin_pass()


def test_the_record_cap_is_a_multiple_of_the_replay_bound() -> None:
    """The exhausted record's ceiling is stated in terms of the bound it outlives.

    Pinned as its own assertion so a later edit that retunes ``SEEN_LIMIT``
    cannot leave a cap that no longer relates to it -- the same rule
    ``UNCOMMITTED_WINDOW_CAP`` is held to.
    """
    assert EXHAUSTED_LIMIT == 4 * SEEN_LIMIT


def test_an_exhausted_id_survives_eviction_from_the_replay_window() -> None:
    """THE #185.2 REPRO: 800 ordinary ids evict it from ``seen`` and not from here.

    Both halves are asserted deliberately. The window MUST let it go -- that is
    ``SEEN_LIMIT`` working, and a record that prevented eviction would be the
    unbounded window the bound exists to refuse. The exhausted record MUST keep
    it, because the abandonment is a fact about a message and not about a
    position in a FIFO.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_exhausted(INBOX, POISON)
    state.record_cursor(INBOX, "delta-token-abandoned")

    _ordinary_traffic(state, key=INBOX, count=800, tag="ok")

    assert state.seen_message(INBOX, POISON) is False
    assert state.message_exhausted(INBOX, POISON) is True


def test_one_oversized_pass_evicts_the_window_entry_and_not_the_record() -> None:
    """A SINGLE page of ``SEEN_LIMIT`` + 1 ids is enough, with no corruption at all.

    The probe's variant E. The abandoned id sits at the front of the window by
    construction, so it is the first thing dropped: no outage, no restart, no
    unparseable file -- one busy poll on a healthy mailbox.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_exhausted(INBOX, POISON)
    state.record_cursor(INBOX, "delta-token-abandoned")

    state.begin_pass()
    for index in range(SEEN_LIMIT + 1):
        state.mark_seen(INBOX, f"page-{index}")
    state.record_cursor(INBOX, "delta-token-page")
    state.begin_pass()

    assert state.seen_message(INBOX, POISON) is False
    assert state.message_exhausted(INBOX, POISON) is True


def test_a_quiet_mailbox_reaches_the_same_eviction_ten_ids_at_a_time() -> None:
    """The probe's variant D: the cycle is PERIODIC, not rare, and volume sets the period.

    Ten messages a poll is a small firm's mailbox. It still evicts the entry
    after ``SEEN_LIMIT`` ids -- roughly fifty polls -- so a watcher left running
    re-arms on a schedule no operator would connect to the abandonment.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_exhausted(INBOX, POISON)
    state.record_cursor(INBOX, "delta-token-abandoned")

    for poll in range(60):
        state.begin_pass()
        for index in range(10):
            state.mark_seen(INBOX, f"quiet-{poll}-{index}")
        state.record_cursor(INBOX, f"delta-token-quiet-{poll}")
    state.begin_pass()

    assert state.seen_message(INBOX, POISON) is False
    assert state.message_exhausted(INBOX, POISON) is True


def test_the_record_is_kept_per_stream_including_the_mailbox_wide_key() -> None:
    """``container_id=None`` is Gmail's ONLY shape, so it cannot be the untested one.

    Gmail stores one mailbox-wide cursor because ``history.list`` is mailbox-wide;
    Graph stores one per folder. A record keyed only for the prefixed case would
    leave the default provider uncovered, which is the shape the probe's variant
    F drove.

    The WRITE is what this pins as per-stream: the verdict lands under the
    mailbox-wide key and under no other. What may be READ from it is a different
    question with a different answer -- see the cross-stream test below.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_exhausted(None, POISON)
    state.record_cursor(None, "history-abandoned")

    _ordinary_traffic(state, key=None, count=800, tag="ok")

    assert state.seen_message(None, POISON) is False
    assert state.message_exhausted(None, POISON) is True
    assert state.exhausted == {MAILBOX_WIDE_KEY: [POISON]}


def test_a_sibling_stream_does_not_restart_an_abandoned_message_at_attempt_zero() -> None:
    """THE CROSS-STREAM READ (#185.2 round 5, the probe's variant J).

    Graph runs one stream per FOLDER and a message can be offered by more than
    one of them, so a verdict readable only under the stream that wrote it let
    folder-B spend a complete fresh ``MAX_FILING_ATTEMPTS`` on an id folder-A
    had already given up on -- the issue's own "an exhausted id restarts at
    attempt 0", with no eviction and no corruption involved at all.

    Abandonment is a fact about a MESSAGE, so the answer is provider-wide. The
    mailbox-wide key is asserted too because it is Gmail's only shape and a
    stream a Graph message can also be offered from.
    """
    state = WatchState()

    state.mark_exhausted("folder-A", POISON)

    assert state.message_exhausted("folder-A", POISON) is True
    assert state.message_exhausted("folder-B", POISON) is True
    assert state.message_exhausted(None, POISON) is True
    # The negative control: provider-wide is not "everything is exhausted."
    assert state.message_exhausted("folder-B", "never-abandoned") is False


def test_a_saved_and_reloaded_state_still_knows_what_was_abandoned(tmp_path: Path) -> None:
    """The record is PERSISTED, for the reason ``attempts`` is.

    The probe's variant G: a save and load of a PERFECTLY VALID file after
    eviction reported ``seen=False`` and ``attempts_for=0`` with no warning at
    all, because no fresh start ran. A restart between rounds must not be a
    quieter door to the same re-arm.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_exhausted(INBOX, POISON)
    state.record_cursor(INBOX, "delta-token-abandoned")
    _ordinary_traffic(state, key=INBOX, count=800, tag="ok")
    target = tmp_path / "watchstate-google.json"
    state.save("google", path=target)

    reloaded = WatchState.load("google", path=target)

    assert reloaded.seen_message(INBOX, POISON) is False
    assert reloaded.message_exhausted(INBOX, POISON) is True


def test_recording_the_same_abandonment_twice_costs_one_slot() -> None:
    """Idempotent, and it does not move to the newest position on a repeat.

    Re-ordering on a repeat hit would let one id hold the record open and push
    out abandonments that are older but no less true -- the same inversion
    ``mark_seen`` refuses for the replay window.
    """
    state = WatchState()

    state.mark_exhausted(INBOX, POISON)
    state.mark_exhausted(INBOX, "other")
    state.mark_exhausted(INBOX, POISON)

    assert state.exhausted[INBOX] == [POISON, "other"]


def test_passing_the_threshold_keeps_every_verdict_and_says_so_once_per_stream(
    captured_warnings: list[str],
) -> None:
    """Past ``EXHAUSTED_LIMIT`` NOTHING is evicted, and ONE warning says so (#185.2 r5).

    The round-5 correction. This threshold used to CAP the record and evict
    oldest-first, calling it "the same trade ``SEEN_LIMIT`` documents" -- and it
    was not that trade. ``SEEN_LIMIT`` forgets ids whose work is finished, so
    being wrong costs one message processed twice. An entry here names a message
    still sitting in the inbox and still being offered, so forgetting one hands
    it a complete fresh ``MAX_FILING_ATTEMPTS``, a second ``retry_exhausted``
    row, and four more pinned rounds -- and on a stream where filing stays
    broken the record refilled and re-evicted, so it RECURRED. That is the
    #185.2 defect one bound further out, not a bounded trade.

    The oldest verdict is therefore asserted to SURVIVE, which is the whole
    correction; the warning is what keeps the growth from being silent.
    """
    state = WatchState()

    for index in range(EXHAUSTED_LIMIT + 5):
        state.mark_exhausted(INBOX, f"poison-{index}")

    assert len(state.exhausted[INBOX]) == EXHAUSTED_LIMIT + 5
    assert state.message_exhausted(INBOX, "poison-0") is True
    assert state.message_exhausted(INBOX, f"poison-{EXHAUSTED_LIMIT + 4}") is True
    capped = [line for line in captured_warnings if "abandoned-message record" in line]
    assert len(capped) == 1, f"expected exactly one threshold warning, got {capped}"
    assert INBOX in capped[0]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"exhausted": {INBOX: [7]}}, id="a stringifiable non-string id"),
        pytest.param({"exhausted": {INBOX: [""]}}, id="an empty id"),
        pytest.param({"exhausted": {INBOX: POISON}}, id="a string where a list belongs"),
    ],
)
def test_a_structurally_suspect_record_is_refused_rather_than_coerced(
    payload: dict[str, object],
) -> None:
    """The record is strict-typed for the reason every other field here is (#188).

    Non-strict, ``[7]`` would load as ``['7']`` and the abandonment would then be
    filed under an id no provider will ever offer: the record present, the guard
    silently inert, and nothing to read on the way past. Refusing routes the file
    to ``load``'s existing fresh-start door -- one WARNING, fresh state, full
    resync -- which is the recovery this module already treats as first-class.
    """
    with pytest.raises(ValidationError):
        WatchState.model_validate(payload)


def test_a_corrupt_state_reset_is_the_one_thing_that_does_re_arm(tmp_path: Path) -> None:
    """The #185 ruling, stated from the other side: the reset door stays open.

    Point (1)'s ruling is deliberate and is not being narrowed here -- the counts
    live inside the object being discarded and there is no second source. This
    record is discarded with them, which is the same ruling applied to the same
    fact. The distinction the two halves draw is that the reset is bounded,
    announced, and driven by a file already judged untrustworthy, whereas
    eviction was recurring, silent, and driven by an entirely valid one.
    """
    state = WatchState()
    state.mark_exhausted(INBOX, POISON)
    target = tmp_path / "watchstate-google.json"
    state.save("google", path=target)
    target.write_text("{ this is not json", encoding="utf-8")

    reloaded = WatchState.load("google", path=target)

    assert reloaded.message_exhausted(INBOX, POISON) is False


def test_a_state_file_written_before_the_record_announces_the_one_time_re_arm(
    tmp_path: Path,
    captured_info: list[str],
) -> None:
    """THE UPGRADE PATH (#185.2 round 5, the probe's variant K).

    A deployment that upgrades IN PLACE has a valid state file with no
    ``exhausted`` key at all, and the message it already gave up on is sitting
    in the replay window as that window's OLDEST entry. This module used to read
    the absent key as "nothing abandoned here ... needs no migration," which
    reopens the full #185.2 harm for every existing install: ``SEEN_LIMIT``
    ordinary messages evict the window entry and the re-offer draws a complete
    second ``MAX_FILING_ATTEMPTS``.

    NO VERDICT IS INVENTED, and that is the ruling this pins as much as the
    line. The ``seen`` window cannot tell a message that was filed from one that
    was abandoned -- that collapse is precisely what this record exists to end
    -- so a verdict manufactured from it would permanently abandon mail that
    files perfectly well. The honest handling is to say so once: the record is
    being introduced, and a message abandoned before the upgrade may be retried
    for ONE more bounded cycle.

    The fixture is built through the shipped code and then has the key POPPED,
    which is byte-for-byte what the pre-fix build wrote -- rather than
    hand-rolling a payload that could drift from the real serialized shape.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_exhausted(INBOX, POISON)
    state.mark_seen(INBOX, POISON)
    state.record_cursor(INBOX, "delta-token-abandoned")
    payload = state.model_dump(mode="json")
    payload.pop("exhausted")
    target = tmp_path / "watchstate-google.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = WatchState.load("google", path=target)

    # The pre-fix shape loaded: the id is in the window and carries no verdict.
    assert reloaded.seen_message(INBOX, POISON) is True
    assert reloaded.message_exhausted(INBOX, POISON) is False
    introduced = [line for line in captured_info if "record introduced" in line]
    assert len(introduced) == 1, f"expected exactly one upgrade line, got {introduced}"
    assert "one more bounded cycle" in introduced[0]


def test_a_state_file_that_already_has_the_record_says_nothing(
    tmp_path: Path,
    captured_info: list[str],
) -> None:
    """The upgrade line is ONCE, because the file is only upgraded once.

    The negative control for the test above. The first ``save`` after the
    upgrade writes the key, so every later load takes the ordinary path -- a
    line repeated on every round of every restart is noise, and it would say
    "may be retried" about a deployment where the record is already doing its
    job.
    """
    state = WatchState()
    state.mark_exhausted(INBOX, POISON)
    target = tmp_path / "watchstate-google.json"
    state.save("google", path=target)

    reloaded = WatchState.load("google", path=target)

    assert reloaded.message_exhausted(INBOX, POISON) is True
    assert [line for line in captured_info if "record introduced" in line] == []


def test_an_ordinary_stream_is_never_warned_about(captured_warnings: list[str]) -> None:
    """A mailbox that abandons nothing must never see the line.

    The negative control. An operator warned about a healthy mailbox learns to
    ignore the line before the broken mailbox arrives.
    """
    state = WatchState()

    _ordinary_traffic(state, key=INBOX, count=800, tag="ok")
    state.mark_exhausted(INBOX, POISON)

    assert [line for line in captured_warnings if "abandoned-message record" in line] == []
