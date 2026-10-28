"""A PARTLY-CORRECT scope is the shape a typo actually takes, and it alarms.

Split out of ``test_watchloop_zero_stream_warn.py`` when that file crossed this
repo's 500-line code ceiling; no test here changed, was renamed, or was dropped.
That module's docstring holds the full history of the zero-stream alarm and its
refutations (1) through (6) -- read it first, because the tests here are the
ones that pin refutations (5) and (6), and the sentences below refer to them by
number.

WHAT LIVES HERE. Every test left in the original file drives a scope that is
entirely right or entirely wrong. An operator who misspells EVERY folder notices
on day one, because nothing is ever filed; one who misspells ``Invoices`` while
``Inbox`` works sees mail flowing and never looks, and the counts stay
half-healthy so each individual alarm reads the round as fine. That shape --
``PARTLY_TYPOD_FOLDERS`` beside ``_ReplayPlusMisfiledInvoiceGmail``, which
delivers an already-dealt-with id and a refused invoice on the SAME poll -- is
what this module builds:

- Variants R, S, and U: a typo'd folder beside a correct one is alarmed, and
  over 500 rounds it is alarmed ONCE PER SIGNAL rather than 1,440 times a day.
- The MIXED round, end to end and directly at ``PassResult``. It is the round a
  real mailbox produces and the one the alternating-round shape could never
  build, which is exactly how the unreachable increment stayed hidden.
- The bounds on the widened increment: an unexplained message and a pure replay
  round must each stay silent, and a filed round must still clear the streak.

SHARED FIXTURES AND HELPERS ARE IMPORTED FROM THE ORIGINAL MODULE rather than
copied, or lifted into a ``conftest.py``. ``warnings``, ``all_warnings``, and
the autouse ``_rearmed_entry_latch`` are pytest fixtures, so importing their
names into this module's namespace is what makes them resolvable in the tests
below -- and keeping each defined beside the docstring that explains why it
exists (a loguru sink rather than ``caplog``; a streak-only filter; a per-test
clear of a deliberately process-scoped latch) is worth more than the symmetry a
conftest would buy. A conftest would also make those fixtures visible to every
other module in this directory, which is a wider blast radius than a file split
should have.
"""

from __future__ import annotations

import pytest
import test_watchloop_zero_stream_warn as _origin
from pydantic import ValidationError
from test_watchloop_zero_stream_warn import STREAK_MARKER, _GmailProvider, _StoppingSleep

from _watch_harness import _context, _message
from inbox_zero_connector.providers.base import ChangeSet, Container
from inbox_zero_connector.services.watch import loop as watchloop
from inbox_zero_connector.services.watch.round import RoundState, RoundSummary
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState

# The fixtures stay defined in the original module; binding them as module
# attributes here is how pytest discovers them for this file.
warnings = _origin.warnings
all_warnings = _origin.all_warnings
_rearmed_entry_latch = _origin._rearmed_entry_latch

REPLAY_ID = "m-already-seen"
"""One id the connector has already dealt with and the mailbox keeps offering."""

INVOICES_LABEL = "Label_Invoices"
"""The Gmail label id of the folder the operator meant and misspelled."""

PARTLY_TYPOD_FOLDERS = ("Inbox", "Invoces")
"""``only_listed`` naming one folder correctly and one with a typo.

THE SHAPE A TYPO ACTUALLY TAKES. An operator who misspells EVERY folder notices
on day one, because nothing is ever filed; one who misspells ``Invoices`` while
``Inbox`` works sees mail flowing and never looks. That is also the shape the
whole-scope tests above cannot produce: their scope is entirely wrong, so every
count in the round agrees something is broken.
"""


def _summary(**counts: int) -> RoundSummary:
    """A clean round's counts, overridden by whichever ones the caller names."""
    return RoundSummary(
        container_id=None,
        state=RoundState.COMMITTED,
        seen=counts.get("seen", 0),
        skipped_out_of_scope=counts.get("skipped_out_of_scope", 0),
        skipped_unenumerated_container=counts.get("skipped_unenumerated_container", 0),
        skipped_already_seen=counts.get("skipped_already_seen", 0),
        classified=counts.get("classified", 0),
        applied=counts.get("applied", 0),
        failed=counts.get("failed", 0),
        cursor_stored=True,
        resynced=False,
    )


def _partly_typod_scope() -> MailboxScope:
    """The refutation's scope: one working folder beside one misspelled one."""
    return MailboxScope(
        mode="only_listed",
        included_folders=PARTLY_TYPOD_FOLDERS,
        names_encode_nesting=True,
    )


class _ReplayPlusMisfiledInvoiceGmail(_GmailProvider):
    """The third refutation's mailbox, and every part of it is ordinary.

    Two messages per poll. The first is an id this connector ALREADY DEALT WITH,
    sitting in the in-scope Inbox: either permanently abandoned (#185.2) or
    given up on after the filing retry bound (#158). Both are re-offered on
    every poll by documented design, and both are counted
    ``skipped_already_seen`` rather than out of scope, because the Inbox IS in
    scope -- the operator spelled that one right.

    The second is a NEW invoice each round, landing in the real ``Invoices``
    label that the config misspells as ``Invoces``, so the scope refuses it.

    Before the fix, the first message pinned ``triaged_something`` True on every
    round, which reset the scope streak before it could ever reach the
    threshold, and the invoices piled up for 500 rounds in silence.
    """

    def __init__(self) -> None:
        super().__init__()
        self.containers = (
            *self.containers,
            Container(
                id=INVOICES_LABEL,
                name="Invoices",
                well_known=None,
                parent_id=None,
                exclusive=False,
            ),
        )
        self.polls = 0

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        self.polled.append(cursor)
        self.polled_containers.append(container_id)
        self.polls += 1
        return ChangeSet(
            messages=[
                _message(REPLAY_ID, containers=("Label_INBOX",)),
                _message(f"m-invoice-{self.polls}", containers=(INVOICES_LABEL,)),
            ],
            cursor=f"history-{self.polls}",
        )


@pytest.mark.parametrize("mark", ["seen", "exhausted"])
def test_a_typod_folder_beside_a_correct_one_is_alarmed(all_warnings: list[str], mark: str) -> None:
    """Variants R and S: a replay skip is not evidence the scope selects mail.

    RED before the fix: 50 rounds, zero invoices filed, ``warnings == []``. One
    id the connector had already dealt with -- abandoned under #185.2
    (``exhausted``) or merely inside the replay window (``seen``) -- made
    ``triaged_something`` True on every round, because it counted
    ``skipped_already_seen`` as positive evidence that the scope selects mail.
    #185.2 documents that state as "permanent until the watch state is reset",
    so one such message disarmed the alarm for the life of the install.

    ``all_warnings`` rather than ``warnings``, because this shape must produce
    BOTH signals and the assertion names each one. It used to assert only that
    SOMETHING alarmed, on the reasoning that either signal getting there first
    was acceptable -- and that is exactly how the streak's unreachable increment
    (refutation 6) stayed hidden: the entry WARN satisfied the assertion on
    every run while the streak sat at zero. An either-or assertion cannot
    distinguish two working signals from one working and one dead.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _ReplayPlusMisfiledInvoiceGmail()
    state = WatchState()
    if mark == "seen":
        state.mark_seen(None, REPLAY_ID)
    else:
        state.mark_exhausted(None, REPLAY_ID)
    context = _context(provider, scope=_partly_typod_scope(), state=state)
    sleep = _StoppingSleep(control, stop_after=50)

    watchloop.run(context, sleep=sleep, control=control)

    assert provider.filed == [], "the misspelled folder's mail must not be filed"
    entry_lines = [line for line in all_warnings if "has no folder matching it" in line]
    streak_lines = [line for line in all_warnings if STREAK_MARKER in line]
    assert len(entry_lines) == 1, (
        f"50 rounds holding one already-seen id ({mark}), one new invoice per "
        f"round refused out of scope, and nothing filed: {all_warnings}"
    )
    assert len(streak_lines) == 1, (
        f"the streak must reach its threshold on the MIXED round this mailbox "
        f"produces, not only on an out-of-scope-only one: {all_warnings}"
    )
    assert "Invoces" in entry_lines[0]


def test_the_replay_skip_no_longer_resets_the_scope_streak(warnings: list[str]) -> None:
    """The reset half of the fix, isolated from the unmatched-entry signal.

    The test above proves the operator is told. This proves WHICH change did it
    on the streak side: ``skipped_already_seen`` is no longer positive evidence,
    so a round that only replays an already-seen id HOLDS the streak instead of
    clearing it, and three out-of-scope rounds interleaved with replay rounds
    still reach the threshold.

    ALTERNATING ROUNDS EXERCISE EACH ARM IN ISOLATION, WHICH IS THIS TEST'S
    SCOPE AND WAS ALSO ITS BLIND SPOT. A real mailbox delivers both messages on
    the SAME poll, and that mixed round satisfied neither arm -- so this test
    stayed green over a streak that could not fire on the shape the issue
    describes. The mixed round has its own tests now
    (``test_a_mixed_round_advances_the_streak_at_the_property`` and the
    end-to-end ``test_the_streak_itself_fires_on_a_mixed_replay_and_out_of_scope_round``);
    this one keeps the narrower property it was written for, that a replay round
    HOLDS rather than clears.
    """
    del warnings
    replay_only = watchloop.PassResult(
        streams=(
            watchloop.StreamResult(None, "COMMITTED", _summary(seen=1, skipped_already_seen=1)),
        )
    )
    out_of_scope_only = watchloop.PassResult(
        streams=(
            watchloop.StreamResult(None, "COMMITTED", _summary(seen=1, skipped_out_of_scope=1)),
        )
    )
    assert replay_only.triaged_something is False, (
        "an already-seen id proves the scope admits the folder it is ALREADY in, "
        "and nothing about the folder the operator misspelled"
    )

    streaks = watchloop._SilenceStreaks()
    for index in range(watchloop.ZERO_STREAM_WARN_ROUNDS * 2):
        streaks.record(out_of_scope_only if index % 2 == 0 else replay_only)

    assert streaks.out_of_scope >= watchloop.ZERO_STREAM_WARN_ROUNDS, (
        f"a replay round reset the streak: {streaks.out_of_scope}"
    )


def test_the_partly_typod_alarm_is_one_line_per_signal_over_five_hundred_rounds(
    all_warnings: list[str],
) -> None:
    """Variant U: the alarm is not merely late, and it does not become noise.

    The refutation measured 500 rounds of total silence. This drives the same
    500 and asserts the opposite bound in BOTH directions -- the operator is
    told, and told ONCE PER SIGNAL. A daemon polling every 60 seconds would
    otherwise repeat a true sentence 1,440 times a day, which is noise an
    operator filters, which costs the same as the silence this replaced.

    TWO LINES, NOT ONE, AND THE COUNT CHANGING IS THE POINT. This bound used to
    be ``len(all_warnings) == 1``, which passed because only the entry WARN
    could fire on this shape: the streak's increment demanded
    ``skipped_out_of_scope == seen``, and this mailbox delivers the replayed id
    and the refused invoice on the SAME poll, so it never satisfied the
    equality and sat at zero for all 500 rounds. Asserting the total was
    therefore asserting the streak stayed broken. Each signal is now bounded on
    its own, which is the property that was actually meant.

    The two lines must not contradict each other, so this asserts the seam
    between them: the entry line names the folder and its own cause, and the
    streak line -- which counts rounds and cannot know which entry failed --
    asserts no cause of its own and points at the entry lines instead.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _ReplayPlusMisfiledInvoiceGmail()
    state = WatchState()
    state.mark_seen(None, REPLAY_ID)
    context = _context(provider, scope=_partly_typod_scope(), state=state)
    sleep = _StoppingSleep(control, stop_after=500)

    watchloop.run(context, sleep=sleep, control=control)

    assert provider.filed == []
    entry_lines = [line for line in all_warnings if "has no folder matching it" in line]
    streak_lines = [line for line in all_warnings if STREAK_MARKER in line]
    assert len(entry_lines) == 1, all_warnings
    assert len(streak_lines) == 1, all_warnings
    assert len(all_warnings) == 2, f"500 rounds produced {len(all_warnings)} lines"
    assert "Invoces" in entry_lines[0]
    assert "misspelled" not in streak_lines[0], (
        "the streak counts rounds and cannot know which entry failed or why; "
        f"naming a cause here is a guess in the grammar of a fact: {streak_lines[0]}"
    )
    assert "per-folder scope WARN lines" in streak_lines[0], (
        "the streak must point at the signal that CAN name the entry"
    )


def test_the_streak_itself_fires_on_a_mixed_replay_and_out_of_scope_round(
    warnings: list[str],
) -> None:
    """The STREAK reaches its threshold on the round a real mailbox produces.

    RED before this fix: ``scope_warnings == []`` after 50 rounds. The reset arm
    had been corrected so a replay could no longer CLEAR the streak, and that
    bought nothing, because the INCREMENT arm demanded
    ``skipped_out_of_scope == seen``. This mailbox delivers the replayed id and
    the refused invoice on the SAME poll -- ``seen=2``,
    ``skipped_out_of_scope=1``, ``skipped_already_seen=1`` -- so neither arm
    fired and the counter sat at zero for 500 rounds.

    THE ASSERTION IS THE STREAK LINE, NOT "SOMETHING ALARMED". The ``warnings``
    fixture filters to streak lines only, so the entry WARN -- which fires on
    this config too, and correctly -- cannot satisfy this test. That distinction
    is the whole refutation: the shipped tests asserted only that some line
    appeared, so they stayed green over a streak that could never fire.

    A MIXED ROUND, NOT ALTERNATING ONES. ``_ReplayPlusMisfiledInvoiceGmail``
    returns both messages from every single poll, which is what an abandoned
    message (#185.2) sitting in the mailbox beside arriving invoices actually
    looks like. The alternating shape drives each arm in isolation and is
    exactly the shape that missed this.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _ReplayPlusMisfiledInvoiceGmail()
    state = WatchState()
    state.mark_seen(None, REPLAY_ID)
    context = _context(provider, scope=_partly_typod_scope(), state=state)
    sleep = _StoppingSleep(control, stop_after=50)

    watchloop.run(context, sleep=sleep, control=control)

    assert provider.filed == [], "the misspelled folder's mail must not be filed"
    scope_warnings = [line for line in warnings if "scope matched nothing it saw" in line]
    assert len(scope_warnings) == 1, (
        f"50 mixed rounds -- one replayed id and one refused invoice per poll, "
        f"nothing filed -- produced {len(scope_warnings)} streak lines: {warnings}"
    )


def test_a_mixed_round_advances_the_streak_at_the_property(warnings: list[str]) -> None:
    """The seam under the test above, driven directly at ``PassResult``.

    The end-to-end test proves the operator is warned. This proves WHICH
    property changed, and pins the counts that used to defeat it: a round of
    ``seen=2, skipped_out_of_scope=1, skipped_already_seen=1`` is the exact
    shape the old ``skipped_out_of_scope == seen`` equality read as healthy.
    """
    del warnings
    mixed = watchloop.PassResult(
        streams=(
            watchloop.StreamResult(
                None,
                "COMMITTED",
                _summary(seen=2, skipped_out_of_scope=1, skipped_already_seen=1),
            ),
        )
    )

    assert mixed.saw_only_out_of_scope_mail is True, (
        "a replayed id beside a refused invoice is not evidence the scope works"
    )
    assert mixed.triaged_something is False

    streaks = watchloop._SilenceStreaks()
    for _ in range(watchloop.ZERO_STREAM_WARN_ROUNDS):
        streaks.record(mixed)

    assert streaks.out_of_scope == watchloop.ZERO_STREAM_WARN_ROUNDS


def test_an_unaccounted_round_cannot_be_built_at_all() -> None:
    """The bound on widening the increment moved from a runtime term to the type.

    This test used to construct a ``RoundSummary`` reporting three messages seen
    while accounting for two, and assert the streak stayed silent on it. That
    round is not one any provider produces -- ``poll_once`` tallies every message
    into exactly one bucket -- so the property was defending against a shape only
    a test could build, and the docstring describing that defence was fiction.

    ``RoundSummary`` now enforces the partition (``services/watch/round.py``), so
    the guard is structural: the unexplained round is unconstructible rather than
    tolerated. What is asserted here is that the model refuses it and says which
    counts disagreed, because a future outcome bucket added without a counter is
    exactly the mistake this catches.
    """
    with pytest.raises(ValidationError) as caught:
        _summary(seen=3, skipped_out_of_scope=1, skipped_already_seen=1)

    assert "do not account for every message seen" in str(caught.value)


def test_a_pure_replay_round_still_advances_nothing(warnings: list[str]) -> None:
    """A round the scope refused NOTHING is not out-of-scope evidence either.

    The negative control on the other side of the widened increment. A round
    that only replays an already-seen id saw the scope refuse nothing at all, so
    it must neither advance the streak nor clear it -- it is uninformative, the
    same verdict the quiet poll and the failed listing get. ``> 0`` on
    ``skipped_out_of_scope`` is the guard that holds this, replacing the
    ``seen`` equality that used to hold it by accident.
    """
    del warnings
    replay_only = watchloop.PassResult(
        streams=(
            watchloop.StreamResult(None, "COMMITTED", _summary(seen=1, skipped_already_seen=1)),
        )
    )

    assert replay_only.saw_only_out_of_scope_mail is False

    streaks = watchloop._SilenceStreaks()
    streaks.out_of_scope = 2
    streaks.record(replay_only)

    assert streaks.out_of_scope == 2, "a replay round must hold the streak, not move it"


def test_a_filed_round_still_clears_the_streak_without_the_replay_term(
    warnings: list[str],
) -> None:
    """The negative control: dropping the third term must not make the alarm a ratchet.

    ``classified or applied`` is still positive evidence and still resets, so an
    install whose scope was briefly wrong -- a folder created a minute after the
    watcher started -- goes quiet again on its own. If this went red, the fix
    would have traded a silent alarm for one that never stops.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _GmailProvider()
    state = WatchState()
    state.mark_seen(None, REPLAY_ID)
    context = _context(provider, scope=MailboxScope(mode="all_folders"), state=state)
    sleep = _StoppingSleep(control, stop_after=20)

    watchloop.run(context, sleep=sleep, control=control)

    assert provider.filed, "an in-scope round must actually file"
    assert warnings == []
