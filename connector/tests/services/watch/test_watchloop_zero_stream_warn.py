"""A daemon triaging nothing at all must eventually SAY so, not log INFO forever.

The defect this pins (#191): ``watch --once`` exits 1 on a scope that selected
no stream (#175, ``main.py``), but the long-running ``watch`` form -- the one
``#180``'s service units make the normal deployment shape -- logged
``streams_polled=0`` at INFO every round and exited 0 forever. A typo'd
``only_listed`` folder name is then indistinguishable in the log from a healthy
quiet mailbox, which is the original cron-blindness failure wearing a daemon's
clothes.

The fix is a STREAK, not a per-round warning: one WARN after
``ZERO_STREAM_WARN_ROUNDS`` consecutive silent rounds, naming the likely cause
and the fix, then silence until a round breaks the streak and re-arms it. A
warning on every round is noise an operator filters, which is the same as no
warning at all.

TWO REFUTATIONS OF THE FIRST ATTEMPT ARE PINNED HERE, and both were found by
driving shapes the original test file never built:

1. THE ALARM COULD NOT FIRE ON THE KIT'S DEFAULT PROVIDER. Counting
   ``streams_polled == 0`` is unreachable on Gmail: ``_stream_ids`` returns the
   one mailbox-wide stream unconditionally, so scope is applied per MESSAGE
   inside the round and the count is always 1. ``config.py`` makes ``google``
   the default, so the shipped install was the uncovered case, and the only
   provider the shipped test exercised was ``microsoft``. The second signal --
   mail seen, every message out of scope -- is provider-agnostic and covers it.
2. AN ORDINARY LISTING BLIP RESET THE STREAK. The documented fail-open arm of
   ``_list_containers`` returned a one-row ``PassResult``, so a pass that polled
   nothing BECAUSE LISTING FAILED read as evidence the scope was fine. A
   1-in-3 transient error masked the defect permanently -- on exactly the flaky
   mailbox where an operator is least likely to read every INFO line.

TWO REFUTATIONS OF THE SECOND ATTEMPT ARE PINNED HERE TOO, and they are the
same defect class as (2) above, found one latch over:

3. AN ORDINARY QUIET POLL RESET THE GMAIL STREAK.
   ``saw_only_out_of_scope_mail`` returns False on ``seen == 0`` -- the guard
   that stops an idle mailbox being accused -- and the streak reset on False,
   so the alarm demanded out-of-scope mail on THREE CONSECUTIVE polls. A
   60-second watcher is quiet on most polls, so a typo'd Gmail scope ran 120
   rounds filing nothing and warned never. The reset is now ``triaged_something``:
   positive evidence the scope admitted mail, not the absence of evidence.

4. THE ``all_folders`` FIX SENTENCE REPRODUCED THE MISDIRECTION IT REPLACED.
   That mode has no folder list to correct and no wider mode to widen to, so
   "correct the folder names ... or widen the mode" named two edits the operator
   cannot make. It now says the mail is landing where the listing did not
   enumerate, and names a check the operator can actually run.

ONE REFUTATION OF THE THIRD ATTEMPT IS PINNED HERE, and it is the SAME defect
class as (2) and (3), found one term over inside the reset itself:

5. AN ALREADY-SEEN ID RESET THE GMAIL STREAK. ``triaged_something`` counted
   ``skipped_already_seen`` as positive evidence, on the reasoning that the
   replay window can only hold ids the scope admitted earlier. True, and it does
   not follow: an id the scope admitted proves the scope admits THAT FOLDER, not
   the one the operator misspelled. This connector re-offers such an id forever
   by documented design -- a permanently abandoned message (#185.2) or one the
   filing retry bound gave up on (#158) -- so ONE of them pinned the reset True
   on every round and the streak never reached the threshold. Measured: 500
   rounds, zero invoices filed, zero warnings. The term is gone; the reset is
   ``classified or applied`` alone.

A REFUTATION OF THE FOURTH ATTEMPT IS PINNED HERE, and it is the SAME replayed
id, found in the OTHER arm of the same counter:

6. FIXING THE RESET ALARMED NOTHING, BECAUSE THE INCREMENT WAS UNREACHABLE.
   ``saw_only_out_of_scope_mail`` demanded ``skipped_out_of_scope == seen``, and
   a real mailbox delivers the replayed id and the refused invoice on the SAME
   poll -- ``seen=2``, ``skipped_out_of_scope=1``, ``skipped_already_seen=1``.
   That satisfies neither arm, so the streak held at 0 for 500 rounds after (5)
   landed, and the entry WARN was doing all the work while the prose claimed the
   streak was now reachable. The tests missed it by shape and by assertion:
   ``test_the_replay_skip_no_longer_resets_the_scope_streak`` drives ALTERNATING
   rounds, which exercises each arm in isolation and never builds the mixed one,
   and the end-to-end test asserted only that SOMETHING alarmed -- which the
   entry WARN satisfied. The increment now reads the round's answer: mail was
   refused at the scope gate, nothing was classified or applied, and every other
   message seen was one the watcher had already dealt with. A MIXED round now
   has its own test, and it asserts the STREAK line specifically.

THE SHAPE THAT FOUND (5) IS A PARTLY-CORRECT SCOPE, and it is the shape none of
the tests above build: every one of them uses a scope that is entirely right or
entirely wrong. An operator who misspells EVERY folder notices on day one
because nothing is ever filed; one who misspells ``Invoices`` while ``Inbox``
works sees mail flowing and never looks, and the counts stay half-healthy so
each individual alarm reads the round as fine.

WHAT NO STREAK HERE CAN SEE lives in ``test_watchloop_scope_entries.py``: on
Microsoft Graph the misspelled folder yields no stream, so its mail is never
fetched and never counted by anything a round observes.
``loop._warn_unmatched_scope_entries`` compares the folders the operator NAMED
against the folders the provider ENUMERATED, which needs no mail and no streak.

``--once`` semantics are deliberately untouched: the #175 contract at
``main.py`` owns the one-shot exit status, and one pass can never reach a
streak of three. ``loop._ONCE_EXIT_PROPOSAL`` records the Gmail hole that
contract still has, as a proposal, unimplemented. The unmatched-entry WARN does
fire on ``--once`` -- it changes no exit status, and a config fact is complete
in one pass where a streak is not.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from loguru import logger

from _watch_harness import FakeProvider, _context, _message
from inbox_zero_connector.providers.base import ChangeSet, Container
from inbox_zero_connector.services.watch import loop as watchloop
from inbox_zero_connector.services.watch.scope import MailboxScope

TYPO_FOLDER = "Inobx"
"""The misspelled folder name the scope names and the mailbox does not have."""


STREAK_MARKER = "consecutive rounds"
"""What every STREAK line says and the unmatched-entry line does not.

The two silence streaks both count to ``ZERO_STREAM_WARN_ROUNDS`` and both say
so in their text. ``_warn_unmatched_scope_entries`` is not a streak -- it
reports a config fact from one listing -- so it carries no such phrase, and this
is the seam that lets ``warnings`` mean "streak lines" for the tests that were
written before that signal existed.
"""


@pytest.fixture(autouse=True)
def _rearmed_entry_latch() -> Iterator[None]:
    """Clear the once-per-process unmatched-entry latch around every test.

    ``loop._unmatched_entries_warned`` is deliberately process-scoped so a
    60-second daemon says a true thing once instead of 1,440 times a day. Inside
    one pytest process that makes the SECOND test to use a given typo'd folder
    silent, so whether a test observes the line would depend on test ORDER --
    the classic shape of a suite that passes until somebody adds a file. Clearing
    it per test makes each one see a fresh process's behaviour.
    """
    watchloop._unmatched_entries_warned.clear()
    yield
    watchloop._unmatched_entries_warned.clear()


@pytest.fixture
def all_warnings() -> Iterator[list[str]]:
    """Every WARNING-or-above line loguru emitted, as plain strings.

    A loguru SINK rather than pytest's ``caplog``: this package logs through
    loguru, which does not propagate into the stdlib ``logging`` tree, so
    ``caplog`` assertions here would run against an always-empty list.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING", format="{message}")
    yield lines
    logger.remove(sink_id)


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    """Only the STREAK warnings, which is what the tests below are about.

    NARROWED WHEN ``_warn_unmatched_scope_entries`` LANDED. Most tests here
    drive a typo'd ``only_listed`` scope, which now also produces one
    unmatched-entry line per process -- a correct, deliberate second signal that
    would otherwise turn every ``len(warnings) == 1`` in this file into a count
    of two unrelated signals sharing one assertion. Each of those assertions is
    about ONE streak reaching or not reaching its threshold, so the fixture keeps
    that meaning; ``all_warnings`` is there for the tests that need the whole log.

    The filter is applied in the SINK rather than over a captured list, so a
    test reading the list after ``run`` returns sees exactly the streak lines
    with no refresh step to forget.
    """
    lines: list[str] = []

    def _streaks_only(message: str) -> None:
        if STREAK_MARKER in message:
            lines.append(message)

    sink_id = logger.add(_streaks_only, level="WARNING", format="{message}")
    yield lines
    logger.remove(sink_id)


class _NamedFolderProvider(FakeProvider):
    """A Graph-shaped mailbox exposing exactly one container, ``Inbox``."""

    name = "microsoft"

    def __init__(self) -> None:
        super().__init__(
            ChangeSet(),
            containers=(
                Container(
                    id="folder-inbox",
                    name="Inbox",
                    well_known="inbox",
                    parent_id=None,
                    exclusive=False,
                ),
            ),
        )


class _StoppingSleep:
    """Count injected waits and request a clean stop after a fixed number."""

    def __init__(self, control: watchloop.LoopControl, *, stop_after: int) -> None:
        self.control = control
        self.stop_after = stop_after
        self.waits = 0

    def __call__(self, _delay_seconds: float) -> None:
        self.waits += 1
        if self.waits >= self.stop_after:
            self.control.request_stop()


def _typod_scope() -> MailboxScope:
    """An ``only_listed`` scope naming a folder this mailbox does not have."""
    return MailboxScope(mode="only_listed", included_folders=(TYPO_FOLDER,))


def _matching_container() -> Container:
    """A container whose name is the typo, so the scope suddenly selects it."""
    return Container(
        id="folder-typo",
        name=TYPO_FOLDER,
        well_known=None,
        parent_id=None,
        exclusive=False,
    )


def test_a_zero_stream_streak_warns_once_with_the_cause_and_the_fix(
    warnings: list[str],
) -> None:
    """Three consecutive empty rounds produce exactly one WARN that is actionable."""
    control = watchloop.LoopControl(install_handler=None)
    provider = _NamedFolderProvider()
    context = _context(provider, scope=_typod_scope())
    sleep = _StoppingSleep(control, stop_after=6)

    watchloop.run(context, sleep=sleep, control=control)

    assert len(warnings) == 1, warnings
    warned = warnings[0]
    assert f"rounds={watchloop.ZERO_STREAM_WARN_ROUNDS}" in warned
    assert "only_listed" in warned
    assert "scope" in warned


def test_no_warning_before_the_streak_is_reached(warnings: list[str]) -> None:
    """Two empty rounds are not yet a streak, so nothing is warned."""
    control = watchloop.LoopControl(install_handler=None)
    provider = _NamedFolderProvider()
    context = _context(provider, scope=_typod_scope())
    sleep = _StoppingSleep(control, stop_after=watchloop.ZERO_STREAM_WARN_ROUNDS - 1)

    watchloop.run(context, sleep=sleep, control=control)

    assert warnings == []


def test_a_polled_round_rearms_the_warning(warnings: list[str]) -> None:
    """A round that polls a stream clears the streak, so the next one warns again."""
    control = watchloop.LoopControl(install_handler=None)
    provider = _NamedFolderProvider()
    context = _context(provider, scope=_typod_scope())
    empty_rounds = watchloop.ZERO_STREAM_WARN_ROUNDS
    original = provider.containers

    class _RepairingSleep(_StoppingSleep):
        """Rename the folder into the scope once the first streak has warned.

        The mailbox is what changes, not the scope: ``MailboxScope`` is frozen,
        and a folder appearing (or being renamed back into the configured list)
        is the real-world shape of the streak ending anyway.
        """

        def __call__(self, delay_seconds: float) -> None:
            if self.waits == empty_rounds - 1:
                provider.containers = (_matching_container(),)
            elif self.waits == empty_rounds:
                provider.containers = original
            super().__call__(delay_seconds)

    sleep = _RepairingSleep(control, stop_after=empty_rounds * 2 + 1)
    watchloop.run(context, sleep=sleep, control=control)

    assert len(warnings) == 2, warnings


def test_once_mode_never_warns(warnings: list[str]) -> None:
    """``--once`` keeps the #175 contract: one pass cannot reach a streak."""
    control = watchloop.LoopControl(install_handler=None)
    provider = _NamedFolderProvider()
    context = _context(provider, scope=_typod_scope())

    result = watchloop.run(context, once=True, control=control)

    assert result is not None
    assert result.streams_polled == 0
    assert warnings == []


class _GmailProvider(FakeProvider):
    """The kit's DEFAULT provider shape: one mailbox-wide stream, live mail.

    ``name = "google"`` is the whole point. ``_stream_ids`` short-circuits on it
    and returns ``(None,)`` whatever the scope says, so this mailbox always
    reports ``streams_polled=1`` -- and every message it returns is discarded
    per-message at the scope gate inside the round.
    """

    name = "google"

    def __init__(self) -> None:
        super().__init__(
            ChangeSet(messages=[_message("m-1")], cursor="history-2"),
            containers=(
                Container(
                    id="Label_INBOX",
                    name="Inbox",
                    well_known="inbox",
                    parent_id=None,
                    exclusive=False,
                ),
            ),
        )


class _FlakyFolderProvider(_NamedFolderProvider):
    """A Graph mailbox whose container listing blips on every third call.

    An ordinary transient: a WAN hiccup, a 429, a token refresh landing mid-poll.
    Nothing here is a scope fault, which is exactly why it must not be read as
    evidence that the scope selects something.
    """

    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0

    def list_containers(self) -> list[Container]:
        self.list_calls += 1
        if self.list_calls % 3 == 0:
            raise RuntimeError("transient listing fault")
        return list(self.containers)


def test_gmail_scope_matching_nothing_it_saw_warns(warnings: list[str]) -> None:
    """The default provider gets the alarm too, keyed on the round's own counts.

    RED before the fix: zero warnings after any number of rounds, because
    ``streams_polled`` is pinned at 1 on Gmail and the only signal read it.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _GmailProvider()
    context = _context(provider, scope=_typod_scope())
    sleep = _StoppingSleep(control, stop_after=6)

    result = watchloop.run(context, sleep=sleep, control=control)

    assert result is not None
    assert result.streams_polled == 1, "Gmail always reports one stream"
    assert provider.filed == [], "the typo'd scope must file nothing"
    assert len(warnings) == 1, warnings
    warned = warnings[0]
    assert "scope matched nothing it saw" in warned
    assert f"rounds={watchloop.ZERO_STREAM_WARN_ROUNDS}" in warned
    assert "only_listed" in warned


def test_a_quiet_gmail_mailbox_is_never_accused(warnings: list[str]) -> None:
    """``seen == 0`` is health, not misconfiguration, and must stay silent.

    The negative control for the signal above: without the ``seen > 0`` guard
    an idle inbox on a perfectly good scope would warn every three rounds.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _GmailProvider()
    provider.changes = ChangeSet(cursor="history-2")
    context = _context(provider, scope=MailboxScope(mode="inbox_only"))
    sleep = _StoppingSleep(control, stop_after=6)

    watchloop.run(context, sleep=sleep, control=control)

    assert warnings == []


def test_a_listing_failure_does_not_reset_the_zero_stream_streak(
    warnings: list[str],
) -> None:
    """A pass that polled nothing because LISTING failed is not scope evidence.

    RED before the fix: 30 rounds of a typo'd scope produced ten
    ``pass failed listing containers`` errors and ZERO scope warnings, because
    the fail-open row counted as a polled stream and pinned the streak at 2.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _FlakyFolderProvider()
    context = _context(provider, scope=_typod_scope())
    sleep = _StoppingSleep(control, stop_after=9)

    watchloop.run(context, sleep=sleep, control=control)

    scope_warnings = [line for line in warnings if "no stream polled" in line]
    assert len(scope_warnings) == 1, warnings
    assert provider.list_calls >= watchloop.ZERO_STREAM_WARN_ROUNDS


def test_a_permanently_failing_listing_reaches_the_streak_exactly_once(
    all_warnings: list[str],
) -> None:
    """Never listing at all IS a watcher watching nothing, and it says so once.

    The boundary this pins is deliberate and is where the streak change stops.
    A listing failure does not COUNT as a polled stream, so it cannot be read as
    evidence the scope is fine -- but neither is it silenced: a mailbox whose
    listing is permanently down is triaging nothing just as surely as a typo'd
    scope, and the operator gets the per-round ERROR plus exactly one streak
    line. The alarm is about SILENCE; suppressing it here would restore the
    silence on the one shape that never recovers on its own.
    """
    control = watchloop.LoopControl(install_handler=None)

    class _AlwaysFailingProvider(_NamedFolderProvider):
        """A Graph mailbox whose container listing never succeeds."""

        def list_containers(self) -> list[Container]:
            raise RuntimeError("listing is down")

    context = _context(_AlwaysFailingProvider(), scope=MailboxScope(mode="all_folders"))
    sleep = _StoppingSleep(control, stop_after=8)

    watchloop.run(context, sleep=sleep, control=control)

    streak_lines = [line for line in all_warnings if "no stream polled" in line]
    listing_errors = [line for line in all_warnings if "pass failed listing containers" in line]
    assert len(streak_lines) == 1, all_warnings
    assert len(listing_errors) >= watchloop.ZERO_STREAM_WARN_ROUNDS, all_warnings
    assert "scope mode=all_folders" in streak_lines[0]


def test_the_warning_names_the_mode_actually_configured(warnings: list[str]) -> None:
    """An ``inbox_only`` silence must not blame an ``only_listed`` list.

    The old prose named a list in every mode, sending an operator to edit a
    config section that is empty in three of the four.
    """
    control = watchloop.LoopControl(install_handler=None)

    class _NoInboxProvider(FakeProvider):
        """A Graph mailbox with folders but NO well-known Inbox to select."""

        name = "microsoft"

        def __init__(self) -> None:
            super().__init__(
                ChangeSet(),
                containers=(
                    Container(
                        id="folder-archive",
                        name="Archive",
                        well_known=None,
                        parent_id=None,
                        exclusive=False,
                    ),
                ),
            )

    context = _context(_NoInboxProvider(), scope=MailboxScope(mode="inbox_only"))
    sleep = _StoppingSleep(control, stop_after=6)

    watchloop.run(context, sleep=sleep, control=control)

    assert len(warnings) == 1, warnings
    assert "scope mode=inbox_only" in warnings[0]
    assert "only_listed" not in warnings[0]


def test_both_signals_share_one_line_when_a_round_is_silent_both_ways(
    warnings: list[str],
) -> None:
    """One silence produces one WARN, never two, even when both streaks are live.

    A Graph mailbox whose scope selects no folder polls no stream AND sees no
    mail, so only the stream streak can reach its threshold; the assertion that
    matters is the count, which is what the shared latch buys.
    """
    control = watchloop.LoopControl(install_handler=None)
    context = _context(_NamedFolderProvider(), scope=_typod_scope())
    sleep = _StoppingSleep(control, stop_after=12)

    watchloop.run(context, sleep=sleep, control=control)

    assert len(warnings) == 1, warnings


def test_once_mode_on_gmail_still_exits_the_way_it_did(warnings: list[str]) -> None:
    """``--once`` semantics are UNCHANGED by this fix, deliberately (#175).

    Gmail reports one stream even when the scope matches nothing, so the
    one-shot path still returns a pass ``_once_exit_code`` reads as clean. That
    hole is real and is recorded as a PROPOSAL in ``loop._ONCE_EXIT_PROPOSAL``;
    this test pins that nothing in this change altered the behaviour, so the
    ruling stays a ruling rather than an accident.
    """
    control = watchloop.LoopControl(install_handler=None)
    context = _context(_GmailProvider(), scope=_typod_scope())

    result = watchloop.run(context, once=True, control=control)

    assert result is not None
    assert result.streams_polled == 1
    assert result.errors == 0
    assert result.saw_only_out_of_scope_mail is True
    assert warnings == []
    assert "PROPOSAL, NOT IMPLEMENTED" in watchloop._ONCE_EXIT_PROPOSAL


class _CadencedGmailProvider(_GmailProvider):
    """Gmail delivering one out-of-scope message every ``every``-th poll.

    THE ORDINARY SHAPE OF A REAL MAILBOX, which is the point: at the default
    60-second interval a mailbox taking a dozen messages an hour is quiet on
    four polls in five. Every other shipped test here returns the SAME
    ``ChangeSet`` on every poll, so mail is present on all six rounds and the
    streak never has to survive a gap -- which is why the suite was green over
    the defect this class reproduces.
    """

    def __init__(self, *, every: int) -> None:
        super().__init__()
        self.every = every
        self.calls = 0

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        self.polled.append(cursor)
        self.polled_containers.append(container_id)
        self.calls += 1
        if self.calls % self.every == 0:
            return ChangeSet(messages=[_message(f"m-{self.calls}")], cursor=f"history-{self.calls}")
        return ChangeSet(messages=[], cursor=f"history-{self.calls}")


@pytest.mark.parametrize("every", [2, 3, 5])
def test_a_quiet_poll_between_out_of_scope_rounds_does_not_disarm_the_alarm(
    warnings: list[str], every: int
) -> None:
    """Mail on every Nth poll of a typo'd Gmail scope still reaches the streak.

    RED before the fix: 60 rounds filing nothing and ``warnings == []`` at every
    cadence, because ``record`` reset the streak whenever
    ``saw_only_out_of_scope_mail`` was False -- and it is False on a quiet round
    by the very guard that keeps an idle mailbox from being accused.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _CadencedGmailProvider(every=every)
    context = _context(provider, scope=_typod_scope())
    sleep = _StoppingSleep(control, stop_after=60)

    watchloop.run(context, sleep=sleep, control=control)

    assert provider.filed == [], "the typo'd scope must file nothing"
    scope_warnings = [line for line in warnings if "scope matched nothing it saw" in line]
    assert len(scope_warnings) == 1, (
        f"60 rounds, mail on every {every}th poll, nothing filed: {warnings}"
    )


def test_a_permanently_quiet_mailbox_is_still_never_accused(warnings: list[str]) -> None:
    """The negative control the new reset must not break: silence is not guilt.

    Holding the streak across quiet rounds is only safe because a quiet round
    cannot ADVANCE it either. A mailbox that never delivers anything sees
    ``seen == 0`` forever, so the streak stays at zero and no line is ever
    emitted -- health, not misconfiguration, exactly as before.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _GmailProvider()
    provider.changes = ChangeSet(cursor="history-2")
    context = _context(provider, scope=_typod_scope())
    sleep = _StoppingSleep(control, stop_after=60)

    watchloop.run(context, sleep=sleep, control=control)

    assert warnings == []


def test_a_round_that_files_something_resets_the_scope_streak(warnings: list[str]) -> None:
    """Positive evidence -- a message classified and applied -- clears the latch.

    The counterpart to the two tests above. The streak must still be breakable,
    or the alarm becomes a one-way ratchet that fires on any install whose scope
    was briefly wrong. What breaks it is the round DOING something, which is the
    only observation that proves the scope admits mail.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _GmailProvider()
    context = _context(provider, scope=MailboxScope(mode="all_folders"))
    sleep = _StoppingSleep(control, stop_after=8)

    watchloop.run(context, sleep=sleep, control=control)

    assert provider.filed, "an in-scope round must actually file"
    assert warnings == []


def test_the_scope_warning_is_one_line_per_streak_and_re_arms_on_a_filed_round(
    warnings: list[str],
) -> None:
    """The latch bounds the line to one per streak, and a filed round re-arms it.

    HOLDING THE COUNTER ACROSS A QUIET ROUND BROKE THE OLD BOUND, which was the
    equality ``out_of_scope == ZERO_STREAM_WARN_ROUNDS``: that is once-per-streak
    only while the counter moves every round, so a streak parked at the threshold
    re-satisfied it on every quiet round after it. This drives 12 out-of-scope
    rounds with quiet rounds interleaved, then renames the folder INTO the scope
    so a round files, then renames it back -- exactly two lines, one per streak.

    The MAILBOX is what changes, not the scope, for the same two reasons the
    stream streak's re-arm test gives: ``RoundContext`` validates on assignment,
    and a folder being renamed into the configured list is the real-world shape
    of a streak ending anyway.
    """
    control = watchloop.LoopControl(install_handler=None)
    provider = _CadencedGmailProvider(every=2)
    context = _context(provider, scope=_typod_scope())
    typod = (
        Container(
            id="Label_INBOX",
            name=TYPO_FOLDER,
            well_known=None,
            parent_id=None,
            exclusive=False,
        ),
    )
    original = provider.containers

    class _RepairingSleep(_StoppingSleep):
        """Rename the inbox into the scope once the first streak has warned."""

        def __call__(self, delay_seconds: float) -> None:
            if self.waits == 12:
                provider.containers = typod
            elif self.waits == 16:
                provider.containers = original
            super().__call__(delay_seconds)

    watchloop.run(context, sleep=_RepairingSleep(control, stop_after=30), control=control)

    assert provider.filed, "the repaired scope must actually file something"
    scope_warnings = [line for line in warnings if "scope matched nothing it saw" in line]
    assert len(scope_warnings) == 2, warnings


def test_the_all_folders_warning_names_a_check_instead_of_an_impossible_edit(
    warnings: list[str],
) -> None:
    """``all_folders`` has no list to correct, so the line must not say to correct one.

    RED before the fix: the shared fix sentence told the operator of a scope that
    already covers everything to "correct the folder names ... or widen the
    mode" -- the exact misdirection ``_scope_fix_sentence`` was added to end,
    reproduced on the one mode where neither edit exists.

    RETARGETED FOR #198, AND THE RETARGET IS THE POINT. This test used to drive
    an UNENUMERATED container and read the resulting scope WARN, which is the
    conflation the split ends: a container the listing did not carry is now its
    own outcome, its own count, and its own WARN naming the listing rather than
    the scope. The ``all_folders`` scope sentence still exists and is still
    wrong to phrase as an edit, so it is asserted directly here; the
    unenumerated path's own signal is pinned in
    ``tests/services/watch/test_unenumerated_container.py``.
    """
    del warnings
    context = _context(_GmailProvider(), scope=MailboxScope(mode="all_folders"))

    sentence = watchloop._scope_fix_sentence(context)

    assert "scope mode=all_folders" in sentence
    assert "correct the folder names" not in sentence
    assert "widen the mode" not in sentence
    assert "include_archive" in sentence
