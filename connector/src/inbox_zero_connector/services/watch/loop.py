"""Foreground loop boundary for repeated calls to ``watcher.poll_once``.

This module owns the loop that calls ``poll_once``. It runs in the foreground
only: no daemon or scheduler belongs here. It IS the daemon entry seam, so
signal handling lives here and not in the CLI -- SIGINT and SIGTERM are wired to
one stop event (``LoopControl``), the inter-pass wait is that event's own
interruptible wait, and a stop arriving mid-backoff wakes the loop at once so it
returns through its checkpoint instead of being SIGKILLed with unsaved state
(#162). The handler installer is injected so tests drive both without signals.

State is persisted after EVERY STREAM's round rather than once per pass, which
bounds what a kill can lose to the single round in flight. Which containers are
polled remains ``MailboxScope``'s decision; nothing here widens it.

It also owns the daemon's SILENCE ALARM (``_SilenceStreaks``, #191): a watcher
that runs cleanly while triaging nothing is the failure this loop can see and
the CLI cannot, because only the loop watches consecutive rounds. Beside the two
streaks sits one signal that needs no streak at all
(``_warn_unmatched_scope_entries``): an ``only_listed`` entry that yields NO
STREAM is wrong on the first round, and it is the ONLY signal that can see such
a folder on Microsoft Graph, whose mail is never fetched and therefore never
counted by anything a round observes. It is also the only one that can name
WHICH entry is at fault and why -- a misspelling, or a well-known system folder
``include_archive``/``include_junk``/``include_deleted`` still refuses -- so the
streaks defer to it rather than guessing between causes they cannot tell apart.

ONE EXCEPTION FAMILY IS FATAL TO THIS LOOP: ``errors.OperatorRefusalError``
(``ConfigError``, ``AuthError``, ``TaxonomyError``). Every broad boundary here
re-raises it before its survive-and-back-off handling, because an operator
refusal returns the same answer on every pass -- retrying it is a hot loop
wearing a health mask, and it hides the one-line refusal the CLI handler
exists to print (#171, #195).
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from difflib import get_close_matches
from pathlib import Path
from types import FrameType
from typing import Final, TypedDict, Unpack

from loguru import logger

from inbox_zero_connector.config import data_dir
from inbox_zero_connector.errors import OperatorRefusalError
from inbox_zero_connector.providers.base import Container
from inbox_zero_connector.services.audit import intents as intents
from inbox_zero_connector.services.notify import notify
from inbox_zero_connector.services.watch.accounts_pass import AccountRound, run_accounts
from inbox_zero_connector.services.watch.health import Health, record_round
from inbox_zero_connector.services.watch.round import (
    RoundContext,
    RoundState,
    RoundSummary,
    poll_once,
)
from inbox_zero_connector.services.watch.scope import MailboxScope

DEFAULT_INTERVAL_SECONDS: Final = 60.0
MAX_BACKOFF_SECONDS: Final = 15.0 * 60.0
GMAIL_PROVIDER: Final = "google"

NOTIFY_FAILURE_STREAK: Final = 3
"""Consecutive failed rounds before the second, louder notification.

Three for the same reason ``ZERO_STREAM_WARN_ROUNDS`` is three: one failure is
ordinary and already got the healthy->failing notification; three in a row is a
watcher that is not coming back on its own.
"""

REAUTH_WARNING_WINDOW: Final = timedelta(hours=24)
"""How close to credential expiry the advance warning fires (decision D9).

Matched to ``apps/cli/status.REAUTH_WARNING_WINDOW`` so the notification and
the ``status`` readout name the same day.
"""

ZERO_STREAM_WARN_ROUNDS: Final = 3
"""Consecutive rounds that triaged nothing before the daemon says so out loud.

Three rather than one, because a single empty round is ordinary: a provider can
return an empty listing mid-reconfiguration, and a warning per round is noise an
operator filters, which costs the same as no warning at all. Three consecutive
rounds cannot be a blip -- the scope selected nothing and will keep selecting
nothing until a human edits it.

SHARED BY BOTH SILENCE SIGNALS AND BY ONE LATCH, so an install cannot be warned
twice for the same silence: the stream-selection streak (nothing was polled) and
the scope-match streak (everything polled was out of scope). See
``_silence_streaks``.

DAEMON-ONLY. ``watch --once`` already exits 1 on a zero-stream pass under the
#175 contract (``apps/cli/main.py``), and one pass can never reach a streak.
"""

_ONCE_EXIT_PROPOSAL: Final = """PROPOSAL, NOT IMPLEMENTED -- the ``--once`` exit contract's Gmail hole (#191).

``apps/cli/main.py:_once_exit_code`` refuses a pass on ``streams_polled == 0``.
On Gmail that condition is unreachable: ``_stream_ids`` returns the single
mailbox-wide stream unconditionally, so a scope naming a folder that does not
exist yields ``streams_polled=1``, ``seen>0``, ``applied=0``, and EXIT 0. A
scheduler reading that status is told a misconfigured install is healthy.

The exact condition, were it adopted:

    if result.saw_only_out_of_scope_mail:
        return 1

placed AFTER the existing ``streams_polled == 0`` arm and BEFORE the
``errors > 0`` arm, with its own stderr sentence naming scope as the cause.

WHY IT IS NOT WRITTEN HERE: this changes the exit status of a command
schedulers already consume, and this module does not own that contract --
``main.py`` does, under #175. It also has a real false-positive shape worth
ruling on first: a single ``--once`` run against a busy mailbox whose new mail
genuinely all sits outside a DELIBERATELY narrow scope would begin exiting 1.
The daemon signal above tolerates that by requiring a streak; a one-shot cannot.
"""
"""Recorded next to the code it would change, so the ruling has a home."""

type SignalHandler = Callable[[int, FrameType | None], None]
type SignalInstaller = Callable[[signal.Signals, SignalHandler], None]


class RunOptions(TypedDict, total=False):
    """Optional pass-completion callbacks accepted by ``run``."""

    checkpoint: Callable[[], object] | None


def _install_signal_handler(signum: signal.Signals, handler: SignalHandler) -> None:
    """Install one process signal handler through the standard library."""
    signal.signal(signum, handler)


@dataclass
class LoopControl:
    """Stop signal and injectable process-handler installation boundary.

    THE STOP IS AN EVENT, NOT A FLAG, AND THAT IS THE #162 FIX. The loop's
    inter-pass wait used to be ``time.sleep``, which PEP 475 makes
    UNINTERRUPTIBLE: a Python-level signal handler runs and the sleep then
    RESUMES for its remaining time. With the backoff at its 15-minute cap, a
    SIGTERM set the flag and the process kept sleeping -- long past any service
    manager's stop timeout, so the manager escalated to SIGKILL, ``checkpoint``
    never ran, and the pass's cursors and replay marks were lost. The restart
    re-read and RE-BILLED every classification the dead pass had already paid
    for.

    ``threading.Event.wait`` has no such problem: ``set`` from the handler wakes
    the waiter immediately, so the loop returns through its normal exit and runs
    its checkpoint. The boolean is kept as a read-only view over the event so
    every existing caller and test that reads ``stop_requested`` still works.
    """

    install_handler: SignalInstaller | None = _install_signal_handler
    stop_event: threading.Event = field(default_factory=threading.Event)

    @property
    def stop_requested(self) -> bool:
        """Whether a clean stop has been requested. Reads the event, holds no copy."""
        return self.stop_event.is_set()

    def request_stop(self, _signum: int = 0, _frame: FrameType | None = None) -> None:
        """Request a clean return, waking the loop if it is waiting.

        Wired directly to SIGINT and SIGTERM (``install_signal_handlers``), so
        the signal path and the programmatic path are the same code.
        """
        self.stop_event.set()

    def wait(self, timeout_seconds: float) -> bool:
        """Wait up to ``timeout_seconds``, returning early if a stop is requested.

        Args:
            timeout_seconds: The full delay to wait when no stop arrives.

        Returns:
            True when the wait ended because a stop was requested, False when it
            ran its full timeout.
        """
        return self.stop_event.wait(timeout_seconds)

    def install_signal_handlers(self) -> None:
        """Route SIGINT and SIGTERM to the stop flag, or skip when disabled."""
        if self.install_handler is None:
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.install_handler(signum, self.request_stop)


@dataclass(frozen=True)
class StreamResult:
    """Counts-only outcome from one stream's polling round.

    ``container_id=None`` with ``summary=None`` also encodes a pass that failed
    before any stream ran, so a whole-pass fault counts as an error in
    ``PassResult.errors`` and drives the inter-pass backoff.

    ``polled=False`` IS WHAT SEPARATES THOSE TWO SHAPES, and it exists because
    reading them off ``container_id``/``summary`` cannot (#191). A Gmail stream
    that raised is ``StreamResult(None, error, None)`` and DID reach the
    mailbox; the whole-pass listing failure wears the identical shape and
    reached nothing. Only the second is a round where no stream was polled, so
    only the second must leave the zero-stream streak intact -- a 1-in-3 listing
    blip otherwise resets the counter forever and the scope alarm never fires.
    """

    container_id: str | None
    outcome: str
    summary: RoundSummary | None
    polled: bool = True
    """Whether this row represents a stream the pass actually attempted.

    Defaults True because every row built inside ``run_pass`` is a real attempt,
    errored or not. ``_list_containers`` is the sole producer of ``False``.
    """


@dataclass(frozen=True)
class PassResult:
    """Counts-only outcome from one pass across every selected stream."""

    streams: tuple[StreamResult, ...]

    @property
    def streams_polled(self) -> int:
        """Return the number of streams attempted in this pass."""
        return len(self.streams)

    @property
    def errors(self) -> int:
        """Return the number of streams whose polling round raised."""
        return sum(result.summary is None for result in self.streams)

    @property
    def resyncs(self) -> int:
        """Return the number of streams whose polling round resynced."""
        return sum(result.outcome == RoundState.RESYNCED.name for result in self.streams)

    @property
    def streams_attempted(self) -> int:
        """Return the number of streams this pass actually reached for (#191).

        DIFFERS FROM ``streams_polled`` ONLY ON THE WHOLE-PASS LISTING FAILURE,
        which reports one row for a pass that never chose a stream at all.
        ``streams_polled`` stays as it is because the ``--once`` exit contract
        (#175) and the round log are written against it; this is the number the
        silence alarm reads, so a transient listing error neither hides the
        scope defect nor pretends a stream was watched.
        """
        return sum(result.polled for result in self.streams)

    @property
    def saw_only_out_of_scope_mail(self) -> bool:
        """Whether this pass saw mail, refused some at the scope gate, and triaged none.

        PROVIDER-AGNOSTIC, AND THAT IS THE POINT (#191). On Gmail the scope is
        never applied at stream selection -- ``_stream_ids`` returns the single
        mailbox-wide stream unconditionally -- so a misspelled ``only_listed``
        folder leaves ``streams_attempted`` at 1 forever and the stream-streak
        signal can never fire on the kit's DEFAULT provider. What IS visible on
        every provider is the round's own counts: mail arrived, and the scope
        matched none of what the watcher had not already dealt with.

        A REPLAYED ID IN THE SAME ROUND MUST NOT DISARM THIS, and requiring
        ``skipped_out_of_scope == seen`` did exactly that. That equality reads
        the ORDINARY mailbox as a healthy one: this connector re-offers an
        already-seen id indefinitely by documented design -- a permanently
        abandoned message (#185.2), or one the filing retry bound gave up on
        (#158) -- so a real mailbox delivers the replayed id and the
        out-of-scope invoice on the SAME poll, ``seen`` is 2 while
        ``skipped_out_of_scope`` is 1, and the equality fails on every round
        forever. Measured: 500 rounds, streak pinned at 0, zero warnings. The
        reset arm (``triaged_something``) was corrected for the same event one
        term over, and correcting only the reset left the INCREMENT arm
        unreachable on the shape a typo actually takes.

        SO THE QUESTION IS ASKED OF WHAT THE ROUND DID, NOT OF ONE COUNT'S
        EQUALITY: mail was refused at the scope gate and nothing was classified.
        Those two facts are the whole test, because ``RoundSummary`` enforces
        that ``seen`` partitions into out-of-scope, already-seen, and
        classified -- so ``classified == 0`` already says every OTHER message
        seen was one the watcher had already dealt with, and there is no
        remainder left to hedge about (#201.3).

        THAT HEDGE USED TO BE HERE, as an ``accounted >= seen`` clause guarding
        an "unexplained remainder" no ``poll_once`` tally can produce. It read
        as caution and was fiction: the guard could not fire, and the test that
        proved it worked built the impossible round by hand. The invariant now
        lives at the construction site as a validator, which is where it can
        actually be broken and therefore where it is worth checking.

        ``applied`` is not read either, for the same reason: it is a component
        of ``classified``, so a round with ``classified == 0`` has
        ``applied == 0`` by construction.

        Requires ``skipped_out_of_scope > 0`` so a genuinely quiet mailbox is
        never accused, and so a round of PURE replay is not either: neither saw
        the scope refuse anything. THAT GUARD MAKES THIS PROPERTY UNSAFE AS A
        RESET, and the reset is ``triaged_something`` for exactly that reason:
        an uninformative round is False here without being evidence of anything.
        """
        summaries = tuple(stream.summary for stream in self.streams if stream.summary is not None)
        if not summaries:
            return False
        if sum(summary.skipped_out_of_scope for summary in summaries) == 0:
            return False
        if sum(summary.skipped_unenumerated_container for summary in summaries):
            return False
        return sum(summary.classified for summary in summaries) == 0

    @property
    def saw_unenumerated_containers(self) -> bool:
        """Whether this pass refused mail because the LISTING did not carry its folder.

        THE THIRD SILENCE, AND IT IS NOT THE SCOPE'S (#198). The round refused
        the mail for the same safety reason the scope gate refuses it, and the
        cause is the opposite one: every folder name in the config may be
        spelled correctly and the mode may be ``all_folders``, where there is
        no list at all -- the container the mail sits in was simply absent from
        ``list_containers``. Sending that operator to the scope section is a
        false lead, so this pass reports its own condition and the WARN it
        drives names the listing and the command that prints it.

        Deliberately not "and nothing was classified": a folder missing from
        the listing hides SOME mail while the rest of the mailbox files
        normally, which is exactly the shape that goes unnoticed. One refused
        message is the whole signal, and the streak is what keeps a single
        stale-listing round from warning.
        """
        summaries = tuple(stream.summary for stream in self.streams if stream.summary is not None)
        return any(summary.skipped_unenumerated_container for summary in summaries)

    @property
    def triaged_something(self) -> bool:
        """Whether this pass produced POSITIVE evidence the scope selects mail (#191).

        THE RESET FOR THE SCOPE STREAK, AND IT IS DELIBERATELY NOT
        ``not saw_only_out_of_scope_mail``. That negation reads a quiet round --
        ``seen == 0``, the majority state of a 60-second watcher -- as proof the
        scope is fine, so a typo'd scope on a mailbox receiving one message every
        few minutes never reached three CONSECUTIVE out-of-scope rounds and never
        warned. A quiet poll is the ABSENCE of evidence, which is the same
        reasoning ``streams_attempted`` already applies to a failed listing one
        latch over: neither round watched anything, so neither may clear an alarm.

        Positive evidence is a round that classified or applied a message -- the
        scope let something through and the watcher acted on it. AND THAT IS THE
        WHOLE LIST. ``skipped_already_seen`` used to count as a third term, on
        the reasoning that the replay window can only hold ids the scope admitted
        on an earlier round. That premise is true and the conclusion does not
        follow: an id the scope admitted earlier proves the scope admits THAT
        FOLDER, and proves nothing about the folder the operator misspelled.

        THE THIRD TERM DISARMED THE ALARM PERMANENTLY on the shape a typo
        actually takes -- one wrong name beside several right ones. This
        connector re-offers an already-seen id indefinitely by its own
        documented design: a permanently abandoned message (#185.2) and a
        message the filing retry bound gave up on (#158) both sit in the
        mailbox and are counted ``skipped_already_seen`` on EVERY round
        thereafter. One such id therefore pinned this property True forever, the
        scope streak reset before it could reach ``ZERO_STREAM_WARN_ROUNDS``, and
        an ``only_listed`` scope naming ``Invoces`` beside a working ``Inbox``
        filed nothing for 500 rounds and warned never.

        A replay skip is an id this connector ALREADY DEALT WITH, which is as
        uninformative as the quiet poll and the failed listing the two latches
        above already refuse to be reset by. Same rule, third statement of it.

        REMOVING THE TERM HERE WAS HALF THE FIX AND ALARMED NOTHING BY ITSELF.
        The same replayed id also blocked the INCREMENT arm, which demanded
        ``skipped_out_of_scope == seen``; a round carrying both the replay and
        the refused invoice satisfied neither arm, so the streak held at 0
        rather than climbing. ``saw_only_out_of_scope_mail`` documents the
        matching correction. This docstring describes only the reset.
        """
        summaries = tuple(stream.summary for stream in self.streams if stream.summary is not None)
        return any(summary.classified or summary.applied for summary in summaries)


def _list_containers(context: RoundContext) -> Sequence[Container] | PassResult:
    """List the mailbox's containers, or return the failed pass that replaces them.

    DOCUMENTED FAIL-OPEN, same reasoning as the per-stream boundary in
    ``run_pass``: a listing that failed reported nothing, so the pass has
    nothing to process, and the correct response is an errored pass the loop
    backs off from rather than a dead daemon. Only the error's TYPE NAME is
    logged -- provider prose can quote a folder or a subject.

    Raises:
        OperatorRefusalError: An operator refusal is FATAL, never backed off
            (#171, #195).
            No pass count can describe it and no retry can fix it, so it goes
            to the CLI's refusal handler untouched.
    """
    try:
        return context.provider.list_containers()
    except OperatorRefusalError:
        raise
    except Exception as error:  # ruff: ignore[blind-except]
        error_type = type(error).__name__
        logger.error(f"WATCH LOOP: pass failed listing containers error_type={error_type}")
        # polled=False: this pass never chose a stream, so it is not evidence
        # that the scope selects anything (#191).
        return PassResult(streams=(StreamResult(None, error_type, None, polled=False),))


_ONLY_LISTED: Final = "only_listed"
_CLOSE_NAME_SUGGESTIONS: Final = 3
"""How many listed folder names a WARN offers as the likely intended spelling.

Three because the line is read by a human at 3am with a mailbox in front of
them: one suggestion is a guess presented as an answer, and the whole listing is
the thing they already could not eyeball. ``difflib`` ranks by similarity, so
three is the shortlist a misspelling realistically belongs to.
"""

_unmatched_entries_warned: set[tuple[str, str]] = set()
"""``(mode, entry)`` pairs already warned about, ONCE PER PROCESS (#191).

MODULE STATE ON PURPOSE, and the alternative was worse. This signal is not a
streak: an ``only_listed`` entry that selects no container is wrong on the FIRST
round and stays wrong until a human edits the config, so there is nothing to
count and nothing to wait for. What there is, is a daemon polling every 60
seconds -- so the same true sentence would be emitted 1,440 times a day, which
is noise an operator filters, which costs the same as no warning at all.

The latch is keyed by the ENTRY rather than by the scope, so a config naming
three bad folders produces three lines naming three folders rather than one
line about the first, and each line carries the cause of ITS OWN entry. It is
deliberately never cleared: a folder appearing mid-run makes the entry select
something and the WARN stop being emitted anyway, and a process that restarts
re-arms it, which is the same re-arm rule the streak latches use.
"""


def _closest_listed_names(entry: str, containers: Sequence[Container]) -> str:
    """Return the listed container names most like ``entry``, as a log phrase.

    Args:
        entry: One ``included_folders`` name that matched no container.
        containers: Every container the provider enumerated this pass.

    Returns:
        A phrase naming the closest spellings, or the first few listed names
        when nothing is close enough for ``difflib`` to rank.
    """
    names = [container.name for container in containers]
    close = get_close_matches(entry, names, n=_CLOSE_NAME_SUGGESTIONS, cutoff=0.6)
    if close:
        return "closest folders this mailbox has: " + ", ".join(repr(name) for name in close)
    listed = ", ".join(repr(name) for name in names[:_CLOSE_NAME_SUGGESTIONS])
    return f"nothing in this mailbox is close to it; it lists {listed or 'no folders at all'}"


def _unmatched_entry_reason(
    scope: MailboxScope, named: Sequence[Container], suggestions: str
) -> str:
    """Why an ``only_listed`` entry produced no stream, as the operator's own fix.

    TWO CAUSES THAT NEED DIFFERENT EDITS, and telling them apart is the whole
    value of the line. A MISSPELLED entry matches no container at all, and the
    fix is to correct the spelling or create the folder. A WELL-KNOWN entry --
    ``Archive``, ``Junk Email``, ``Deleted Items`` -- matches a container that
    genuinely exists and is refused anyway, because ``scope.py`` decides the
    system folders before the mode. Sending that operator to check a spelling
    that is already correct tells them nothing true.

    THE SWITCH IS ASKED OF THE SCOPE, NOT OF THE PROVIDER FIELD (#201.1). This
    used to read ``container.well_known`` and build ``include_{tag}`` from it,
    which is a SECOND opinion about what kind of folder this is -- and on
    Gmail it is the wrong one. ``GMAIL_WELL_KNOWN`` carries no archive key, so
    a Gmail label named ``Archive`` arrives tagged None; ``covers`` refused it
    as ``archive`` from its display name and this function, reading the empty
    provider field, could name no switch and printed "the system-folder
    settings" instead. ``MailboxScope.refusal`` is now the only thing that
    answers this, and it is the same resolution ``covers`` refused by, so the
    two cannot disagree on any provider.

    A container in ``named`` that ``refusal`` returns None for was refused by
    the MODE rather than by a switch, which has no single field to point at --
    the misspelling sentence is the honest one for it, because there is no
    system-folder setting to turn on.

    Args:
        scope: The configured scope, asked which switch refused each container.
        named: The containers this entry NAMES, none of which ``covers``
            selects. Empty when the entry matched nothing at all.
        suggestions: Closest listed spellings, for the misspelling branch only.
            A well-known folder that exists needs no suggestion -- the operator
            spelled it right.

    Returns:
        One clause naming the real cause and one naming the edit that fixes it.
    """
    switches = sorted({
        switch
        for container in named
        if (switch := scope.refusal(container.name, well_known=container.well_known))
    })
    if not switches:
        return (
            f"this mailbox has no folder matching it, so its mail is never "
            f"watched ({suggestions}). Fix: correct the name in the mailbox scope "
            "section of the connector config, or create the folder, then restart "
            "the watcher."
        )
    settings = ", ".join(switches)
    return (
        f"this mailbox HAS that folder and the watcher is refused it anyway, so its "
        f"mail is never watched: it is a well-known system folder and those are "
        f"decided before the mode. The spelling is not the problem. "
        f"Fix: set {settings} to true in the mailbox scope section of the connector "
        "config, or drop the folder from included_folders, then restart the watcher."
    )


def _warn_unmatched_scope_entries(context: RoundContext, containers: Sequence[Container]) -> None:
    """WARN once per process for each ``only_listed`` name that yields no stream (#191).

    THE ONLY SIGNAL THAT SEES A FOLDER WHOSE MAIL IS NEVER FETCHED. Both streak
    alarms read what a round OBSERVED, and on Microsoft Graph an entry that
    yields no stream is never polled at all: its mail is not counted out of
    scope, not counted seen, not counted anything. The working folders beside it
    keep ``streams_attempted`` above zero and keep filing mail, so both streaks
    stay at zero and the invoices pile up invisibly. On Gmail the same config is
    visible only as out-of-scope mail, which the streak can see but takes three
    rounds and a mailbox actually receiving invoices to notice.

    THE GUARD IS THE ANSWER, NOT A NAME MATCH, and the difference is a defect
    this signal shipped with. An entry is satisfied only when some enumerated
    container BOTH is named by it (``MailboxScope.entry_names``) AND is one
    ``_stream_ids`` selects (``MailboxScope.covers``) -- both public, both the
    scope's own, so nothing here re-implements either half and the WARN and the
    stream selection cannot disagree. Guarding on the name alone silently
    exempted every entry naming a well-known folder:
    ``only_listed=("Inbox", "Archive")`` with the default
    ``include_archive=False`` matched ``Archive`` by name, suppressed its own
    warning, and was still refused a stream, so the folder's mail was never
    fetched and nothing said so for as long as the install ran.

    Both causes are knowable from the listing on the FIRST round, on BOTH
    providers, whether or not any mail ever arrives -- so this needs no streak
    and gets none. ``_unmatched_entry_reason`` names which of the two it is,
    because they need different edits.

    Args:
        context: The round context, read only for its scope.
        containers: Every container the provider enumerated this pass.
    """
    scope = context.scope
    if scope.mode != _ONLY_LISTED:
        return
    for entry in scope.included_folders:
        named = [container for container in containers if scope.entry_names(entry, container.name)]
        if any(
            scope.covers(container.name, well_known=container.well_known) for container in named
        ):
            continue
        latch = (scope.mode, entry)
        if latch in _unmatched_entries_warned:
            continue
        _unmatched_entries_warned.add(latch)
        reason = _unmatched_entry_reason(scope, named, _closest_listed_names(entry, containers))
        logger.warning(f"WATCH LOOP: mailbox scope names folder={entry!r} and {reason}")


def _stream_ids(context: RoundContext, containers: Sequence[Container]) -> tuple[str | None, ...]:
    """Return the stream ids this pass polls, in the provider's own shape.

    Gmail exposes one mailbox-wide stream, spelled ``None``. Every other
    provider exposes one stream per container, filtered by ``MailboxScope``,
    WHICH REMAINS THE SOLE AUTHORITY ON SELECTION -- this function reads its
    decision and never widens it.
    """
    if context.provider.name == GMAIL_PROVIDER:
        return (None,)
    return tuple(
        container.id
        for container in containers
        if context.scope.covers(container.name, well_known=container.well_known)
    )


def run_pass(context: RoundContext, checkpoint: Callable[[], object] | None = None) -> PassResult:
    """Poll every in-scope stream once, isolating failures and saving per stream.

    Gmail exposes one mailbox-wide stream, represented by ``container_id=None``.
    Other providers expose one stream per selected container, and ``MailboxScope``
    remains the sole authority on which of those containers are selected. THIS
    FUNCTION CHANGES NO SCOPE: which containers are polled is ``MailboxScope``'s
    decision and is untouched here. It does REPORT on that decision -- an
    ``only_listed`` entry that selects no container is WARNed about once per
    process (``_warn_unmatched_scope_entries``, #191) -- and reporting is not
    widening: the entry that produced no stream still produces none. The report
    reads the SAME ``covers`` call the selection does, so what is reported and
    what is polled cannot disagree.

    STATE IS CHECKPOINTED AFTER EACH STREAM, NOT ONCE PER PASS (#162). A Graph
    mailbox fans out to one delta stream per folder, so a pass held N rounds'
    worth of advanced cursors and fresh replay marks in memory with nothing on
    disk until the last one finished. A kill anywhere in that fan-out discarded
    all of them, and the restart re-polled and re-billed every folder already
    done. Saving per stream bounds that loss to the single round in flight.

    ANY EXCEPTION FROM A STREAM IS SURVIVABLE, NOT ONLY ``ProviderError``
    (#157) -- EXCEPT ``OperatorRefusalError``. The narrow catch let a raw ``httpx``
    transport error off ``poll_changes`` -- a WAN blip -- or an unexpected
    escape from a classifier backend kill the daemon outright, which on an
    unattended watcher means mail silently stops being triaged until somebody
    notices. Only the error's TYPE NAME is logged: provider and model prose can
    quote the subject it choked on. ``KeyboardInterrupt`` and ``SystemExit`` do
    not derive from ``Exception`` and are deliberately still unhandled.

    ``OperatorRefusalError`` IS THE CARVE-OUT AND IT IS FATAL (#171, #195). Every other
    survivable fault is transient by nature -- retrying it later is a real
    chance at a different answer. A configuration fault is not: an
    uninitialized install, a missing credential field, a bad JSON layer returns
    the SAME refusal on every pass, so the survive-and-back-off arm turns it
    into a hot loop wearing a health mask, and the traceback the boundary logs
    per stream is the only thing the operator sees. Re-raising instead lets the
    fault reach ``apps/cli/main.py``, whose handler renders exactly one
    ``Configuration error: <msg>`` line and exits 1 -- a refusal a wrapper
    script can read, which is the whole point of that handler. The 0.2.9 smoke
    saw two tracebacks per tier and no refusal line; this is why.

    Args:
        context: Dependencies shared by every polling round in this pass.
        checkpoint: Optional persistence callback, run after EVERY stream's
            round -- including one that errored, because the streams before it
            earned cursors that must not ride on the rest of the pass.

    Returns:
        Counts-only outcomes for every stream attempted, or a single errored
        row when the pass failed before any stream ran.

    Raises:
        OperatorRefusalError: Propagates any refusal, from either boundary,
            so the CLI renders it as a refusal instead of the loop retrying it.
        Exception: Propagates any checkpoint failure so the run stops on unsaved
            state. A poll that cannot be persisted is worse than one not made.
    """
    context.state.begin_pass()
    containers = _list_containers(context)
    if isinstance(containers, PassResult):
        return containers
    _warn_unmatched_scope_entries(context, containers)
    results: list[StreamResult] = []
    for container_id in _stream_ids(context, containers):
        try:
            summary = poll_once(context.model_copy(update={"container_id": container_id}))
        except OperatorRefusalError:
            # FATAL, not transient (#171, #195). The ``finally`` below still runs, so
            # the streams already polled keep their cursors; what does not
            # happen is another pass against an installation that cannot work.
            raise
        except Exception as error:  # ruff: ignore[blind-except]
            # DOCUMENTED FAIL-OPEN. The refusal is HANDLED rather than swallowed:
            # the stream is recorded as an error, which is what drives the
            # caller's backoff and the pass's non-zero exit status in `--once`
            # mode. The traceback is discarded at this boundary on purpose --
            # exception TEXT can quote a subject line, and a leaked log must not
            # be a leaked mailbox.
            error_type = type(error).__name__
            logger.error(
                f"WATCH LOOP: stream failed container_id={container_id} error_type={error_type}"
            )
            results.append(StreamResult(container_id, error_type, None))
            continue
        finally:
            # AFTER EVERY STREAM, ERRORED OR NOT. The streams already polled in
            # this pass earned cursors and replay marks, and those must not be
            # lost because a later folder failed or the process was killed
            # before the pass ended (#162).
            if checkpoint is not None:
                checkpoint()
        results.append(StreamResult(container_id, summary.state.name, summary))
    return PassResult(streams=tuple(results))


def _account_rounds(context: RoundContext | Sequence[AccountRound]) -> tuple[AccountRound, ...]:
    """Normalise ``run``'s context argument to the account list a round polls.

    ONE ROUND CONTEXT BECOMES ONE ACCOUNT WITH AN EMPTY ADDRESS, and the
    emptiness is the whole back-compatibility guarantee: ``_log_round`` omits
    the prefix on an empty address, so a single-account caller's log line is
    byte-identical to the one it produced before accounts existed, and the
    context's own ``account`` field (empty unless the caller set it) is what
    the receipts carry. Nothing here invents an address.

    Raises:
        ValueError: An EMPTY account list. A watcher with no mailbox to poll
            would otherwise loop forever reporting clean rounds that watched
            nothing -- the exact silence the streak alarms exist to catch,
            manufactured at startup. The CLI never produces one (a config with
            no accounts falls back to the single-account form), so this is a
            programming error and it is loud.
    """
    if isinstance(context, RoundContext):
        return (AccountRound(address="", context=context),)
    if not context:
        raise ValueError(
            "watch was given an empty account list, so a round would poll no "
            "mailbox at all. ACTION REQUIRED: pass one RoundContext, or at "
            "least one AccountRound."
        )
    return tuple(context)


def _round(rounds: Sequence[AccountRound], checkpoint: Callable[[], object] | None) -> PassResult:
    """Poll every account once and combine the passes into ONE round result.

    CONCATENATING THE STREAMS IS WHAT KEEPS EVERYTHING DOWNSTREAM UNCHANGED.
    ``PassResult``'s counts are all derived from ``streams``, so the streaks,
    the backoff, the health reporter, and the ``--once`` exit contract read a
    two-account round exactly as they read a one-account one -- an error in
    either mailbox is an error in the round, which is the worst-account rule
    the health file wants.

    A single-account round returns the same object shape ``run_pass`` returned
    before, so a one-account install is byte-for-byte what it was.
    """
    results = run_accounts(rounds, run_pass, _log_round, checkpoint)
    return PassResult(streams=tuple(stream for result in results for stream in result.streams))


def run(
    context: RoundContext | Sequence[AccountRound],
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    once: bool = False,
    sleep: Callable[[float], None] | None = None,
    control: LoopControl | None = None,
    **options: Unpack[RunOptions],
) -> PassResult | None:
    """Run polling passes in the foreground until the caller requests a stop.

    THE DAEMON FORM ALARMS ON A SCOPE THAT TRIAGES NOTHING (#191), by three
    signals sharing one threshold and one latch (``_SilenceStreaks``) -- the
    third of them naming the container LISTING rather than the scope (#198).
    After
    ``ZERO_STREAM_WARN_ROUNDS`` consecutive rounds that polled no stream, or
    that saw mail and matched none of it, one WARN names the cause and the fix,
    then stays quiet until a round breaks the streak. The second signal exists
    because the first cannot fire on Gmail at all: Gmail has one mailbox-wide
    stream, so scope is applied per message and ``streams_attempted`` is always
    1 on the kit's default provider.

    ``once=True`` is deliberately excluded FROM THE STREAKS: the one-shot
    exit-status contract (#175, ``apps/cli/main.py``) owns that pass, and this
    loop's own status semantics are unchanged. See the PROPOSAL in this module's
    ``_ONCE_EXIT_PROPOSAL`` for the Gmail hole that contract still has.

    THE UNMATCHED-ENTRY WARN IS NOT SO EXCLUDED, and the difference is the
    reason it exists. A streak is an inference from several rounds, which one
    pass cannot make; an ``only_listed`` entry that selects no container in the
    listing is a FACT of the config and the mailbox, complete in one pass. It
    fires from ``run_pass``, so ``--once`` gets it too -- it changes no exit
    status, so the #175 contract is untouched, and an operator running the
    one-shot to check an install is exactly who most needs to be told the
    folder they named does not exist.

    EVERY CONFIGURED ACCOUNT IS POLLED ONCE PER ROUND (#69, decision D11), and
    the clock is per PROCESS rather than per mailbox: one round means "each
    account got a pass", and the interval, the backoff, the streaks, and the
    health file all describe the round rather than any one account. A single
    ``RoundContext`` is accepted unchanged and normalised to a one-account
    round whose address is empty, so every existing caller -- and every
    single-account install -- runs the identical code path with an identical
    log line and identical receipts.

    Args:
        context: Either one ``RoundContext`` (the single-account form, kept
            for every existing caller) or the install's configured
            ``AccountRound`` list, first account first. A list is polled in
            order, one pass per account per round, and the results are
            combined into the single ``PassResult`` the streaks, the health
            reporter, and the backoff already read.
        interval_seconds: Base seconds to wait between completed passes.
        once: Return after exactly one pass without sleeping.
        sleep: Injectable wait callable. ``None``, the default, uses the stop
            event's own interruptible wait (``LoopControl.wait``) -- which is
            what makes a SIGTERM during a 15-minute backoff return in
            milliseconds instead of being ignored until SIGKILL (#162). A test
            passing a recording callable still drives the loop's delay sequence
            without a real clock; it simply does not exercise the wake path,
            which is asserted directly against ``LoopControl.wait``.
        control: Injectable stop event and signal-handler installation boundary.
        checkpoint: Optional persistence callback, run after EVERY STREAM's
            round rather than once per pass, and once more when the pass itself
            failed before any stream ran (#162).

    Returns:
        The counts-only outcome of the LAST completed pass, or ``None`` when a
        stop was already pending and no pass ran. The loop itself treats an
        errored pass as survivable -- backing off and continuing -- so this
        return value is how a one-shot caller learns the round was not clean.

    Raises:
        OperatorRefusalError: Propagates a refusal from ``run_pass`` WITHOUT
            backing off or running another pass. Backoff cannot fix an install;
            the CLI turns this into one refusal line and exit 1 (#171).
        Exception: Propagates any checkpoint failure so the run stops on unsaved state.
    """
    rounds = _account_rounds(context)
    loop_control = control if control is not None else LoopControl()
    loop_control.install_signal_handlers()
    # ONCE PER START, before any pass. An unsettled intent names a message the
    # PREVIOUS run may have moved without writing its receipt -- the audit gap
    # the ledger's own fsync cannot reach, because it protects a row that was
    # never begun. Reported at ERROR and never repaired: inventing a receipt
    # for a mutation this process did not observe would be worse than naming
    # the gap. Compaction is inside `reconcile`, so the journal cannot grow.
    intents.reconcile()
    checkpoint = options.get("checkpoint")
    delay_seconds = interval_seconds
    result: PassResult | None = None
    streaks = _SilenceStreaks()
    reporter = _HealthReporter(data_dir())
    while not loop_control.stop_requested:
        result = _round(rounds, checkpoint)
        streaks.record(result)
        # ONCE PER ROUND, with the round's COMBINED result -- so on a
        # multi-account install the health file records the worst account's
        # state, which is what `_first_error_type` reads off the concatenated
        # streams. One process, one health document (#69).
        reporter.record(result)
        _log_round(result)
        if not once:
            streaks.warn_if_due(rounds[0].context)
        if checkpoint is not None and result.streams_polled and result.streams[0].summary is None:
            # The whole-pass failure arm ran no stream, so `run_pass` reached no
            # per-stream checkpoint. Persist here so a pass that failed listing
            # containers still leaves the previous pass's state on disk.
            checkpoint()
        if once or loop_control.stop_requested:
            return result
        delay_seconds = _next_delay(result, delay_seconds, interval_seconds)
        _wait(loop_control, delay_seconds, sleep)
    return result


def _wait(
    control: LoopControl, delay_seconds: float, sleep: Callable[[float], None] | None
) -> None:
    """Wait between passes, waking immediately when a stop is requested.

    The default is the stop event's own wait, so the daemon's shutdown latency
    is bounded by the signal rather than by the backoff (#162). An injected
    ``sleep`` is honoured verbatim, which is how the delay-sequence tests assert
    the backoff schedule with no clock at all.
    """
    if sleep is None:
        control.wait(delay_seconds)
        return
    sleep(delay_seconds)


def _next_delay(result: PassResult, current: float, base: float) -> float:
    """Return the next inter-pass delay, resetting after any error-free pass."""
    if result.errors == 0:
        return base
    return min(current * 2.0, MAX_BACKOFF_SECONDS)


_LISTING_FIX_SENTENCE: Final = (
    "the container listing this connector reads did not carry the folder that "
    "mail is sitting in, so the scope never got to decide and no folder name in "
    "the config is wrong. A folder created or renamed since the watcher started, "
    "or one the signed-in account cannot see, is missing for exactly this reason. "
    "Check: run `inbox-zero-connector list-folders` and compare what it prints "
    "against the folder the skipped mail is actually in; if the folder is absent "
    "there, the mailbox permission or the folder itself is the fix, not the scope."
)
"""The whole actionable half of the listing WARN (#198).

A CONSTANT RATHER THAN A MODE TABLE, because this failure does not depend on
the scope mode at all -- every mode reads the same listing, and under
``all_folders`` there is no folder list to correct. It used to be a clause
inside ``_scope_fix_sentence``'s ``all_folders`` row, where only one mode could
ever read it and where it was attached to a line whose subject was the scope.
"""


def _scope_fix_sentence(context: RoundContext) -> str:
    """Return the actionable half of a silence WARN, worded for the ACTUAL mode.

    The old text blamed "an only_listed list" in every mode, including
    ``inbox_only`` and ``all_folders`` where no list exists -- misdirection that
    sends an operator to edit a config section that is empty. Each mode gets the
    sentence describing the thing that can actually be wrong in it, AND ITS OWN
    FIX: the shared fix sentence reproduced the original misdirection on
    ``all_folders``, where there is no folder list to correct and no wider mode
    to widen to, so the only actionable half of the line named two edits the
    operator cannot make. That mode's mail is landing somewhere the container
    listing did not enumerate, or in an excluded system folder, and the check is
    a comparison the operator can actually run.

    ``only_listed`` NAMES NO CAUSE AND POINTS AT THE LINE THAT CAN. This
    sentence is reached from a STREAK, which counts rounds and knows nothing
    about which entry failed or why; it used to assert "a renamed or misspelled
    folder matches nothing", which is confidently wrong on a scope naming
    ``Archive`` -- the mailbox has that folder and ``include_archive`` is what
    refuses it, so the operator checks a spelling that was already correct and
    is told nothing true. ``_warn_unmatched_scope_entries`` names each failing
    entry with its own reason, so this sentence sends the operator there instead
    of guessing between two causes it cannot distinguish.

    Args:
        context: The round context, read only for the scope's mode.

    Returns:
        One sentence naming the likely cause and one naming the check or fix.
    """
    mode = context.scope.mode
    edit_the_scope = (
        "Fix: correct the folder names in the mailbox scope section of the "
        "connector config, or widen the mode, then restart the watcher."
    )
    causes = {
        "only_listed": (
            "the only_listed list selects no folder that carries this mail. This "
            "line cannot say WHICH entry or why -- it counts rounds, not folders",
            "Check: read this watcher's earlier per-folder scope WARN lines. Each "
            "one names an entry that yields no stream and gives its own reason and "
            "fix -- an entry the mailbox has no folder for, or a well-known system "
            "folder that include_archive/include_junk/include_deleted refuses. If "
            "every entry was accepted, the mail is landing in a folder none of "
            "them names. Fix: edit the mailbox scope section of the connector "
            "config accordingly, then restart the watcher.",
        ),
        "all_except": (
            "the all_except list excludes every folder that carries mail, "
            "so nothing is left to watch",
            edit_the_scope,
        ),
        "inbox_only": (
            "inbox_only watches the well-known Inbox alone, and this mailbox "
            "either exposes no Inbox or delivers mail elsewhere",
            edit_the_scope,
        ),
        "all_folders": (
            "all_folders already covers every folder this connector can "
            "enumerate, so there is no folder list to correct -- the mail is "
            "landing in an excluded well-known system folder",
            "Check: open the mailbox and find the folder the skipped mail is "
            "actually sitting in, then confirm include_archive/include_junk/"
            "include_deleted does not refuse it. Editing folder names in the "
            "config cannot fix this mode. If the folder is one the connector "
            "cannot enumerate at all, this watcher says so in its own listing "
            "WARN rather than here.",
        ),
    }
    cause, fix = causes.get(
        mode, ("the configured scope matches no folder in this mailbox", edit_the_scope)
    )
    return f"scope mode={mode}; {cause}. {fix}"


@dataclass
class _SilenceStreaks:
    """Three ways a watcher can be silently triaging nothing, one latch (#191, #198).

    THE FAILURE BOTH SURFACE IS SILENCE, NOT AN ERROR. A round that watched
    nothing is error-free by every count the loop keeps, so the daemon logs
    INFO, exits nothing, and backs off nothing -- a typo'd folder name reads in
    the log exactly like a quiet mailbox, forever. ``watch --once`` refuses one
    shape of this (#175); the service-unit deployment (#180) is the long-running
    form, which had no alarm at all.

    ``no_stream`` counts rounds where the pass polled no stream. A whole-pass
    listing failure does NOT reset it (``streams_attempted``, not
    ``streams_polled``): a mailbox that blips on one listing in three otherwise
    masks the scope defect permanently, which is exactly the deployment least
    likely to have somebody reading every INFO line.

    ``out_of_scope`` counts rounds where mail arrived, the scope refused some of
    it, and nothing the watcher had not already dealt with got through. This is
    the signal that works on GMAIL, the kit's default provider, where the first
    one structurally cannot fire -- Gmail exposes one mailbox-wide stream, so
    scope is applied per message inside the round and ``streams_attempted`` is 1
    no matter what the scope says.

    AN ALREADY-SEEN ID CLEARS NEITHER ARM OF ``out_of_scope``, AND BOTH ARMS
    HAD TO BE FIXED FOR THAT TO MEAN ANYTHING. Correcting the RESET alone --
    dropping ``skipped_already_seen`` from ``triaged_something`` -- stopped a
    replay round zeroing the counter, and left the counter at zero anyway,
    because the INCREMENT demanded ``skipped_out_of_scope == seen`` and a
    replayed id in the same round breaks that equality. On the mailbox the
    issue describes -- one abandoned id re-offered every poll beside a new
    invoice the scope refuses -- the streak sat at 0 for 500 rounds after the
    reset fix, and the entry WARN was doing all the work. The increment now
    reads the round's ANSWER (mail refused, nothing triaged, the remainder
    already dealt with), so the mixed round the mailbox actually produces
    advances it.

    NEITHER STREAK IS RESET BY A ROUND THAT WATCHED NOTHING, and that is one
    rule stated three times rather than three special cases. ``no_stream`` reads
    ``streams_attempted`` so a FAILED LISTING cannot clear it; ``out_of_scope``
    reads ``triaged_something`` so a QUIET POLL cannot clear it, and
    ``triaged_something`` no longer counts ``skipped_already_seen`` so an
    ALREADY-SEEN id cannot clear it either. Each of the three was found the same
    way: an ordinary, benign event was being read as evidence the scope selects
    mail, and it silently disarmed the alarm for as long as the event kept
    happening -- which for a permanently abandoned message (#185.2) is forever.

    NEITHER STREAK CAN SEE A FOLDER THAT IS NEVER POLLED, which is why
    ``_warn_unmatched_scope_entries`` sits beside them and is not a streak at
    all. Both counters here are inferences from what a round OBSERVED; a
    misspelled Graph folder yields no stream, so its mail is never fetched and
    never observed by anything.

    ONE LINE PER STREAK IS AN EXPLICIT LATCH (``out_of_scope_warned``), not the
    counter's own arithmetic. It used to be ``== ZERO_STREAM_WARN_ROUNDS``, which
    is only once-per-streak while the counter MOVES every round -- and the
    hold-across-a-quiet-round rule above is exactly the change that stops it
    moving. A streak sitting at the threshold re-satisfied the equality on every
    quiet round after it, so the operator got the same line repeatedly. The latch
    is set when the line is emitted and cleared by the same reset that zeroes the
    counter, so a genuine second streak still re-arms it.

    ``unenumerated`` counts rounds where mail was refused because its container
    was absent from the LISTING (#198). It is a third streak rather than more
    terms on ``out_of_scope`` because its fix is a different one: no folder name
    in the config is wrong, and under ``all_folders`` there is no list to edit --
    the connector could not enumerate the folder the mail is in, and
    ``list-folders`` is what shows that. Sharing the scope counter is how the
    operator used to be sent to correct a scope that was already correct.

    A round that is silent in MORE THAN ONE WAY warns once, because each later
    signal is skipped while an earlier one is warning about the same round.
    """

    no_stream: int = 0
    out_of_scope: int = 0
    out_of_scope_warned: bool = False
    unenumerated: int = 0
    unenumerated_warned: bool = False
    """The third streak (#198): mail refused because the LISTING lacked its folder.

    Kept beside the other two rather than folded into ``out_of_scope``, because
    the operator's fix is a different one and the old shared counter said the
    scope was at fault when it was not.
    """

    def record(self, result: PassResult) -> None:
        """Advance or reset both streaks from one completed pass.

        Advance is cheap and reset is expensive: an alarm cleared by a round
        that saw nothing is an alarm that never fires. Both counters therefore
        HOLD across an uninformative round rather than returning to zero.
        """
        self.no_stream = 0 if result.streams_attempted else self.no_stream + 1
        if result.saw_only_out_of_scope_mail:
            self.out_of_scope += 1
        elif result.triaged_something:
            self.out_of_scope = 0
            self.out_of_scope_warned = False
        if result.saw_unenumerated_containers:
            self.unenumerated += 1
        elif result.triaged_something:
            # Same rule as the scope streak: only POSITIVE evidence clears it,
            # so a quiet poll cannot disarm an alarm about mail it never saw.
            self.unenumerated = 0
            self.unenumerated_warned = False

    def warn_if_due(self, context: RoundContext) -> None:
        """Emit at most one WARN for whichever streak just hit the threshold."""
        if self.no_stream == ZERO_STREAM_WARN_ROUNDS:
            logger.warning(
                f"WATCH LOOP: no stream polled for rounds={ZERO_STREAM_WARN_ROUNDS} "
                "consecutive rounds -- the configured scope selected no folder "
                f"({_scope_fix_sentence(context)})"
            )
            return
        if self.out_of_scope >= ZERO_STREAM_WARN_ROUNDS and not self.out_of_scope_warned:
            self.out_of_scope_warned = True
            logger.warning(
                f"WATCH LOOP: scope matched nothing it saw for rounds="
                f"{ZERO_STREAM_WARN_ROUNDS} consecutive rounds -- mail arrived, the "
                "scope refused some of it, and every message it did not refuse was "
                "one this watcher had already dealt with, so nothing was classified "
                f"or filed ({_scope_fix_sentence(context)})"
            )
            return
        if self.unenumerated >= ZERO_STREAM_WARN_ROUNDS and not self.unenumerated_warned:
            self.unenumerated_warned = True
            logger.warning(
                f"WATCH LOOP: mail refused because the container listing did not carry "
                f"its folder, for rounds={ZERO_STREAM_WARN_ROUNDS} consecutive rounds "
                f"-- {_LISTING_FIX_SENTENCE}"
            )


class _HealthReporter:
    """Record every round's outcome, and notify on the transitions that matter (#64).

    ONE OBJECT RATHER THAN TWO because the notification decisions are all
    functions of the state transition this same object just wrote. Splitting
    them would mean the notifier re-reading the file the writer had just
    produced and re-deriving "did this round change anything", which is how the
    health readout and the notification start disagreeing.

    EVERY LATCH IS PER PROCESS, and that is the whole noise budget. A watcher
    that fails for a week must notify ONCE, not ten thousand times: the second
    notification tells the operator nothing the first did not, and a desktop
    that pops a toast every sixty seconds gets its notifications turned off --
    which loses the first one too. The healthy->failing latch clears on
    recovery, so a genuinely new outage is announced again; the failure-count
    and re-auth latches never clear, because their conditions are one-way.

    WRITING NEVER RAISES INTO THE LOOP. Health is a REPORT on the watcher, so a
    report that cannot be written must not stop the thing it reports on -- the
    inversion where the health file kills the watcher is strictly worse than
    having no health file. Failures are logged and the round continues.
    """

    def __init__(self, data_directory: Path) -> None:
        """Bind the health file's directory and clear all three notify latches.

        The latches start false because they are per process by design, as the
        class docstring explains: a freshly started watcher will announce a
        failure a previous process already announced, and nothing about the
        latch state is carried across restarts on purpose.
        """
        self.data_directory = data_directory
        self.notified_failing = False
        self.notified_streak = False
        self.notified_reauth = False

    def record(self, result: PassResult) -> None:
        """Write this round's health and fire any notification it just earned."""
        error = _first_error_type(result)
        try:
            health = record_round(self.data_directory, error=error)
        except OSError:
            # Already logged with its path by the atomic writer. Swallowed here
            # on purpose: see the class docstring. The line below says what the
            # swallow COST -- this round's notifications never got the chance to
            # fire -- which the writer's own message cannot know.
            logger.warning(
                "WATCH LOOP: health not recorded this round; "
                f"notifications skipped error_type={error!r}"
            )
            return
        self._notify_failing(health)
        self._notify_streak(health)
        self._notify_reauth(health)

    def _notify_failing(self, health: Health) -> None:
        """Announce the healthy->failing edge once, and re-arm on recovery."""
        if health.last_error is None:
            self.notified_failing = False
            return
        if self.notified_failing:
            return
        self.notified_failing = True
        notify(
            "Inbox Zero: watcher failing",
            f"A watch round failed ({health.last_error}). Run `inbox-zero-connector status`.",
        )

    def _notify_streak(self, health: Health) -> None:
        """Announce the failure streak once it reaches the threshold."""
        if health.consecutive_failures < NOTIFY_FAILURE_STREAK or self.notified_streak:
            return
        self.notified_streak = True
        notify(
            "Inbox Zero: watcher still failing",
            f"{health.consecutive_failures} rounds in a row have failed. Mail is not being filed.",
        )

    def _notify_reauth(self, health: Health) -> None:
        """Announce an imminent credential expiry once, ahead of the failure.

        THE ADVANCE WARNING IS THE POINT (decision D9): an expiry is the one
        failure this watcher can see coming, so telling the operator only when
        it lands wastes the only predictable outage there is. It fires today on
        no install -- ``token_expires_at`` is always ``None`` because nothing
        records when the grant was made -- and the arm is written now so the
        credential layer's eventual timestamp needs no change here.
        """
        if self.notified_reauth or health.token_expires_at is None:
            return
        try:
            expires = datetime.fromisoformat(health.token_expires_at)
        except ValueError:
            # Fail open: an unparseable stamp must not stop the round. It DOES
            # disarm the one warning this watcher can give ahead of time, so it
            # is a real condition and warns rather than whispers.
            logger.warning(
                "WATCH LOOP: unparseable token_expires_at; re-auth warning disarmed "
                f"value={health.token_expires_at!r}"
            )
            return
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires - datetime.now(UTC) > REAUTH_WARNING_WINDOW:
            return
        self.notified_reauth = True
        notify(
            "Inbox Zero: re-authorization needed",
            f"The mailbox credential expires {expires.date().isoformat()}. Run `auth` to renew.",
        )


def _first_error_type(result: PassResult) -> str | None:
    """Return the first failing stream's exception type name, or ``None``.

    THE FIRST, not a joined list: the health file holds one ``last_error``
    because an operator acts on the kind of failure, and every stream in a pass
    that fails usually fails the same way (one dead credential, one WAN outage).
    The count lives in ``consecutive_failures``.
    """
    for stream in result.streams:
        if stream.summary is None:
            return stream.outcome
    return None


def _log_round(result: PassResult, account: str = "") -> None:
    """Log one counts-only INFO record for a completed polling pass.

    ``account`` PREFIXES the line rather than being appended to it (#69), so a
    multi-account install's log reads as one block per mailbox instead of a
    merged stream an operator has to parse a trailing field out of. Empty on
    the combined round line and on every single-account install, which is what
    keeps the existing line byte-identical.
    """
    summaries = tuple(stream.summary for stream in result.streams if stream.summary is not None)
    prefix = f"{account} " if account else ""
    logger.info(
        f"{prefix}WATCH LOOP: round complete streams_polled={result.streams_polled} "
        f"errors={result.errors} resyncs={result.resyncs} "
        f"seen={sum(summary.seen for summary in summaries)} "
        f"skipped_out_of_scope={sum(summary.skipped_out_of_scope for summary in summaries)} "
        f"skipped_unenumerated_container="
        f"{sum(summary.skipped_unenumerated_container for summary in summaries)} "
        f"skipped_missing={sum(summary.skipped_missing for summary in summaries)} "
        f"skipped_already_seen={sum(summary.skipped_already_seen for summary in summaries)} "
        f"classified={sum(summary.classified for summary in summaries)} "
        f"applied={sum(summary.applied for summary in summaries)} "
        f"failed={sum(summary.failed for summary in summaries)} "
        f"cursors_stored={sum(summary.cursor_stored for summary in summaries)}"
    )
