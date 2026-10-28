"""Watch cursor persistence for the unattended watcher.

WHAT THIS MODULE IS, AND WHAT IT DELIBERATELY IS NOT
    This is the provider-NEUTRAL state layer under a polling watch loop. It
    persists opaque resume tokens, tracks resync generations, remembers which
    message ids were recently processed, and counts how many times a message
    has been attempted and refused. It performs no network I/O,
    holds no provider client, classifies nothing, and touches no mail. The loop
    that calls a provider and acts on the results is another module's job; this
    one only remembers where that loop got to.

    The split is deliberate. A watcher's correctness lives almost entirely in
    its state handling -- did the cursor survive the crash, did the replay get
    reprocessed, did the resync reset the right cursor -- and none of that is
    testable if it is welded to a network client. Everything here runs in a
    test with no credential and no socket.

WHY POLLING AND NOT WEBHOOKS
    Settled by the constraints brief before any code was written, and the
    reasoning holds on both providers. Microsoft Graph states twice that a
    change-notification endpoint must be publicly reachable HTTPS, and that it
    does not deliver otherwise; Gmail's push tier needs a Cloud Pub/Sub
    subscription reaching an endpoint the same way. The buyer's clients are law
    firms behind NAT with no inbound ports -- which is the correct
    configuration for that customer, not a deficiency to engineer around. A
    push tier therefore cannot run at the client at all.

    Worse, the push failure mode is silent and permanent: an endpoint whose
    responses exceed the allowance in more than 15% of a 10-minute window
    enters "drop" state, and Microsoft states plainly that dropped
    notifications cannot be recovered. A poller that misses a round simply
    picks up more on the next one. For a watcher sold on "nothing falls
    through," that asymmetry decides it.

    Polling is not a degraded fallback. Microsoft documents change tracking as
    the peer of webhooks -- "delta query uses a pull model... while change
    notifications use a push model" -- and Gmail documents a full
    history-based sync path with no push component required.

THE THREE FACTS THIS MODULE IS SHAPED AROUND
    1. The resume token is OPAQUE. It is copied and replayed whole. Nothing
       here parses it, inspects it, validates its shape, or constructs one.
    2. A token has NO GUARANTEED LIFETIME that shared code may rely on.
       Providers differ -- one evicts tokens from a fixed-capacity cache with
       no stated floor, another publishes a typical availability window but
       explicitly declines to guarantee it. Expiry is therefore normal
       operation on a quiet mailbox, not an exceptional condition, and this
       module assumes the pessimistic case for both.
    3. Replays are documented and expected. Providers make a best effort to
       reduce them but do not eliminate them, so processing must be
       idempotent, keyed on message id. That is what ``seen``/``mark_seen``
       exist for.

ONE STATE FILE PER PROVIDER
    The file is named for the provider it belongs to. A host may watch a
    Google mailbox and a Microsoft mailbox at once, and a shared filename
    would let each watcher's whole-file save erase the other's cursors with no
    error at all. See ``STATE_FILENAME_TEMPLATE``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from inbox_zero_connector.config import PROVIDER_NAMES
from inbox_zero_connector.utils import write_text_atomic
from inbox_zero_connector.utils.paths import data_dir

SCHEMA_VERSION: Final = 1
"""On-disk format version for the watch state file.

Separate from the ledger's version on purpose: the two files have unrelated
lifecycles, and coupling their version numbers would mean bumping one to
describe a change in the other.
"""

CURSOR_GENERATION: Final = 1
"""Which BUILD of the connector a stored resume token may have been minted by.

NOT ``SCHEMA_VERSION``, AND THE DISTINCTION IS THE POINT. ``SCHEMA_VERSION``
describes the file's LAYOUT: bump it when a reader can no longer parse what a
writer produced. This describes the VALUES: the layout is unchanged, an old
file parses perfectly, and every token inside it is nonetheless wrong.

WHAT MADE THEM WRONG (#143). Before the fix, ``round.poll_once`` looked its
cursor up BY CONTAINER and then polled WITHOUT one. Microsoft Graph has no
mailbox-wide delta, so a cold start with no container is a GET against
``me/mailFolders/inbox/messages/delta`` -- which means every per-folder stream
stored an INBOX delta link under a FOLDER's cursor key. Those rows are not
stale, they are mislabelled, and the token is opaque so nothing may inspect or
rewrite one to find out which. Fixing the poll does not fix them: a replayed
inbox link keeps returning the inbox forever, on corrected code.

SO THEY ARE DROPPED, WHICH IS A PATH THE WATCHER ALREADY SUPPORTS. "No cursor"
is the ordinary state of every stream before its first round and after every
token expiry (``cursor_for``), so the drop costs ONE cold start per stream and
nothing else. The replay window is kept across it, exactly as ``resync_required``
keeps it, so the re-read that cold start returns is suppressed rather than
re-filed -- which is what makes the drop safe rather than a mass re-file.

Bump this ONLY when a change makes previously-stored tokens unreplayable for
what they claim to be. An ordinary provider-side expiry is not that: it is
``resync_required``, per stream, and already handled.
"""

STATE_FILENAME_TEMPLATE: Final = "watchstate-{provider}.json"
"""The state filename, SCOPED BY PROVIDER.

WHY THE PROVIDER IS IN THE NAME
    One host can watch a Google mailbox and a Microsoft mailbox at the same
    time, and the two run as independent watchers over the same data
    directory. A single shared ``watchstate.json`` would have both of them
    writing the same file: each save is a whole-file replace, so the last
    writer wins and the other provider's cursors are gone. The loss is silent
    -- the surviving file is well-formed and parses cleanly -- and its cheapest
    consequence is a full resync, its worst a stream whose cursor jumped to a
    token from the wrong provider entirely.

    The provider therefore belongs in the PATH, not in a key inside the file.
    Keying by provider within one file would leave both processes writing that
    one file, and the clobber is the write, not the layout.
"""


MAILBOX_WIDE_KEY: Final = "\x00mailbox-wide"
"""The store key standing in for ``container_id=None``.

WHY A KEY AND NOT A SENTINEL STRING
    A mailbox-wide cursor genuinely has no container, so it needs a slot in the
    same dict as the per-container cursors -- ``cursors`` is one map and the
    watcher reads it as one map. The obvious spellings (``""``, ``"*"``,
    ``"__mailbox__"``) all live in the SAME namespace as real provider
    container ids, so a provider that ever emits that exact id silently
    overwrites the mailbox-wide cursor with a folder's, or the reverse. That
    collision has no error, no log line, and shows up as a watcher reading the
    wrong stream.

    A leading NUL byte cannot collide, because no provider container id can
    contain one: Graph folder ids are base64url text and Gmail label ids are
    ``Label_<digits>`` plus the uppercase system names. It also survives the
    JSON round trip -- ``json`` encodes it as ``\\u0000`` and decodes it back
    -- so the on-disk file stays readable and the key stays exact.

    It is deliberately not derived from any provider vocabulary. Shared code
    owns this key; no adapter supplies it or matches on it.
"""

SEEN_LIMIT: Final = 500
"""How many recently-processed message ids are remembered per cursor.

WHY 500 AND NOT MORE
    This bounds a REPLAY window, not a history. Providers replay the same
    change in a subsequent response shortly after the original -- the
    documented case is a change reappearing in the next round or two, not one
    reappearing days later. 500 ids covers many polling rounds of a busy
    mailbox: an inbox taking 500 new messages between the original and its
    replay is far outside the behaviour the replay guard is defending against.

WHY IT IS BOUNDED AT ALL
    The alternative is a set that grows for the life of the deployment. On an
    unattended watcher that is a slow leak in both the file and in memory, and
    it is paid for by every process that loads the state. A bound makes the
    cost of the guard fixed and knowable.

THE BOUND APPLIES TO CARRYOVER, NEVER TO THE PASS THAT IS RUNNING (#161b)
    Plain oldest-first eviction is SELF-DEFEATING at the one moment it matters
    most. A single page of 550 ids marked in one pass evicted 100% of its own
    marks -- the 51st mark pushed out the 1st, and by the end of the page not
    one id from it remained. A replay of that page then reprocessed every
    message on it (550 of 550, observed), which is precisely the outcome the
    window exists to prevent, produced BY the window.

    So ``mark_seen`` never evicts an id the CURRENT pass recorded. The window
    may exceed ``SEEN_LIMIT`` while a pass is in flight and trims back to the
    bound -- oldest CARRYOVER first -- when the next pass begins. See
    ``WatchState.begin_pass`` and ``WatchState.mark_seen``.

    The transient overshoot is the cheap side of the trade: it is bounded by the
    size of one page, it lasts one pass, and the alternative is a guard that
    reliably fails on exactly the large pages a backlog produces.

THE BOUND IS A FLOOR, NOT A CEILING -- IT IS SIZED FROM OBSERVED VOLUME (#161 A1)
    Protecting a pass from its own marks moved the self-defeat exactly one pass
    later and did not remove it. ``begin_pass`` trimmed back to a FLAT 500, so a
    550-id round kept its 550 marks only until the next round started, lost the
    oldest 50 at that instant, and -- offered the same backlog again -- marked
    550 once more and lost them again. Measured cliff: a 500-id round replays
    0%, a 501-id round replays 100%, every round, for as long as the backlog is
    that size. On a 600-id backlog that is 600 billed classifier calls and 600
    provider writes PER ROUND.

    The fix is not a bigger constant; a constant is the same cliff at a new
    number, and picking it means guessing a mailbox's busiest hour. The trim
    target is sized from what the deployment actually delivered: at least
    ``SEEN_LIMIT``, and never less than the largest pass in the recent history
    ``PASS_VOLUME_HISTORY`` keeps. One round therefore cannot evict its own ids
    before the next poll is offered them.

    The growth stays bounded and temporary. The target follows the mailbox down
    as well as up -- a spike ages out of the history after
    ``PASS_VOLUME_HISTORY`` quieter rounds and the window trims back to 500 --
    so the ceiling is "the biggest thing this mailbox recently handed us," which
    is the number the guard needed all along.

AN ID MAY AGE OUT ONLY ONCE ITS ROUND'S CURSOR HAS COMMITTED (#161 round 3)
    Volume history alone was refuted, and by its own decay. The target comes
    down while the window is still holding the pass that raised it: one 501-id
    pass followed by ten one-id trickle rounds ages the 501 out of the history,
    the target drops back to 500, and the next ``begin_pass`` evicts eleven live
    ids. If the cursor was withheld -- ``round._settle_cursor`` keeps the old
    position on any message that could not be filed -- the provider re-offers
    those eleven at once, they are marked again, and the round after evicts them
    again. On a 600-id backlog that is a PERMANENT 100-110-ids-per-round replay
    ring, not the one-round overshoot the bound tolerates. A watcher killed
    mid-pass reaches the same ring by persisting a partial pass volume beside a
    window no cursor covers.

    The missing question was never "how big was the round" but "can the provider
    still hand this back," and only the cursor answers that. So eviction is tied
    to COMMITMENT: while a stream's cursor is withheld the window may not shrink
    below its occupancy, because those ids are exactly the re-offerable set; on
    ``record_cursor`` the flag clears and the history rule decays as above. The
    floor is self-releasing, so it is not the never-decaying high-water mark
    ``PASS_VOLUME_HISTORY`` refuses -- a stream whose rounds commit normally
    never meets it. See ``WatchState.uncommitted`` and
    ``WatchState._window_target``.

WHAT HAPPENS WHEN A REPLAY FALLS OUTSIDE THE WINDOW -- THE HONEST ANSWER
    The message is treated as new and IS REPROCESSED. There is no second line
    of defence here and this module does not pretend otherwise. Whether that
    reprocessing is harmful depends entirely on what the loop does with it: a
    re-file into the folder the message is already in is a no-op the owner
    never sees, whereas a second draft is a duplicate in the owner's Drafts
    folder and a second ledger row. The guard makes reprocessing UNLIKELY; it
    does not make the pipeline idempotent, and any action that cannot tolerate
    being repeated needs its own idempotency at its own boundary.
"""

UNCOMMITTED_WINDOW_CAP: Final = 4 * SEEN_LIMIT
"""The ceiling on the commitment floor. A stuck stream's CARRYOVER stops here (#194).

CARRYOVER, and the word is load-bearing. This bounds what a window holds at a
pass boundary, which is what ``begin_pass`` trims. It does NOT bound peak
occupancy inside a pass, because ``mark_seen`` may not evict the running pass's
own marks (#161b) and a stuck stream's pass is the whole re-offered backlog. See
``_window_target`` for the measurement.

IT BOUNDS THE FILE TOO, BUT ONLY BECAUSE THE WRITE ENFORCES IT SEPARATELY (round
6). This entry used to claim the cap was "what the state file persists" and to
scope its exception to occupancy "inside a pass" -- and the watch loop's only
save runs inside a pass, so the stated exception swallowed the stated guarantee
and the file was measured at six times this number. ``WatchState._persisted_seen``
now applies the same trim to the payload; see it for why the writer is the only
boundary the file has.

WHY A CEILING ON A FLOOR THAT IS SUPPOSED TO SELF-RELEASE
    The #161-round-3 floor releases on exactly one event: ``record_cursor``
    clearing ``uncommitted``. That is the right release for every stream whose
    rounds eventually complete, and it is NO release at all for a stream whose
    commit can never happen -- the provider refusing the cursor write with a
    4xx, a state file that went read-only after a permissions change, a round
    that aborts before ``_settle_cursor`` on every pass. Such a stream raises
    the floor on every round and never lowers it, so the persisted window grows
    for the life of the deployment: 50 rounds x 200 marks measured 10,000 ids
    against a target of 10,000. That is precisely the unbounded growth
    ``SEEN_LIMIT`` exists to refuse, reached through a different door, and until
    this constant existed it grew in silence.

WHY THE CAP IS SECOND-ORDER AND NOT THE FLOOR ITSELF
    Below this number the floor's reasoning is unchanged and is not being
    softened: while a cursor is withheld, every id in the window is one the next
    poll is offered, so evicting one guarantees the reprocessing the window
    exists to prevent, and a cap applied eagerly reopens the permanent replay
    ring round 3 closed. The trade only flips once the window is so far past the
    bound that the stream is plainly broken rather than merely behind. At that
    point the choice is between reprocessing a bounded backlog once and carrying
    an unbounded file, an unbounded parse on every round, and unbounded memory
    in an unattended process -- and the reprocessing is the recoverable one.

WHY FOUR TIMES THE BOUND
    It has to sit well clear of the legitimate uses of the floor so no healthy
    stream ever reaches it. The floor's own worst honest case is a backlog
    drained over several withheld rounds, which is the shape ``SEEN_LIMIT``'s
    volume sizing already covers at one to two times the bound; four leaves room
    for a genuinely deep backlog on a busy mailbox and still bounds the persisted
    window at 2,000 ids. It is deliberately a MULTIPLE rather than a second
    free-standing number, so retuning ``SEEN_LIMIT`` cannot silently leave a
    ceiling that no longer relates to the bound it caps.

IT HAS TO OUTRANK ``pass_volume``, WHICH IT DID NOT AT FIRST (round 5).
``_window_target`` returned ``max(target, UNCOMMITTED_WINDOW_CAP)`` with
``target`` already carrying ``max(pass_volume)``, so this constant acted as a
MINIMUM: a pass bigger than the ceiling raised the ceiling to the pass size.
Because a withheld cursor re-offers what the trim evicted, the evicted ids were
marked again, ``pass_volume`` grew, and the window grew with it -- 200 rounds
measured 20,000, ten times this number. The clamp is now ``min``, and it is keyed
on the value about to be returned rather than on the window's current length
(round 6 -- the ``occupancy > cap`` proxy let a 3,000 target through the round
after a trim), so no observed volume can outrank the ceiling; ``SEEN_LIMIT``
remains a floor beneath it.

REACHING IT IS A WARNING, ONCE PER STREAM. See ``_window_target``. The growth
was always a deliberate trade; what #194 found is that nobody could observe it,
and what round 5 found is that the constant meant to bound it did not.
"""

EXHAUSTED_LIMIT: Final = 4 * SEEN_LIMIT
"""When a stream's ABANDONED-message record is reported as a broken mailbox (#185.2).

A REPORTING THRESHOLD, NOT A CEILING, and the distinction is the round-5
correction. This number used to cap the record and evict oldest-first past it,
described as "the same trade ``SEEN_LIMIT`` documents." It was not that trade.
``SEEN_LIMIT`` forgets ids whose work is FINISHED -- filed, receipted, gone from
the inbox -- and the cost of being wrong is one message processed twice. This
record's entries name messages that are STILL IN THE INBOX and still being
offered, so forgetting one hands it a complete fresh
``round.MAX_FILING_ATTEMPTS``, a second ``retry_exhausted`` row, and four more
rounds of a pinned cursor. On a stream where filing stays broken the record
refilled and re-evicted, so that re-arm RECURRED on a period set by abandonment
volume -- one bound further out than the defect #185 point 2 closed, not a
different kind of bound. See ``WatchState.mark_exhausted``: nothing is evicted
now, and passing this threshold WARNs once per stream instead.

WHY THIS RECORD EXISTS AT ALL, SEPARATE FROM ``seen``
    ``round._exhaust`` used to record an abandonment with ``mark_seen`` alone,
    and ``mark_seen``'s first statement is ``clear_attempts``. So the abandoned
    message kept no attempts entry and its ONLY record was its slot in the
    bounded FIFO replay window -- where, being the oldest entry there by
    construction, it was the first thing the next ``begin_pass`` evicted.

    Measured on the round-level probe: 800 ordinary messages (four polls of a
    normal mailbox, or ONE page of ``SEEN_LIMIT`` + 1) pushed it out, the
    provider re-offered it, and the round handed it a COMPLETE second
    ``round.MAX_FILING_ATTEMPTS`` budget -- a second ``retry_exhausted`` ledger
    row and four more rounds of a pinned cursor. That is the exact #158 harm the
    attempt bound exists to end, recurring with a period of ``SEEN_LIMIT``
    messages for the life of the deployment.

    The window could not be made to carry it. Its bound is a REPLAY window sized
    from recent volume, and an entry that may never be evicted is precisely the
    unbounded growth ``SEEN_LIMIT`` refuses; raising the bound only moves the
    period. Abandonment is a different fact with a different lifetime, so it
    gets a record of its own.

WHY FOUR TIMES THE REPLAY BOUND
    A multiple rather than a free-standing number, for the reason
    ``UNCOMMITTED_WINDOW_CAP`` is one: retuning ``SEEN_LIMIT`` must not silently
    leave a threshold that no longer relates to it. Four is generous by a wide
    margin, because an entry here is written only when a message has been
    refused ``round.MAX_FILING_ATTEMPTS`` times -- a stream holding 2,000 of
    them has abandoned two thousand distinct messages, which is a broken
    mailbox, not a busy one. A healthy deployment never approaches it, which is
    what makes crossing it worth a WARNING rather than a routine trim.

WHAT BOUNDS THE FILE INSTEAD, now that nothing here evicts. The record is
bounded by the number of messages the mailbox contains that can never be filed,
and each costs one id. It cannot grow from traffic: a message that files spends
no entry, and a message that fails spends at most one no matter how many rounds
refuse it. An unbounded record therefore requires an unbounded supply of
permanently unfileable mail, which is a mailbox nobody can fix by forgetting
which messages it already gave up on.

PASSING IT WARNS, ONCE PER STREAM, AND CHANGES NOTHING ELSE. See
``WatchState.mark_exhausted`` and ``_warn_exhausted_capped``.
"""

PASS_VOLUME_HISTORY: Final = 10
"""How many recent pass sizes size the replay window (#161 A1).

WHY A HISTORY AND NOT JUST THE LAST PASS
    The last pass alone is too twitchy in the direction that costs money. A
    backlog does not arrive as one round: it arrives as a big round, then a
    small one while the operator is mid-catch-up, then the SAME big set again
    because the cursor was withheld on an unfiled message (#158). Sizing from
    the immediately previous pass would trim the window back to 500 during that
    one quiet round and hand the next big round exactly the replay this is
    meant to stop. Ten rounds is a wide enough memory to cover that gap and
    short enough that a genuine one-off spike is gone within a few minutes of
    polling rather than held for the life of the deployment.

WHY NOT A HIGH-WATER MARK THAT NEVER DECAYS
    Because it never comes down. A single 50,000-message import would pin the
    window at 50,000 ids forever, in the file and in memory, on every load, for
    a mailbox that went back to twenty a day the following week -- which is the
    unbounded growth ``SEEN_LIMIT`` exists to prevent, arriving by a different
    door. A sliding history is the bound: at most ten numbers per cursor, and
    the largest of them is the only thing the trim reads.
"""


def _validate_provider(provider: str) -> str:
    """Return ``provider`` if it is a known, path-safe name; otherwise raise.

    TWO SEPARATE REASONS TO REFUSE, BOTH SILENT IF LEFT UNCHECKED.

    An UNKNOWN name is rejected rather than accepted, because a typo that is
    allowed through builds its own state file, and a state file that does not
    exist yet is indistinguishable from a first run. ``load`` would return an
    empty state, the watcher would find no cursor, and it would resync the
    entire mailbox -- with no error, no warning, and a real cursor file sitting
    beside it under the correctly-spelled name, still holding the position
    nothing is reading. The legal set is ``PROVIDER_NAMES``; it is imported
    rather than restated, so a provider added there cannot be one this module
    silently refuses.

    A TRAVERSAL-SHAPED name is rejected because the filename is built from a
    variable, and that is the shape regardless of where the variable came from.
    Today every value originates in a fixed constant, so the guard fires on
    nothing; the guard belongs at the point where the path is constructed
    anyway, because that is the only place that can enforce it for every future
    caller. ``"../../etc/cron.d/x"`` composed into ``data_dir()`` escapes the
    data directory, and the containment check must not depend on the caller
    having been careful.
    """
    if os.sep in provider or (os.altsep and os.altsep in provider):
        raise ValueError(
            f"provider name must not contain a path separator: {provider!r}. "
            "ACTION REQUIRED: pass a bare provider name, not a path."
        )
    if provider in {"", ".", ".."} or Path(provider).is_absolute():
        raise ValueError(
            f"provider name is not a usable filename component: {provider!r}. "
            "ACTION REQUIRED: pass a bare provider name, not a path."
        )
    if provider not in PROVIDER_NAMES:
        raise ValueError(
            f"unknown provider: {provider!r}. "
            f"ACTION REQUIRED: use one of {', '.join(PROVIDER_NAMES)}. "
            "An unrecognised name would create its own empty state file, which "
            "is indistinguishable from a first run and would resync the whole "
            "mailbox with no error."
        )
    return provider


def state_path(provider: str) -> Path:
    """Where this provider's watch state file lives, under the data directory.

    No new location is invented and no directory parameter is offered. The data
    directory is already the one place this connector writes durable state,
    and it is already redirectable by the operator (and by the test suite)
    through ``INBOX_ZERO_DATA_DIR``. A per-call override would let two callers
    disagree about where the cursor is, which is how a watcher silently
    restarts from nothing.

    The provider IS required, and it is validated before it reaches the
    filename -- see ``_validate_provider`` for both reasons. The result is
    asserted to be a direct child of the data directory, so containment is a
    property of the returned path rather than of the caller's discipline.
    """
    directory = data_dir()
    target = directory / STATE_FILENAME_TEMPLATE.format(provider=_validate_provider(provider))
    if target.parent != directory:
        raise ValueError(f"provider name escapes the data directory: {provider!r}")
    return target


def _now() -> str:
    """``WatchCursor.stored_at``'s default: UTC, ISO-8601, whole seconds.

    UTC because a cursor file outlives the process that wrote it and may be
    read on a machine in another zone, and second precision because these
    stamps exist to be read by an operator asking "when did the watcher last
    get anywhere?" -- nothing here compares them at finer resolution.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def _store_key(container_id: str | None) -> str:
    """The dict key for a cursor, mapping ``None`` onto a non-colliding key.

    See ``MAILBOX_WIDE_KEY`` for why ``None`` gets a key of its own rather
    than an empty string.
    """
    return MAILBOX_WIDE_KEY if container_id is None else container_id


def _fresh_start_line(cause: str, target: Path) -> str:
    """The one WARNING every fresh-start door emits, naming ALL of what was lost.

    ONE BUILDER FOR THREE DOORS, because they discard identical state. ``load``
    starts fresh on bytes that will not open, bytes that are not JSON, bytes that
    are not an object, and bytes whose shape the model refuses -- and every one
    of those returns ``cls()``, which is every cursor, every replay window, and
    every retry budget gone. A line per door drifts, and a diagnosis that depends
    on which way the file happened to break is not a diagnosis.

    IT NAMES THE ATTEMPT BUDGET BECAUSE THE RESET RE-ARMS IT (#185). The
    fresh-start recovery discards ``attempts`` along with ``seen``, so a message
    that had already spent its whole budget and been abandoned is offered again
    at attempt 0 -- another bounded cursor-pin cycle, and repeated corruption
    means repeated cycles. That re-arm is the correct behaviour and not the
    defect: the counts live inside the object being thrown away, so preserving
    them would mean salvaging numbers out of a file already judged
    untrustworthy, and carrying a stale bound the other way abandons mail that
    may now file, on the strength of a count from a corrupt file. The defect was
    that it happened SILENTLY -- an operator watching a second pin cycle had
    nothing connecting it to the reset that caused it.

    The line says what follows rather than only what was discarded, because a
    fact with no reading is not actionable: the retry is bounded, it is
    receipted, and no mail is lost.

    The path is named and nothing else is. A cursor token, a message id, and a
    container id are all absent by construction -- this line goes in a log an
    operator reads, and the state it is announcing is exactly the state that
    could not be trusted to be quoted.
    """
    return (
        f"WATCH: {cause}, starting fresh path={target} "
        "(cursors, the replay window, and every message's attempt budget are "
        "reset together -- an already-abandoned message may be retried for one "
        "more bounded cycle; the retry is receipted and no mail is lost)"
    )


def _announce_exhausted_introduced(target: Path) -> None:
    """Say ONCE, on load, that this file predates the abandonment record (#185.2 r5).

    THE ONE-TIME RE-ARM THIS LINE EXISTS TO MAKE VISIBLE. A state file written
    before ``exhausted`` existed carries no verdicts, and the abandoned message
    it does hold is sitting in the replay window as that window's OLDEST entry
    -- so the first ``SEEN_LIMIT`` ordinary messages evict it and the re-offer
    draws a complete fresh ``round.MAX_FILING_ATTEMPTS``. That is the #185.2
    harm exactly, arriving once per upgraded deployment through the one door the
    fix could not close retroactively, and the reason it must not be silent is
    the reason the fresh-start line exists: an operator watching a pin cycle
    needs something connecting it to the upgrade that caused it.

    IT STATES WHAT IS TRUE AND REFUSES TO GUESS. The line says the record is
    being introduced and that a message abandoned before the upgrade may be
    retried for one more bounded cycle. It does NOT name a message or a count,
    because there is nothing honest to name: the ids in ``seen`` are
    indistinguishable between "filed successfully" and "given up on" -- that
    collapse is the whole defect ``exhausted`` was added to end -- so any
    verdict manufactured from the window would be a guess wearing a receipt.
    Back-filling one is worse than the re-arm: it would abandon mail that files
    perfectly well, permanently and with no way for an operator to see why.

    INFO rather than WARNING. Nothing is wrong, nothing was lost, and the
    condition is a normal consequence of upgrading; a WARNING here trains an
    operator to skip the level the real breakage uses.

    ONCE BECAUSE THE FILE IS ONLY UPGRADED ONCE. The next ``save`` writes the
    key, so the next ``load`` takes the ordinary path with no line at all.
    """
    logger.info(
        f"WATCH: abandoned-message record introduced on an older state file path={target} "
        "(this file predates the record, so nothing in it says which messages were "
        "already given up on; a message abandoned before this upgrade may be retried "
        "for one more bounded cycle, after which the new record makes the verdict "
        "permanent -- the retry is receipted and no mail is lost)"
    )


Count = Annotated[int, Field(ge=0, strict=True)]
"""A count this module writes: never negative, and never a string that looks like one.

WHY THE CONSTRAINT AND NOT JUST THE TYPE (#188)
    A bare ``int`` annotation makes pydantic's job naming a type, and its
    ordinary coercion then turns a suspect file into a working object:
    ``pass_volume {'c1': ['7']}`` loads as ``[7]``, ``generations -3`` loads as
    ``-3``, ``attempts -5`` loads as ``-5``. Nothing crashes, which is the
    problem -- the watcher runs on numbers no correct write in this module can
    produce, and the one with teeth extends a bound rather than breaking it:
    ``round._count_attempt`` compares the running total against
    ``MAX_FILING_ATTEMPTS``, so an attempts entry of -5 buys a permanently
    unfileable message ten rounds of cursor pinning instead of five.

WHY ``ge=0`` AND NOT ``gt=0``
    Every counter here has a real zero. ``_record_quiet_streams`` files a
    literal 0 for a stream that delivered nothing -- the observation that lets a
    volume spike age out -- a never-resynced stream reports generation 0, and
    ``cursor_generation`` 0 is the load-bearing pre-stamp reading. Excluding
    zero would refuse the connector's own honest output.

WHY ``strict`` AS WELL
    Non-strict, ``"7"`` satisfies ``ge=0`` after coercion, so the constraint
    would catch the negative values and wave through the wrong TYPE -- and a
    stringified count is the signature of a writer that is not this module,
    which is the provenance most worth refusing.

WHY REJECT RATHER THAN CLAMP
    A -5 is not a number to repair. Nothing knows what it should have been, so a
    clamp to 0 invents a budget just as unfounded as the -5 and hides that the
    file was wrong. Rejecting routes it to ``load``'s existing
    ``ValidationError`` path: one WARNING, fresh state, full resync -- the
    recovery this module already treats as first-class.
"""

Token = Annotated[str, Field(min_length=1, strict=True)]
"""A provider resume token: opaque, and never the empty string.

``record_cursor``'s own contract is that a cursor is REMOVED rather than blanked
-- ``resync_required`` pops it precisely so ``cursor_for`` reports the honest
"no cursor" instead of an empty string a caller might replay as a token. With no
``min_length`` a state file carrying ``token=""`` loaded and was PRESERVED,
reintroducing from disk the blanked cursor no code path will create (#188). The
model is the only thing positioned to refuse it.

The token stays otherwise UNVALIDATED and is not parsed, normalized, or
compared: only the adapter that minted it may interpret it. Length 1 is the
weakest possible check that still enforces the removal contract.
"""


MessageId = Annotated[str, Field(min_length=1, strict=True)]
"""A provider message id this module records: a real string, never empty.

Constrained for the reason ``Count`` and ``Token`` are (#188). Non-strict, a
state file carrying ``exhausted {'c1': [7]}`` would load as ``['7']`` and the
abandonment would then be filed under an id no provider will ever offer -- the
record present, the guard silently inert, and nothing to read on the way past.
An empty string is refused on the same footing: no provider mints one, so its
presence is evidence the file was not written here, and a zero-length entry
could only ever match a message that does not exist.

The id itself is OPAQUE, exactly as ``Token`` is. It is compared for equality
and nothing else -- never parsed, normalized, or ordered.
"""


class WatchCursor(BaseModel):
    """One resumable position in a provider's change stream.

    ``token`` is stored and replayed VERBATIM. It is OPAQUE: shared code never
    parses it, inspects it, validates its shape, compares it for ordering, or
    constructs one. Only the adapter that produced it may interpret it.
    Treating it as a structured value to be edited is the specific mistake that
    turns a working watcher into one that silently re-reads the wrong data --
    Microsoft's delta link already encodes the original query parameters
    (``$select``, ``changeType``), so a "helpfully" normalized link changes the
    field set of the next round with no error anywhere.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: Token
    """The provider's opaque resume token, replayed byte for byte.

    Microsoft Graph stores its whole ``@odata.deltaLink`` URL here. Gmail
    stores its ``historyId`` as a string. Neither is parsed, inspected, or
    constructed by this module, and a caller that reads its contents has left
    the contract.
    """

    container_id: str | None = None
    """Which container this cursor tracks, or ``None`` for the whole mailbox.

    Gmail stores exactly ONE cursor with ``None``: ``history.list`` is
    mailbox-wide and returns a single ``historyId`` for the entire mailbox, so
    there is no container to name. Microsoft Graph stores one cursor per mail
    folder with the folder's id, because Graph has no mailbox-wide delta at all
    -- ``/me/messages/delta`` does not exist, so covering a mailbox costs N
    streams and N independently-expiring tokens.

    THE DIFFERENCE IS STRUCTURAL, NOT INCIDENTAL. It is not a configuration
    choice, a performance tuning, or an artifact of how either adapter happens
    to be written today: it follows from what each provider's API offers, and
    neither can be made to look like the other. That is why this field is
    ``Optional`` rather than a required string with a stand-in value -- forcing
    Gmail to invent a container id would make a fiction indistinguishable from
    a real folder, and forcing Graph to use ``None`` would collapse N distinct
    streams into one slot.
    """

    stored_at: str = Field(default_factory=_now)
    generation: Count = 0
    """Bumped every time this cursor is forced back to a full resync.

    Monotonic per cursor and never reset. It exists so a downstream consumer
    can tell "the cursor moved forward" from "the cursor was thrown away and
    rebuilt" -- two states that look identical if you only compare tokens,
    because the new token after a resync is just as opaque as the old one. An
    operator reading a rising generation on a quiet mailbox is seeing ordinary
    token expiry, which is expected, not a bug to chase.
    """

    cursor_generation: Count = 0
    """Which build of the connector minted this token. See ``CURSOR_GENERATION``.

    A SEPARATE COUNTER FROM ``generation`` ABOVE, because the two answer
    different questions and one cannot stand in for the other. ``generation``
    counts how many times THIS STREAM was thrown back to a full resync, moves
    per stream, and is a fact about the MAILBOX. This one records which CODE
    wrote the token, is identical across every stream a build touches, and is a
    fact about the CONNECTOR. Folding them together would make "the token is
    from a broken build" indistinguishable from "this folder's token expired
    twice," and only one of those may be replayed.

    ``0`` IS THE PRE-FIX VALUE AND IT IS LOAD-BEARING. A state file written
    before this field existed carries no key for it, so pydantic supplies the
    default -- which is the answer that is true: those cursors came from a build
    that did not stamp, and every one of them predates the fix. Nothing has to
    detect a missing key, because the absence IS the reading.
    """

    def to_json(self) -> dict[str, object]:
        """This cursor as the plain dict written into the state file."""
        return self.model_dump(mode="json")


class WatchState(BaseModel):
    """Watch cursors, replay guards, and retry budgets, as one JSON file.

    One file holds every cursor because the watcher's cursors are read and
    written together on each round; splitting them into a file each would buy
    nothing and would multiply the number of writes that can be interrupted.
    A mailbox-wide deployment simply stores one cursor in the same map.

    FOUR MAPS, ALL KEYED THE SAME WAY and all four durable. ``cursors`` says
    where the stream got to, ``seen`` says what must not be processed again,
    ``attempts`` says how much retry budget an unfiled message has left, and
    ``exhausted`` says which messages the connector has stopped trying to file.
    The third is what bounds the second's interaction with the first: without
    it a message that can never be filed withholds the cursor forever (#158
    round 2). The fourth is what keeps the third's verdict once the second
    forgets: ``seen`` is a bounded replay window that evicts, and an abandoned
    id was always its oldest entry, so the abandonment aged out and the budget
    re-armed (#185 point 2). ``_pass_marks`` is the one piece of state here that
    is NOT persisted, and its docstring says why.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Count = SCHEMA_VERSION
    cursors: dict[str, WatchCursor] = Field(default_factory=dict)
    generations: dict[str, Count] = Field(default_factory=dict)
    """Resync generation per cursor, held OUTSIDE the cursor it describes.

    It cannot live only on ``WatchCursor``, because ``resync_required``
    deletes that cursor -- the counter would be destroyed by the very event it
    exists to record. It is a serialized field and not a pydantic
    ``PrivateAttr`` for the same reason: private attributes are not written to
    the file, so the count would reset on every watcher restart, which is
    exactly when an operator wants to read it.
    """

    seen: dict[str, list[str]] = Field(default_factory=dict)
    """Recently-processed message ids per cursor, oldest first.

    A list rather than a set because order IS the eviction policy: the bound
    is enforced by dropping from the front, and a set has no front.
    """

    pass_volume: dict[str, list[Count]] = Field(default_factory=dict)
    """The last ``PASS_VOLUME_HISTORY`` pass sizes per cursor, oldest first.

    THIS IS WHAT SIZES THE TRIM TARGET (#161 A1). ``begin_pass`` keeps at least
    ``max(SEEN_LIMIT, max(pass_volume[key]))`` ids, so a round can never evict
    its own marks before the next poll is offered them. See ``SEEN_LIMIT``.

    PERSISTED, and for the same reason ``attempts`` is. The subject is a
    MAILBOX -- "how much does this stream deliver in a round" -- not a process.
    A ``PrivateAttr`` would reset the figure on every restart, the first
    ``begin_pass`` after it would trim a 550-id window back to 500, and the
    round that follows would replay the page: the defect, surviving a restart,
    which is precisely the shape #158 found for the retry budget.

    AN ABSENT ENTRY IS THE HONEST PRE-FIX READING. A state file written before
    this field existed has no volume history, so the default applies and the
    trim target is the flat ``SEEN_LIMIT`` those cursors were already living
    with. The first pass after the upgrade records its own size and the window
    is correct from then on -- no migration, and nothing to detect.

    Bounded by construction: at most ``PASS_VOLUME_HISTORY`` integers per
    cursor, and a cursor with no traffic records nothing.
    """

    attempts: dict[str, dict[str, Count]] = Field(default_factory=dict)
    """Filing attempts spent per message id, per cursor. Mid-retry ids only.

    THE BOUND ON A WITHHELD CURSOR (#158 round 2). ``round._settle_cursor``
    keeps the old position whenever a message could not be filed, so the next
    round is offered that message again. With no ceiling on the retry, a
    message that can NEVER be filed pins the cursor for the life of the
    deployment: every round re-reads the whole window, the mailbox keeps
    growing past it, and once the growth exceeds ``SEEN_LIMIT``'s carryover the
    already-filed messages beyond the window are re-filed -- unbounded provider
    writes and unbounded ledger rows (observed applied-per-round 0, 300, 900,
    1500). This map is what lets the round say "five attempts, enough."

    PERSISTED, AND FOR THE SAME REASON THE CURSOR IS. The count is a fact about
    a MESSAGE, not about a process, so a watcher restarted between rounds must
    inherit it. A ``PrivateAttr`` would reset the budget on every restart, and a
    permanently unfileable message would pin the cursor forever across restarts
    exactly as it did with no bound at all -- which is the defect, wearing a
    ceiling. This is the OPPOSITE of ``_pass_marks`` below, whose subject is the
    running pass and is therefore meaningless after a restart.

    ONLY MESSAGES CURRENTLY MID-RETRY HAVE A ROW. The entry is pruned the
    moment the message is resolved -- filed successfully, or marked seen because
    its budget ran out -- so this map is bounded by the number of messages a
    round is failing on, never by the mailbox. See ``clear_attempts`` and
    ``mark_seen``.
    """

    exhausted: dict[str, list[MessageId]] = Field(default_factory=dict)
    """Message ids ABANDONED at the attempt bound, per cursor, oldest first (#185.2).

    THE RECORD THAT OUTLIVES THE REPLAY WINDOW, and it exists because the window
    could not be it. ``round._exhaust`` marks an abandoned message seen and
    ``mark_seen`` prunes its attempts row, so before this field the abandonment
    survived only as the OLDEST entry in a bounded FIFO -- evicted after
    ``SEEN_LIMIT`` ordinary messages, at which point a re-offer was
    indistinguishable from a message never processed and drew a full fresh
    budget. See ``EXHAUSTED_LIMIT`` for the measurement.

    CONSULTED BEFORE CLASSIFICATION, by ``round._process_message``, which is the
    only reader. An id in here is skipped exactly as a replayed one is: not
    classified, not filed, not receipted again. The mail stays in the inbox with
    its ``retry_exhausted`` row, which is the end state ``round._exhaust``
    already documents.

    PERSISTED, and for the reason ``attempts`` is. "This message was abandoned"
    is a fact about a MESSAGE, not about a process; a ``PrivateAttr`` would hand
    every restart a clean slate and reopen the re-arm through a quieter door
    than eviction. An absent MAP is a pre-fix state file and is announced once
    on load rather than read silently as "nothing abandoned here" -- see
    ``load``, which owns that ruling and the one bounded cycle it costs.

    KEYED PER STREAM, READ PROVIDER-WIDE (#185.2 round 5). The key stays the
    store key because eviction is per stream and must remain so: a Graph mailbox
    with one broken folder must not be able to push another folder's
    abandonments out of the record. But the QUESTION ``message_exhausted``
    answers is about a MESSAGE, and on Graph one message is offered by every
    folder stream that holds it -- so a verdict readable only under the stream
    that wrote it let a sibling folder spend a whole fresh
    ``round.MAX_FILING_ATTEMPTS`` on the same provider message id, with no
    eviction and no corruption involved at all. The read is therefore
    provider-wide (see ``_exhausted_ids``) while the write stays per stream.

    A LIST rather than a set because order IS the eviction policy past
    ``EXHAUSTED_LIMIT`` -- the same reasoning ``seen`` is a list for -- and
    ``MessageId`` rather than a bare ``str`` for the reason every other field
    here is constrained: a stringified or empty id is the signature of a writer
    that is not this module (#188).
    """

    uncommitted: dict[str, bool] = Field(default_factory=dict)
    """Cursors that have marked ids the stored cursor does not yet cover (#161 r3).

    THE FLAG THAT TURNS THE TRIM FROM A GUESS INTO A FACT. Pass history says how
    much this stream RECENTLY delivered; it says nothing about whether the
    provider is about to deliver it AGAIN. Only the cursor knows that, because
    the cursor is the sole thing that decides what the next poll is offered:
    while it is withheld -- ``round._settle_cursor`` keeps the old position on
    any message that could not be filed -- the ids marked since it last moved
    are EXACTLY the set coming back, so evicting one guarantees the reprocessing
    the window exists to prevent. ``_window_target`` therefore floors at current
    occupancy while this is set, and lets the history rule decay once it clears.

    ``mark_seen`` sets it; ``record_cursor`` clears it. That pairing is what
    makes the floor self-releasing rather than a high-water mark: a stream whose
    rounds commit normally clears the flag every round and never sees the floor
    at all.

    PERSISTED, because the question it answers outlives the process. A watcher
    killed mid-backlog checkpoints a PARTIAL ``pass_volume`` beside a window of
    ids no cursor covers; a ``PrivateAttr`` would come back False, the partial
    figure would age out of the history over a few trickle rounds, and the trim
    would evict ids the provider is still going to re-offer -- the same ring,
    entered through a crash instead of through a withheld cursor.

    Bounded by the cursor count, and an absent entry reads False -- which is the
    honest answer for a pre-fix state file, whose ids were written by a build
    that had no way to be re-offered them beyond the flat bound anyway.
    """

    _volume_slots: set[str] = PrivateAttr(default_factory=set)
    """Cursors whose CURRENT pass already has a slot open in ``pass_volume``.

    Bookkeeping for one running pass and nothing else, so it is private for the
    same reason ``_pass_marks`` is: after a restart no pass is running, and a
    surviving slot marker would make the next mark bump a DEAD pass's count
    instead of opening a new one -- inflating a stale figure and never
    recording the live round at all.
    """

    _capped_windows: set[str] = PrivateAttr(default_factory=set)
    """Streams already warned about hitting ``UNCOMMITTED_WINDOW_CAP`` (#194).

    A once-per-stream latch for the cap warning and nothing else. It holds no
    state any decision reads -- ``_window_target`` caps whether or not the line
    has been emitted -- so losing it costs one repeated log line, never a
    behaviour change.

    Private, and therefore reset on restart, DELIBERATELY. See
    ``_warn_window_capped``: the condition the line reports is one that is still
    true in the new process, so a fresh watcher is entitled to say it again.
    """

    _capped_exhausted: set[str] = PrivateAttr(default_factory=set)
    """Streams already warned about passing ``EXHAUSTED_LIMIT`` (#185.2).

    A once-per-stream latch, per ``_capped_windows``' reasoning and for the same
    two reasons: ``mark_exhausted`` runs per abandoned message, so a line per
    call buries the fact it exists to raise, and Graph runs a stream per folder,
    so a per-process latch would report the first broken folder and hide the
    rest. Private, so a fresh process is entitled to say it again -- the
    condition it reports is still true in the new process.
    """

    _announced_skips: set[tuple[str, str]] = PrivateAttr(default_factory=set)
    """``(store key, message id)`` pairs whose abandoned-skip line was already said.

    THE LATCH BEHIND ``round._process_message``'s DEBUG SKIP LINE. Without it
    the line repeats for the same message on every round for the life of the
    deployment -- a permanently abandoned message is offered by every poll --
    and a fact repeated a thousand times a day is noise an operator filters out,
    which defeats the reason it is logged at all.

    KEYED BY STREAM AND ID, not by id alone. Graph offers one message from every
    folder that holds it, and each of those streams reaching the skip is a
    separate thing an operator may need to see; collapsing them would report the
    first folder and hide the rest, which is the mistake ``_capped_exhausted``
    documents for its own key.

    Private, and therefore reset on restart, DELIBERATELY -- per
    ``_capped_windows``. The condition is still true in the new process, so a
    fresh watcher is entitled to say it once more. Bounded by the abandoned set,
    which ``EXHAUSTED_LIMIT`` already speaks to.
    """

    _exhausted_index: set[str] | None = PrivateAttr(default=None)
    """The provider-wide union of ``exhausted``, or ``None`` when not yet built.

    A DERIVED CACHE, never a second record. ``exhausted`` is the fact; this is
    the answer ``message_exhausted`` needs from it, which is membership across
    every stream rather than within one. See ``_exhausted_ids``.

    ``None`` rather than an empty set as the unbuilt marker, because an empty
    set is a legitimate BUILT value -- a provider that has abandoned nothing --
    and a marker that collides with a real state rebuilds on every read for
    exactly the mailboxes that are healthy.

    Private, so it is absent after ``load`` and rebuilt from the file that was
    just read. Persisting it would let a stale union outlive the record it
    summarises, which is the one way a cache of a bounded map can be wrong.
    """

    _pass_marks: dict[str, set[str]] = PrivateAttr(default_factory=dict)
    """Ids marked during the CURRENT pass, per cursor. Never persisted.

    THE INVARIANT THIS FIELD ENFORCES: eviction may drop CARRYOVER from earlier
    passes and may never drop an id this pass recorded (#161b -- see
    ``SEEN_LIMIT``). It is a set because the only question asked of it is
    membership; the ORDER that drives eviction lives in ``seen``, which is why
    that one is a list and this one is not.

    A ``PrivateAttr`` deliberately, and the reasoning is the OPPOSITE of
    ``generations`` above. "The current pass" is a fact about a process that is
    running, and no process is running when the file is read: a restart is
    always the start of a new pass, so persisting this would let a dead pass's
    marks protect ids from eviction forever and turn the bound into a leak. An
    empty map after ``load`` is the honest state.
    """

    # -- cursors ---------------------------------------------------------

    def cursor_for(self, container_id: str | None) -> WatchCursor | None:
        """This container's stored cursor, or ``None`` if there is not one.

        ``container_id=None`` asks for the mailbox-wide cursor, which is a real
        lookup and not a wildcard: it returns the cursor stored under
        ``MAILBOX_WIDE_KEY`` and never a per-container one.

        Returning ``None`` for an unknown container is the correct answer, not
        an error worth raising. "No cursor" is the state of every stream before
        its first successful round and of every stream just after a resync,
        and both are ordinary. A raise here would force each caller to wrap
        the first poll of every stream in a handler, which is how the genuine
        errors stop being noticed.
        """
        return self.cursors.get(_store_key(container_id))

    def record_cursor(self, container_id: str | None, token: str) -> WatchCursor:
        """Store the opaque token that ended a COMPLETED round.

        Called ONLY when the provider signalled that the round is complete --
        Graph returning an ``@odata.deltaLink`` rather than an
        ``@odata.nextLink``, Gmail returning a page with no ``nextPageToken``.
        Storing a cursor while pages remain would mark a partial read as a
        finished one and lose every change on the pages not yet fetched.

        The generation carries over from the existing cursor. Advancing within
        the same sync run is not a resync, and the counter must only move for
        the reason it was created.

        ``cursor_generation`` is stamped from the CURRENT build rather than
        carried, because it records who wrote THIS token and this token is
        being written now. Carrying it forward would let a cursor minted by
        corrected code inherit a broken build's mark and be dropped for a
        defect it does not have.

        STORING THE TOKEN IS ALSO WHAT RELEASES THE REPLAY WINDOW (#161 r3).
        Until this runs, every id marked since the last commit is one the
        provider will offer again, so the window may not shrink below them --
        see ``uncommitted`` and ``_window_target``. A completed round is the
        only event that makes those ids ordinary carryover, and it is this
        method, so the release belongs here and nowhere else.
        """
        key = _store_key(container_id)
        self.uncommitted.pop(key, None)
        cursor = WatchCursor(
            container_id=container_id,
            token=token,
            generation=self.generations.get(key, 0),
            cursor_generation=CURSOR_GENERATION,
        )
        self.cursors[key] = cursor
        return cursor

    def resync_required(self, container_id: str | None) -> int:
        """Drop this cursor and bump its generation. Returns the new one.

        THIS IS NOT AN ERROR PATH. It is the ordinary handler for the provider
        answering a poll with its own "your token is no longer valid, start
        over with a full synchronization" signal. WHICH signal that is belongs
        to the adapter and is deliberately not named here: it differs per
        provider, the concept ports and the status code does not, and shared
        code that hardcodes one provider's code silently stops recognising the
        other's. The adapter detects it and calls this method.

        On both providers this is expected operation, not failure. Resume
        tokens have no lifetime guarantee shared code may rely on, so a watcher
        that treats expiry as an exception will raise on a perfectly healthy
        mailbox.

        The caller's correct response is to run a full round with no token and
        store the fresh one it returns. The cursor is REMOVED rather than
        blanked, so ``cursor_for`` reports the honest state (no cursor) instead
        of an empty string a caller might replay as a token.

        The seen-ids window is deliberately KEPT across a resync. A full
        resync re-reads everything the stream covers, so it is precisely the
        moment the replay guard earns its keep -- clearing it would guarantee
        the reprocessing it exists to prevent.
        """
        key = _store_key(container_id)
        self.cursors.pop(key, None)
        generation = self.generations.get(key, 0) + 1
        self.generations[key] = generation
        scope = "mailbox-wide" if container_id is None else f"container_id={container_id}"
        logger.info(
            f"WATCH: resync required {scope} generation={generation} "
            "(expected operation -- resume tokens have no guaranteed lifetime)"
        )
        return generation

    def generation_for(self, container_id: str | None) -> int:
        """How many times this cursor has been forced to resync.

        Read from ``generations``, never from the cursor, because the counter
        must survive the cursor being dropped -- that deletion is the very
        event it records. A stream that has never resynced reports 0.
        """
        return self.generations.get(_store_key(container_id), 0)

    # -- replay guard ----------------------------------------------------

    def seen_message(self, container_id: str | None, message_id: str) -> bool:
        """Whether this message id is inside this cursor's replay window.

        ``True`` means "already processed, skip it." ``False`` means "process
        it" -- and, per ``SEEN_LIMIT``, also covers the case of a genuine
        replay that aged out of the window, which is reported as new because
        this module has no way to tell the two apart.
        """
        return message_id in self.seen.get(_store_key(container_id), [])

    def begin_pass(self) -> None:
        """Declare a new polling pass, trimming every window back to the bound.

        THE PASS BOUNDARY THE EVICTION RULE IS DEFINED AGAINST (#161b). Marks
        made after this call are protected from that pass's own overflow; marks
        made before it become carryover and are what eviction is allowed to
        drop. Trimming HERE rather than at the end of the previous pass is what
        makes the rule hold across a crash: a pass that died mid-fan-out never
        ran its own cleanup, and the next process to poll does it instead.

        THE TRIM TARGET IS THE VOLUME THE MAILBOX ACTUALLY DELIVERED (#161 A1).
        The finishing pass's size is recorded first, and the window is then cut
        to ``max(SEEN_LIMIT, largest recent pass)`` rather than to a flat 500.
        Trimming to the flat bound is what let a 550-id round drop 50 of its own
        marks the instant the NEXT round started and replay the page in full --
        see ``SEEN_LIMIT`` and ``pass_volume``.

        THE TRIM ALSO WAITS FOR THE CURSOR TO COMMIT (#161 round 3). Volume
        history sizes the target but cannot say whether the provider is about to
        hand the same ids back, and a target that decays while the window still
        holds an UNCOMMITTED pass evicts exactly the ids the next poll re-offers
        -- a permanent replay ring rather than a one-round overshoot. So this
        call still trims every window, and ``_window_target`` refuses to size
        one below its occupancy while ``uncommitted`` is set for it. Nothing
        moved out of ``begin_pass``: the pass boundary is still the only moment
        that sees a crashed predecessor's leftovers, which is why the trim is
        here at all.

        Idempotent and cheap. Calling it without any marks having been made is a
        no-op beyond the trim -- the volume history is left alone, so a repeated
        call cannot age a real spike out of it -- and calling it twice simply
        starts two passes of which the second is the one in force.

        The loop calls this once per pass (``watch.loop.run_pass``). A caller
        that never calls it gets one pass that lasts the life of the object,
        which is the correct degradation for a one-shot round in a test: no
        eviction of the current pass's own marks, and the bound reasserted the
        moment any caller declares a boundary.
        """
        self._record_quiet_streams()
        for key, window in self.seen.items():
            target = self._window_target(key)
            overflow = len(window) - target
            if overflow > 0:
                del window[:overflow]
                logger.debug(
                    f"WATCH: replay window trimmed to the bound key={key} "
                    f"dropped={overflow} limit={target}"
                )
        self._pass_marks.clear()
        self._volume_slots.clear()

    def _record_quiet_streams(self) -> None:
        """File a 0 for every stream that had a window but delivered nothing.

        The BUSY streams count themselves as they go -- ``_count_toward_volume``
        does it on the first mark and on every mark after -- because the figure
        has to be durable the instant it is true. A watcher that crashes or is
        restarted mid-backlog would otherwise save a 600-id window with no
        record of the 600-id pass that filled it, and the first ``begin_pass``
        after the restart would trim it straight back to 500 and replay the
        page: the defect, surviving a restart, which is exactly the shape #158
        found for the retry budget.

        The quiet ones are counted HERE instead, at the boundary, because "this
        stream delivered nothing" is only knowable once the pass is over. It is
        the observation that lets a spike age out: without it, a container that
        went quiet after one big round would stay pinned at that round's size
        for the life of the deployment.

        A pass that marked NOTHING ANYWHERE is not recorded at all. That is the
        signature of a caller that never opened a pass -- a second
        ``begin_pass`` in a row, or a one-shot object in a test -- and counting
        it as a genuine quiet round would let repeated calls flush a real spike
        out of the history without a single poll having happened.
        """
        if not self._pass_marks:
            return
        for key in self.seen:
            if key not in self._volume_slots:
                self._append_volume(key, 0)

    def _count_toward_volume(self, key: str) -> None:
        """Add this mark to the pass's running size for ``key``.

        Opens the pass's slot on the first mark and bumps it on every one after,
        so the persisted figure is never behind the window it has to protect.
        """
        if key in self._volume_slots:
            self.pass_volume[key][-1] += 1
            return
        self._append_volume(key, 1)
        self._volume_slots.add(key)

    def _append_volume(self, key: str, size: int) -> None:
        """Push one pass size onto ``key``'s history, dropping the oldest past the bound."""
        history = self.pass_volume.setdefault(key, [])
        history.append(size)
        del history[:-PASS_VOLUME_HISTORY]

    def _window_target(self, key: str) -> int:
        """How much CARRYOVER this cursor may keep: the bound, or bigger.

        ``SEEN_LIMIT`` is a FLOOR. A stream that recently handed the watcher
        more than that in one pass keeps enough room for its largest recent
        pass, because anything less evicts a round's own marks before the next
        poll is offered them (#161 A1).

        THE HISTORY IS SPREAD INTO A LIST, NOT INTO ``max``'s ARGUMENTS (#194
        round 6). ``max(SEEN_LIMIT, *history)`` reads the same and is not the
        same: on an EMPTY history it collapses to ``max(500)``, and ``max`` of a
        single non-iterable argument raises ``TypeError``, which takes down
        ``begin_pass`` -- the first statement of every round -- on a state file
        that ``load`` accepts as valid, since ``list[Count]`` permits ``[]``. The
        old ``[0]`` default covered a MISSING key only, which is a different
        shape. ``max([SEEN_LIMIT, *history])`` has one iterable argument that is
        never empty, so an empty history simply yields the floor, which is the
        same answer a missing key gives and the right one: no observed pass means
        no reason to keep more than the bound. Nothing in this module writes an
        empty list -- ``_append_volume`` appends before it trims -- so the shape
        arrives only from a hand-edited or foreign-written file, which is exactly
        the input that must not stop the watcher.

        The figure is measured against CARRYOVER, not against the whole window,
        because the running pass's marks are exempt from eviction anyway: a
        window holding 550 carryover from the big pass plus one new mark is 551
        ids, and trimming that back to 550 would drop the big pass's OLDEST id
        -- the same one-off-the-front replay, one id at a time. Total occupancy
        is therefore target plus the running pass, which is the overshoot
        ``SEEN_LIMIT`` already documents, reasserted at the next
        ``begin_pass``.

        AND WHILE THE CURSOR IS WITHHELD THE TARGET IS ALSO FLOORED AT CURRENT
        OCCUPANCY (#161 round 3). History sizing alone was refuted: the target
        DECAYS while the window still holds the pass that raised it. One 501-id
        pass followed by ten one-id trickle rounds ages the 501 out of
        ``PASS_VOLUME_HISTORY``, the target falls back to ``SEEN_LIMIT``, and
        the next ``begin_pass`` evicts eleven live ids -- which a withheld
        cursor re-offers at once, so they are marked again and evicted again on
        the round after. That is a permanent replay ring, not a one-off
        overshoot: 100-110 ids per round, forever, on a 600-id backlog.

        The floor closes it because it asks the only question that decides
        whether an id can come back. An id may age out once the stream's cursor
        has COMMITTED past the round that produced it, and not before; while
        ``uncommitted`` is set, every id in the window is one the next poll is
        offered, so shrinking below ``len(window)`` guarantees the reprocessing.
        On commit the flag clears and the history rule resumes in full, which is
        what keeps this a self-releasing floor rather than the never-decaying
        high-water mark ``PASS_VOLUME_HISTORY`` exists to refuse.

        It closes the mid-pass crash by the same reading. ``mark_seen``
        checkpoints volume as it marks, so a watcher killed mid-backlog persists
        a PARTIAL figure beside a window no cursor covers -- a number that never
        described a completed round and must not authorise a trim. The flag is
        persisted with it and says so.

        AND THE FLOOR ITSELF IS CLAMPED TO ``UNCOMMITTED_WINDOW_CAP`` (#194). The
        floor is self-releasing only if ``record_cursor`` EVENTUALLY runs, and a
        stream whose commit can never happen never releases it: 50 rounds x 200
        marks with no commit measured a window of 10,000 against a target of
        10,000. Past the cap the trade flips -- the stream is broken rather than
        behind, and an unbounded file beats nothing. Below the cap NOTHING here
        changes; see ``UNCOMMITTED_WINDOW_CAP`` for why that boundary is where
        it is.

        THE CLAMP IS ``min``, AND IT USED TO BE ``max`` -- ROUND 5. Returning
        ``max(target, UNCOMMITTED_WINDOW_CAP)`` keeps the LARGER of the two, and
        ``target`` above already carries ``max(pass_volume)``, so any pass bigger
        than the ceiling raised the ceiling to the pass size. The constant was a
        minimum and never a maximum, and a withheld cursor is exactly the state
        that grows a pass: the provider re-offers what the trim evicted, those ids
        are marked AGAIN, that inflates ``pass_volume``, and the bigger volume
        permits a bigger window on the next round. Measured at HEAD before this
        line changed: 200 re-offering rounds settled at 20,000 against a cap of
        2,000, growing O(backlog) rather than O(cap), with ONE warning across the
        whole run.

        So the return while uncommitted is
        ``min(max(target, occupancy), UNCOMMITTED_WINDOW_CAP)``: ``SEEN_LIMIT``
        stays a hard floor beneath it, because ``target`` never falls below it and
        the cap is four times it, and the cap outranks ``pass_volume`` rather than
        the other way round.

        THE CLAMP IS KEYED ON THE ANSWER, NOT ON OCCUPANCY (#194 round 6). The
        guard used to read ``if occupancy > UNCOMMITTED_WINDOW_CAP``, which is a
        PROXY for "the value about to be returned exceeds the ceiling" and is not
        the same question. ``target`` carries ``max(pass_volume)`` independently of
        the window's current length, so the moment after ``begin_pass`` trims a
        stuck stream to exactly the cap, occupancy is 2,000, the ``>`` is False,
        and the fall-through returned ``max(target, occupancy)`` -- measured 3,000
        against a cap of 2,000. ``mark_seen`` sizes mid-pass overflow off that
        value, so the over-cap target was live rather than dead code. The condition
        now tests ``max(target, occupancy)``, which is exactly what is returned, so
        no path can leave here above the ceiling while ``uncommitted`` is set.

        THE WARNING FIRES WHEN THE CLAMP BINDS, which is that same condition and
        not occupancy either: a stream whose recorded volume alone outranks the
        ceiling is a stream the operator needs told about, whether or not the
        window happens to have been trimmed a moment ago.

        WHAT THIS BOUNDS IS THE PASS BOUNDARY, NOT PEAK OCCUPANCY. The trim runs
        in ``begin_pass`` and ``mark_seen`` may never evict the current pass's own
        marks (#161b), so occupancy inside a pass is this target plus everything
        the pass has marked. On a stuck stream that second term is not one page --
        ``loop`` opens one pass per poll and the round drains the provider to
        exhaustion inside it -- so mid-pass occupancy still grows with the
        backlog. Closing that means changing the #161b carve-out in ``mark_seen``,
        which is a different boundary; it is stated here so the bound this
        function provides is not read as more than it is. Measured and pinned:
        ``tests/services/watch/test_watchstate_window_cap.py``.
        """
        target = max([SEEN_LIMIT, *self.pass_volume.get(key, [])])
        if not self.uncommitted.get(key):
            return target
        occupancy = len(self.seen.get(key, []))
        uncapped = max(target, occupancy)
        if uncapped > UNCOMMITTED_WINDOW_CAP:
            self._warn_window_capped(key, occupancy)
            return UNCOMMITTED_WINDOW_CAP
        return uncapped

    def _warn_window_capped(self, key: str, occupancy: int) -> None:
        """Say ONCE per stream that this window hit the cap and why (#194).

        Once per stream and not once per call, because ``_window_target`` runs on
        every ``mark_seen`` and every ``begin_pass`` trim: a line per call is
        thousands per round, which buries the one fact it exists to surface. Once
        per stream and not once per PROCESS, because Graph runs a stream per
        folder with independently-settled cursors, and a global latch would
        report the first stuck folder and hide every other one.

        The latch is a ``PrivateAttr`` and so resets on restart, deliberately.
        The claim the line makes -- "this stream's cursor has not committed and
        its window is at the ceiling" -- is about a condition that is still true
        right now, so a fresh process is entitled to say it again. Persisting the
        latch would silence the warning forever after the first emission, on the
        exact deployment where it keeps being true.

        The key is a STORE KEY, which is a provider container id or
        ``MAILBOX_WIDE_KEY`` -- never message content. Naming it is the point:
        without it the operator knows a window capped and not which stream to go
        and look at.
        """
        if key in self._capped_windows:
            return
        self._capped_windows.add(key)
        logger.warning(
            f"WATCH: replay window hit the cap key={key} occupancy={occupancy} "
            f"cap={UNCOMMITTED_WINDOW_CAP} limit={SEEN_LIMIT} "
            "(this stream's cursor has not committed, so the window could not "
            "release; ids beyond the cap may now be reprocessed -- check whether "
            "the cursor write is failing)"
        )

    def mark_seen(self, container_id: str | None, message_id: str) -> None:
        """Record a processed message id, evicting oldest CARRYOVER past the bound.

        Idempotent on its own account: marking an id that is already in the
        window does not duplicate it and does not move it to the newest
        position. Re-ordering on a repeat hit would let one message that keeps
        replaying hold the window open and push out ids that have not been
        seen since, which inverts what the bound is for.

        EVICTION NEVER TOUCHES THIS PASS'S OWN MARKS (#161b). The overflow is
        taken from the front, and the front is carryover from earlier passes, so
        the number of ids this call may drop is capped at the amount of
        carryover present. Once carryover is exhausted the window GROWS past
        ``SEEN_LIMIT`` rather than eating itself, and ``begin_pass`` trims it
        back when the next pass starts. Without that cap, one page larger than
        the bound evicted every mark it had just made and the page replayed in
        full -- see ``SEEN_LIMIT``.

        EACH MARK ALSO COUNTS TOWARD THE PASS'S OBSERVED VOLUME (#161 A1), which
        is what sizes the trim the next ``begin_pass`` performs. Counting here
        rather than at the pass boundary is what makes the figure survive a
        crash mid-backlog: the number is durable the moment the mark it
        describes is. Re-marking an id already in the window returns above and
        does NOT count -- the pass processed one message, not two.

        MARKING SEEN ALSO PRUNES THE MESSAGE'S RETRY BUDGET (#158 round 2). A
        seen message is out of the retry table by definition: the guard is what
        stops it ever being offered again, so a surviving count could only
        accumulate against an id nothing will attempt. See ``attempts``.

        AND EACH MARK PUTS THE STREAM IN THE UNCOMMITTED STATE (#161 round 3).
        A mark records work the STORED cursor does not yet cover, so from here
        until ``record_cursor`` runs the provider may re-offer this id and the
        window may not evict it. Flagged on the mark rather than at the pass
        boundary for the same reason the volume is counted here: the fact is
        durable the instant it is true, so a crash between the mark and the
        commit cannot leave a window that looks trimmable.

        The flag is set for a REPEAT hit too, above the early return. An id
        already in the window that is offered again is the withheld cursor's own
        signature, and reading it as "nothing new, stay committed" would clear
        the guard on precisely the round that proves it is needed.
        """
        self.clear_attempts(container_id, message_id)
        key = _store_key(container_id)
        window = self.seen.setdefault(key, [])
        current = self._pass_marks.setdefault(key, set())
        self.uncommitted[key] = True
        if message_id in window:
            current.add(message_id)
            return
        window.append(message_id)
        current.add(message_id)
        self._count_toward_volume(key)
        carryover = len(window) - len(current)
        overflow = carryover - self._window_target(key)
        if overflow > 0:
            del window[:overflow]

    # -- retry budget ----------------------------------------------------

    def attempts_for(self, container_id: str | None, message_id: str) -> int:
        """How many filing attempts this message has spent and lost.

        0 for a message with no entry, and that is the honest answer rather
        than an error worth raising: no entry is the state of every message
        that has never been refused, which is nearly all of them.
        """
        return self.attempts.get(_store_key(container_id), {}).get(message_id, 0)

    def record_attempt(self, container_id: str | None, message_id: str) -> int:
        """Count one failed filing attempt. Returns the new running total.

        The count is RETURNED rather than only stored so the caller can compare
        it against its own ceiling in the same step that increments it. The
        ceiling itself lives with the round (``round.MAX_FILING_ATTEMPTS``),
        because "how many tries are enough" is a policy of the thing doing the
        trying; this module owns only the counting and its durability.
        """
        key = _store_key(container_id)
        counts = self.attempts.setdefault(key, {})
        counts[message_id] = counts.get(message_id, 0) + 1
        return counts[message_id]

    def clear_attempts(self, container_id: str | None, message_id: str) -> None:
        """Drop this message's retry budget. Silent when there was not one.

        Called on every resolution -- a filing that landed, and a budget that
        ran out -- so the map holds only messages currently mid-retry. Pruning
        rather than zeroing is what keeps it bounded by the size of the
        failing set instead of by the mailbox: a zero row is indistinguishable
        from an absent one to every reader, and costs a row forever.

        Clearing an id that was never recorded is ORDINARY, not a defect. Most
        messages are filed on the first attempt and have no entry at all, and a
        raise here would force every success path to ask first.
        """
        counts = self.attempts.get(_store_key(container_id))
        if counts is None:
            return
        counts.pop(message_id, None)

    # -- abandonment record ----------------------------------------------

    def message_exhausted(self, container_id: str | None, message_id: str) -> bool:
        """Whether this message was already abandoned at the attempt bound.

        ``True`` means "the connector stopped trying to file this, do not start
        again." It is a stronger claim than ``seen_message``: the replay window
        forgets, deliberately and on a schedule set by mailbox volume, and this
        does not until ``EXHAUSTED_LIMIT`` is reached.

        ``container_id`` IS ACCEPTED AND DELIBERATELY NOT USED AS A FILTER
        (#185.2 round 5). Abandonment is a fact about a MESSAGE, and on Graph
        one message is offered by every folder stream that holds it: keying the
        ANSWER to the stream that happened to write the verdict let folder-B
        spend a complete fresh ``round.MAX_FILING_ATTEMPTS`` on an id folder-A
        had already given up on -- the issue's own "an exhausted id restarts at
        attempt 0", reached with no eviction and no corruption at all. The
        parameter is kept because every sibling method here takes it, because
        the caller has it, and because narrowing this read back to one stream
        must be a visible edit rather than a signature that never mentioned the
        distinction. The WRITE stays per stream; see ``exhausted``.

        ``False`` for an unknown id is the honest answer and not an error worth
        raising -- it is the state of every message that was ever filed on the
        first attempt, which is nearly all of them.
        """
        return message_id in self._exhausted_ids()

    def _exhausted_ids(self) -> set[str]:
        """Every abandoned id on this provider, across all streams. Cached.

        THE PROVIDER-WIDE MEMBERSHIP VIEW ``message_exhausted`` reads, kept as a
        derived index rather than as a second stored field. Storing it would put
        the same fact in the file twice, and two copies of one fact is a
        disagreement waiting for a partial write -- the per-stream map is the
        record, this is only a way of asking it a different question.

        CACHED BECAUSE THE READ IS PER MESSAGE AND THE RECORD IS BOUNDED BY
        ``EXHAUSTED_LIMIT`` PER STREAM. A Graph mailbox with many folders would
        otherwise rebuild a union of up to ``EXHAUSTED_LIMIT`` x stream-count
        ids for every message of every round, which is work proportional to the
        broken part of the mailbox on the healthy part of it. The cache is
        invalidated by ``mark_exhausted`` -- the only writer -- so it cannot go
        stale while the object lives, and it is a ``PrivateAttr`` so a load
        always builds it from the file it just read.
        """
        if self._exhausted_index is None:
            index: set[str] = set()
            for record in self.exhausted.values():
                index.update(record)
            self._exhausted_index = index
        return self._exhausted_index

    def announce_skip_once(self, container_id: str | None, message_id: str) -> bool:
        """``True`` the FIRST time this stream skips this abandoned message.

        A latch the round asks before it logs, kept here rather than in the
        round because the question spans rounds and a ``RoundContext`` is built
        fresh for each one and frozen. See ``_announced_skips`` for why the key
        is the pair and why it is deliberately not persisted.

        It answers and RECORDS in one call, so a caller cannot ask without
        arming -- two statements would let a ``return`` between them log the
        same line forever, which is the exact failure the latch exists to
        prevent.
        """
        pair = (_store_key(container_id), message_id)
        if pair in self._announced_skips:
            return False
        self._announced_skips.add(pair)
        return True

    def mark_exhausted(self, container_id: str | None, message_id: str) -> None:
        """Record that this message was abandoned. Bounded, oldest-first, once each.

        THE RE-ARM THIS CLOSES (#185 point 2). ``round._exhaust`` also marks the
        message seen, and that mark is what stops it burning a classification on
        the next round -- but it lives in the bounded replay window, as its
        OLDEST entry, so ``SEEN_LIMIT`` ordinary messages later it is gone and a
        re-offer draws a complete second ``round.MAX_FILING_ATTEMPTS``. This
        record is what survives that eviction, and ``round._process_message``
        consults it before classifying.

        Idempotent, and a repeat does NOT move the id to the newest position.
        Re-ordering on a repeat would let one id hold the record open and push
        out abandonments that are older but no less true, which inverts what the
        bound is for -- the same rule ``mark_seen`` follows.

        THE RECORD GROWS PAST THE CAP RATHER THAN EVICTING (#185.2 round 5).
        Evicting the oldest entry re-arms it: the id it names is one this
        provider is still offering -- that is why a new abandonment is being
        recorded on the same stream -- so dropping the verdict hands that
        message a complete fresh ``round.MAX_FILING_ATTEMPTS``, a second
        ``retry_exhausted`` row, and four more rounds of a pinned cursor. On a
        stream where filing stays broken the record refills and re-evicts, so
        the re-arm RECURS on a period set by abandonment volume, which is the
        "#158 harm on a timer" ``load``'s own ruling says the eviction door must
        not be. It is not the trade ``SEEN_LIMIT`` makes: that bound forgets ids
        whose work is DONE, and this one would forget verdicts whose subject is
        still in the inbox being offered.

        SO THE CAP IS A REPORTING THRESHOLD, NOT A CEILING. Past
        ``EXHAUSTED_LIMIT`` the record keeps every verdict and
        ``_warn_exhausted_capped`` says so once per stream. That is deliberately
        NOT unbounded-in-practice: an entry costs one message id and is written
        only after ``MAX_FILING_ATTEMPTS`` failing rounds, so passing the
        threshold at all means the stream has abandoned two thousand distinct
        messages and the mailbox is broken in a way no bound repairs. Growing a
        bounded-by-mailbox file while telling the operator beats silently
        re-arming a poison message forever.

        NO CURRENT-PASS PROTECTION IS NEEDED, unlike ``mark_seen``'s. #161b's
        failure was a single page marking more ids than the bound and evicting
        its own marks; nothing here evicts anything.
        """
        key = _store_key(container_id)
        record = self.exhausted.setdefault(key, [])
        if message_id in record:
            return
        record.append(message_id)
        if self._exhausted_index is not None:
            self._exhausted_index.add(message_id)
        if len(record) > EXHAUSTED_LIMIT:
            self._warn_exhausted_capped(key, len(record))

    def _warn_exhausted_capped(self, key: str, held: int) -> None:
        """Say ONCE per stream that the abandonment record passed its threshold.

        The line reports a BROKEN MAILBOX, which is the only thing that reaches
        this threshold: an entry is written only after a message has been
        refused ``round.MAX_FILING_ATTEMPTS`` times, so a stream past
        ``EXHAUSTED_LIMIT`` has abandoned two thousand distinct messages. The
        actionable half is therefore "find out why filing keeps failing here,"
        not "expect a retry" -- nothing is dropped and no verdict re-arms, per
        ``mark_exhausted``.

        WHAT IT COSTS IS NAMED ANYWAY, because the growth is the trade being
        made: the record keeps growing with the abandonments rather than
        forgetting them, and an operator is entitled to know the state file
        grows while the condition lasts. Silence on either side of this
        threshold is what #185 found the first time.

        Once per stream and not once per call, per ``_warn_window_capped``. The
        key is a STORE KEY -- a provider container id or ``MAILBOX_WIDE_KEY`` --
        and no message id is named: an id is the closest thing to content this
        module holds, and an operator needs the stream, not the message.
        """
        if key in self._capped_exhausted:
            return
        self._capped_exhausted.add(key)
        logger.warning(
            f"WATCH: abandoned-message record passed its threshold key={key} "
            f"held={held} threshold={EXHAUSTED_LIMIT} "
            "(this stream has abandoned more messages than the record expects to "
            "hold; every verdict is kept -- so none of them re-arms -- and the "
            "record grows while this lasts; check why filing keeps failing here)"
        )

    # -- persistence -----------------------------------------------------

    def save(self, provider: str, path: Path | None = None) -> Path:
        """Write this provider's state atomically. Returns the file written.

        The provider names WHICH file, so two providers watched from one host
        never write each other's cursors. See ``STATE_FILENAME_TEMPLATE``.

        EVERY SERIALIZED FIELD GOES OUT -- ``seen`` bounded, the rest verbatim.
        The one transformation is the window trim below; nothing else here is a
        filtered view of the object. Including ``attempts``: a retry budget
        that did not survive the write would reset on the next restart and the
        bound it enforces would never be reached (#158 round 2). ``uncommitted``
        goes out for the same class of reason -- a watcher killed mid-backlog
        must come back knowing its window covers a round no cursor does, or the
        next trim evicts ids the provider is still going to re-offer (#161 round
        3). ``exhausted`` goes out for the third instance of that class of
        reason -- a watcher restarted between rounds must come back knowing
        which messages it already gave up on, or the restart becomes a quieter
        version of the eviction re-arm #185 point 2 closed. ``_pass_marks`` is
        absent because it is a ``PrivateAttr``, which is the whole point of it
        being one.

        THE WRITE IS ATOMIC BECAUSE A TORN CURSOR IS WORSE THAN NO CURSOR.
        The payload is written to a temporary file in the SAME directory,
        flushed, ``fsync``ed, and then moved into place with ``Path.replace``
        (``os.replace`` underneath), which is atomic on a single filesystem.
        A crash can therefore leave the old complete state or the new complete
        state, never a half-written one.

        The temp file must share the directory with the target: the rename
        is only atomic within one filesystem, and a temp file elsewhere makes
        the "move" a copy that can itself be interrupted.

        The ``fsync`` matches the ledger's durability posture. Without it the
        rename can reach disk before the data it points at, and a power loss
        leaves a correctly-named file with no contents -- which reads back as
        a corrupt cursor and strands the watcher exactly the way this method
        exists to prevent.

        AND THE WINDOW GOES OUT BOUNDED TO WHAT THE NEXT ``begin_pass`` WOULD KEEP
        (#194 round 6). See ``_persisted_seen`` for why the write is the boundary
        that has to do it.
        """
        target = path or state_path(provider)
        payload = json.dumps(self._persisted_payload(), indent=2, sort_keys=True)
        write_text_atomic(target, payload, what="WATCH")
        logger.debug(f"WATCH: state saved path={target} cursor_count={len(self.cursors)}")
        return target

    def _persisted_payload(self) -> dict[str, object]:
        """This state as JSON, with every window bounded to the trim target."""
        payload = self.model_dump(mode="json")
        payload["seen"] = self._persisted_seen()
        return payload

    def _persisted_seen(self) -> dict[str, list[str]]:
        """The ``seen`` windows as the FILE should hold them: trimmed, oldest first.

        THE SAVE IS THE ONLY BOUNDARY THE FILE HAS, BECAUSE THE LOOP NEVER SAVES
        AT A PASS BOUNDARY (#194 round 6). ``loop.run_pass`` calls ``begin_pass``
        as its first statement and then runs the checkpoint in the PER-STREAM
        ``finally`` INSIDE that pass (``loop.py``; ``main.py`` binds it to
        ``state.save``). So every byte this method has ever written was mid-pass
        occupancy, and on a stuck stream mid-pass occupancy is the whole re-offered
        backlog: measured 3,000 ids / 96 KB at 15 rounds, 6,000 / 193 KB at 30,
        12,000 / 388 KB at 60 -- O(backlog), six times ``UNCOMMITTED_WINDOW_CAP``,
        with one warning line across the entire run. That is the harm #194 exists
        to close, and the clamp in ``_window_target`` could not reach it because it
        only ever runs at a boundary the writer never stands on.

        WHAT IS PERSISTED IS EXACTLY WHAT WOULD SURVIVE THE RELOAD. The first
        statement any reader executes is ``begin_pass``, which trims each window to
        ``_window_target``, so every id beyond that target is discarded before it
        is read -- writing it is pure cost with no reader. The trim here is the
        same one, taken from the same end (oldest first, newest kept), so a
        round-trip through the file is byte-identical for any state ``begin_pass``
        would leave alone.

        UNCOMMITTED STREAMS ONLY, and that restriction is #161b, not caution. A
        COMMITTED stream's target is sized from history alone, which can legitimately
        sit BELOW the marks the running pass has made -- trimming it here would drop
        the current pass's own marks, which is precisely the self-defeat
        ``mark_seen``'s carve-out exists to refuse, moved from the window into the
        file. For an uncommitted stream ``_window_target`` is floored at occupancy
        and capped at ``UNCOMMITTED_WINDOW_CAP``, so the trim can only ever bite on
        a stream past the ceiling: the broken one, where the cap already rules that
        a bounded reprocess beats an unbounded file.

        THE IN-MEMORY WINDOW IS NOT TOUCHED. A checkpoint is a copy of the state,
        not an event in the pass, and evicting live ids mid-pass because a save
        happened to run would make the replay guard depend on checkpoint timing --
        the same #161b harm by a third door. The running process keeps every id it
        marked and reasserts the bound at its own next ``begin_pass``.
        """
        persisted: dict[str, list[str]] = {}
        for key, window in self.seen.items():
            if not self.uncommitted.get(key):
                persisted[key] = list(window)
                continue
            target = self._window_target(key)
            overflow = len(window) - target
            if overflow <= 0:
                persisted[key] = list(window)
                continue
            persisted[key] = list(window[overflow:])
            logger.debug(
                f"WATCH: replay window bounded for the file key={key} "
                f"dropped={overflow} limit={target}"
            )
        return persisted

    @classmethod
    def load(cls, provider: str, path: Path | None = None) -> WatchState:
        """Read this provider's state, or an empty one if nothing is readable.

        Reads only the file named for ``provider``, so a Google watcher can
        never resume from a Microsoft cursor or the reverse.

        A missing file is the first run and is not a defect. An UNPARSEABLE
        file is announced and then treated as absent, because the recovery
        from a lost cursor is already a first-class supported path -- it is
        the same full resync an expired token triggers -- and refusing to start
        would turn a recoverable state into a stopped watcher.

        CORRUPTION HAS THREE SHAPES AND ALL THREE START FRESH (#182). The file
        may refuse to open (``OSError``), it may not be JSON
        (``json.JSONDecodeError``), or -- the likeliest of the three, and the
        one the guard used to miss -- it may be perfectly good JSON of the wrong
        SHAPE: a token that is a number, a ``seen`` map holding a string where a
        list belongs, a field a half-written upgrade truncated away. That last
        one raises ``pydantic.ValidationError``, which travelled straight past
        an except clause naming only the first two and out of ``load``. Since
        ``load`` runs at the top of every round, the result was not a degraded
        watcher but a STOPPED one, on the exact failure the fresh-start recovery
        was written for. There is no cursor here worth crashing to protect.

        THE RESET RE-ARMS THE POISON-MESSAGE BUDGET, DELIBERATELY (#185). Every
        door here returns ``cls()``, which discards ``attempts`` along with
        ``seen``: a message that had already spent ``round.MAX_FILING_ATTEMPTS``
        and been abandoned is offered again at attempt 0, so repeated corruption
        means repeated bounded cursor-pin cycles. It is kept rather than closed
        because both alternatives are worse. Preserving the counts means
        salvaging numbers out of a file this method has just judged
        untrustworthy -- there is no second source, since the budget lives inside
        the object being thrown away. Carrying a stale bound forward instead
        abandons a message that may now file, on the strength of a count from a
        corrupt file, when an attempts entry only ever recorded that a provider
        was refusing it AT THE TIME.

        The re-arm costs at most one more bounded cycle, is receipted, and
        orphans nothing. What it must not be is silent, which is what #185
        found: ``_fresh_start_line`` names the budget among what was reset.

        AND THIS RESET IS THE ONLY RE-ARM THERE IS (#185 point 2, the ruling).
        Point (2) found a second, unruled door: an abandoned message's only
        record was its slot in the bounded replay window, so ``SEEN_LIMIT``
        ordinary messages evicted it and a re-offer drew a complete fresh
        budget -- every ``SEEN_LIMIT`` messages, for the life of the deployment.
        The ruling above does NOT transfer to that door and was never meant to.
        Its whole justification is that the counts live inside an object being
        discarded as untrustworthy and there is no second source; on eviction
        nothing was discarded, the state file is valid and stays valid, and the
        count was deleted by this module's own ``mark_seen``. A recurring re-arm
        is also a different animal from "at most one more bounded cycle": it is
        the #158 harm on a timer.

        So EVICTION NEVER RE-ARMS. Abandonment is recorded in ``exhausted``,
        which is persisted here with everything else and is not the replay
        window -- see ``EXHAUSTED_LIMIT`` and ``mark_exhausted``. The corrupt
        state reset discards that record too, which is correct and is the same
        ruling: this door is the deliberate, bounded, announced one, and it is
        the only door.

        AND THE UPGRADE IS THE OTHER ONE-TIME RE-ARM, ANNOUNCED (#185.2 round
        5). A state file written before ``exhausted`` existed has no such key,
        and reading its absence as "nothing was abandoned here" -- which this
        module used to call the honest reading needing no migration -- reopens
        the full #185.2 harm for every deployment that upgrades in place: the
        message that WAS abandoned sits in the replay window as its oldest
        entry, the first ``SEEN_LIMIT`` ordinary messages evict it, and the
        re-offer draws a complete fresh ``round.MAX_FILING_ATTEMPTS``.

        The absence is therefore ANNOUNCED rather than migrated. No verdict is
        invented from the ``seen`` window, because that window cannot tell a
        message that was filed from one that was given up on -- the collapse
        this record exists to end -- and a guessed verdict would abandon mail
        that files perfectly well, permanently and silently. The honest handling
        is one INFO line saying the record is being introduced and that a
        message abandoned before the upgrade may be retried for one more bounded
        cycle; see ``_announce_exhausted_introduced``. It is one cycle and not a
        recurring one, because the next ``save`` writes the key and every
        abandonment from then on is recorded as it happens.

        THE COST OF ALL THIS IS THAT ABANDONMENT IS PERMANENT (#185.2 round 5,
        variant L). Before the record existed, window eviction was the de-facto
        recovery path for a message whose refusal later cleared -- a quota that
        reset, a folder that was unlocked, a 412 that stopped. Closing the
        eviction door closes that path with it: an abandoned message is skipped
        by ``round._process_message`` before it is classified, for as long as
        this state file lives, even when the provider would now file it
        happily. It stays in the inbox with its ``retry_exhausted`` receipt, and
        the round that skips it reports healthy. That is the deliberate trade --
        a bounded, receipted, visible non-recovery beats an unbounded re-file
        cascade -- and the two ways out are both the operator's: file the
        message by hand, or reset the state, which is the corrupt-state door
        above. ``round.MAX_FILING_ATTEMPTS`` carries the same statement, and
        ``round._process_message`` logs the skip at DEBUG so a round is never
        silently healthy about it.
        """
        target = path or state_path(provider)
        if not target.exists():
            return cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            logger.warning(_fresh_start_line("state unreadable", target))
            return cls()
        if not isinstance(raw, dict):
            logger.warning(_fresh_start_line("state is not an object", target))
            return cls()
        try:
            state = cls.model_validate(raw)
        except ValidationError as error:
            logger.warning(
                _fresh_start_line("state is structurally corrupt", target)
                + f" errors={error.error_count()}"
            )
            return cls()
        if "exhausted" not in raw:
            _announce_exhausted_introduced(target)
        state._drop_stale_generation_cursors()
        return state

    def _drop_stale_generation_cursors(self) -> None:
        """Throw away every cursor a build older than ``CURSOR_GENERATION`` minted.

        RUN ON LOAD, NOT ON SAVE, and not as a one-shot migration script.
        Load is the only moment that sees every cursor a deployment actually
        has, including the ones on a machine that was never upgraded through an
        intermediate build, and it is the moment BEFORE anything replays one --
        which is the only place a drop can still prevent the defect rather than
        merely record it.

        The drop goes through ``resync_required`` rather than popping the dict,
        so a forced cold start is bookkept exactly like a provider-demanded one:
        the cursor is REMOVED (not blanked, so nothing can replay an empty
        string as a token), the stream's resync counter moves, the replay window
        is kept, and one log line says which stream and why. It IS a resync --
        it just came from this side of the wire.

        Idempotent by construction. The next ``record_cursor`` stamps the
        current generation, so a stream is dropped once and never again; a state
        file with nothing stale does no work here at all.
        """
        # Keyed off the STORE KEY, not ``cursor.container_id``. The key is what
        # ``cursor_for`` and ``resync_required`` address, so a row whose stored
        # field ever disagreed with its key would otherwise survive the drop
        # under the key that actually gets read.
        stale = [
            None if key == MAILBOX_WIDE_KEY else key
            for key, cursor in self.cursors.items()
            if cursor.cursor_generation < CURSOR_GENERATION
        ]
        if not stale:
            return
        logger.warning(
            f"WATCH: dropping {len(stale)} cursor(s) minted before generation "
            f"{CURSOR_GENERATION} (they resume a stream they were not opened on -- "
            "each affected stream cold-starts once, replay guard intact)"
        )
        for container_id in stale:
            self.resync_required(container_id)
