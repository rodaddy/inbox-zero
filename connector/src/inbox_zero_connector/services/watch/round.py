"""ONE polling round for one provider stream. No daemon, no scheduler, no sleep.

WHAT THIS MODULE IS
    The single round the unattended watcher repeats: read the stored cursor,
    ask the provider what changed, decide and file what is in scope, receipt
    every attempt, and store the new cursor only if the provider said the
    round is finished. Filing means the message leaves the inbox. Sprint 1's
    reset contract owns placement recovery, which is what makes that product
    behavior safe to repeat. The loop that calls this round remains separate
    so the state table can be tested with no clock.

WHAT IT DELIBERATELY DOES NOT OWN
    Not one decision this package already makes elsewhere is re-made here.
    ``classify`` owns the filing decision and coercion to ``Review`` when a
    classifier is uncertain, ``MailboxScope`` owns whether a container may be
    read, ``WatchState`` owns cursors and the replay window, ``ledger`` owns
    the receipt, and the adapters own every provider difference behind
    ``MailProvider``. There is no provider branch here: one mailbox-wide Gmail
    stream and N per-folder Graph streams are absorbed by
    ``container_id: str | None``.

THE WRITE CONTRACT, ENFORCED BY STRUCTURE
    MOVE THE MESSAGE OUT OF THE INBOX
        ``file_message`` relocates on Graph and de-inboxes on Gmail. That is
        now the product rather than a prohibited side effect, and every attempt
        receives exactly one ledger row. Each classified message also leaves
        one content-free decision-trace row (``services.audit.trace``) beside
        that ledger. Sprint 1's reset contract is the
        recovery boundary that makes this write appropriate for unattended
        runs.

    AND, SINCE SPRINT 4, DRAFT A REPLY -- ONCE PER THREAD, FOREVER
        ``create_draft`` is the second and last provider write this module may
        reach. ``WRITE_METHODS`` names both, and the pairing is the point:
        drafting is not idempotent at the provider, so it is gated on the
        append-only ledger (``_already_drafted``) rather than on the bounded
        replay window, which a resync defeats. A draft is an ADDITION on top of
        a filing -- it happens after the filing and its receipt, and its
        failure never undoes either.

        WHICH IS ALSO WHY DRAFTING IS OPTIONAL AT THE MODULE LEVEL, not just at
        the config level. The voice add-on that composes the reply is a SEPARATE
        TIER and is simply absent from the basic kit, so this module loads it
        lazily (``_drafting``) and files exactly as it always did when the
        answer is None. Filing is the product; drafting is the addition, and
        that sentence is now true of the shipped bytes and not only of the
        control flow.

    UNCERTAIN MAIL GOES TO REVIEW
        ``classify`` coerces an illegal or low-confidence result to the
        taxonomy's explicit ``Review`` folder. This module files that result
        exactly like any other decision, so ambiguity clears the inbox rather
        than remaining behind with a tag.

    NEVER DRAFT, NEVER FLAG IMPORTANT, NEVER SEND, NEVER DELETE
        ``create_draft`` and ``set_important`` remain in
        ``FORBIDDEN_METHODS``. Send and delete remain absent from
        ``MailProvider`` entirely. Drafting belongs to a later sprint; moving
        mail is the only capability reset made safe here.

    NEVER CLASSIFY A MESSAGE OUTSIDE SCOPE
        Classification reads subject, sender, and snippet. The order in
        ``_process_message`` is fixed: scope, replay, classify, file. Filtering
        after classification would discard a variable, not undo the read.

    NEVER STORE A CURSOR IT DID NOT RECEIVE AS FINAL, OR DID NOT EARN
        See ``_store_cursor`` for the position rule -- the silent-data-loss
        boundary, one function and one call site -- and ``_settle_cursor`` for
        the failure rule: a round that could not file a message keeps the old
        position so the next round is offered that message again, UNTIL that
        message has spent ``MAX_FILING_ATTEMPTS``. The bound is the other half
        of the rule: without it one permanently unfileable message pins the
        cursor forever and the endless re-read eventually re-files everything
        past the replay window (#158 round 2).

    NEVER ABANDON THE ROUND FOR ONE MESSAGE, EXCEPT FOR THE LEDGER
        The per-message boundary in ``_apply`` catches ``Exception`` -- the
        classifier is inside it, so a backend that raised fails one message
        and not the round. ``OSError`` from the ledger is the single
        carve-out: a mutation without its receipt breaks the record.

WHAT THE SUMMARY CARRIES
    Counts and a container id. Never a subject, sender, snippet, or provider
    error prose. Provider messages can quote content, so the ledger records an
    error code and never the exception text.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from types import ModuleType
from typing import Final, Protocol, runtime_checkable

from loguru import logger
from pydantic import BaseModel, ConfigDict, model_validator

from inbox_zero_connector.config import VoiceRouting
from inbox_zero_connector.providers.base import (
    ChangeSet,
    Container,
    MailProvider,
    Message,
    Thread,
)
from inbox_zero_connector.services.audit import intents as intents
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.audit import trace as trace
from inbox_zero_connector.services.triage import learned as learned
from inbox_zero_connector.services.triage.decide import Classification, Classifier, classify
from inbox_zero_connector.services.triage.learned import LearnedRule
from inbox_zero_connector.services.triage.taxonomy import Taxonomy
from inbox_zero_connector.services.watch import drafting_loader as drafting_loader
from inbox_zero_connector.services.watch import preview as preview
from inbox_zero_connector.services.watch import receipts as receipts
from inbox_zero_connector.services.watch.preview import PlannedFiling as PlannedFiling
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import UNCOMMITTED_WINDOW_CAP, WatchState

# THE DRAFTING MODULE IS LOADED LAZILY, AND #146 IS WHY. The basic tier ships
# this package and does NOT ship the voice add-on, so a module-scope import of
# it here made an installed basic kit raise ``ModuleNotFoundError`` on its
# first watch -- before a single message was filed.
# ``drafting_loader._drafting()`` is that loader; the drafting path turns OFF
# when it answers None, which IS the basic tier's product (folder triage, no
# drafts). The precedent is ``apps/cli/main.py``'s in-function watch-loop
# import, guarded by ``tests/apps/cli/test_cold_import.py``; this one is
# guarded by ``tests/services/watch/test_watcher_without_voice.py``.
#
# THE LOADER MOVED OUT OF THIS FILE in #189 round 7, and the seam is real
# rather than a place to put lines. Every function in ``drafting_loader``
# talks to the import system and to ``logger``; not one of them reads a
# message, a cursor, a taxonomy, or a provider. It answers a PACKAGING
# question -- can this deployment draft, and if not whose fault is that -- and
# this module answers a MAIL question.
#
# EXACTLY TWO NAMES ARE RE-EXPORTED, ``_DRAFTING_MODULE`` and ``_drafting``,
# and they are the two this module's own code and callers use. The loader's
# other functions are private to it and are driven directly by
# ``tests/services/watch/test_drafting_loader_real_modules.py``, which re-aims
# the LOADER's globals: the aliases below are bound at import, so re-aiming one
# here would leave the loader still reading its own.
#
# ``VoiceRouting`` STAYS AT MODULE SCOPE and is not the same problem. It comes
# from ``config``, which every tier ships, so it never reaches the packaging
# judge. It also could not be deferred even if it did: it types a pydantic
# FIELD on ``RoundContext``, and pydantic resolves a model's annotations when
# the first instance is built, not when the module loads. Under
# ``if TYPE_CHECKING`` the name is absent at that moment and EVERY construction
# fails with ``PydanticUserError: RoundContext is not fully defined``.
# Inspecting ``model_fields`` does NOT surface this -- it reports a contented
# ``ForwardRef`` -- so the check that matters is instantiating the model, which
# the suite does. The same rule is what keeps ``draft_backend``'s annotation
# off the drafting module; see the field for the shape it uses instead.

_DRAFTING_MODULE: Final[str] = drafting_loader._DRAFTING_MODULE
"""Dotted path of the add-on module the loader loads. Re-exported, not redefined.

ONE DEFINITION, IN THE MODULE THAT USES IT. ``drafting_loader`` assembles this
from its own ``__package__`` for the packaging reason recorded there, and a
second literal here would be a second thing to keep in sync -- and the one that
drifts is the one nothing calls. This alias exists so a reader of ``round``
still finds the name, and so the module the loader actually reads is never
ambiguous: it is ``drafting_loader``'s global, which is also the one the tests
re-aim.
"""

_drafting: Final = drafting_loader._drafting
"""The cached tier switch. See ``drafting_loader._drafting`` for the contract."""


@runtime_checkable
class DraftComposer(Protocol):
    """The composer seam AS THE WATCH SIDE SEES IT. Structurally identical to
    the add-on's ``DraftBodyBackend``, and declared here because this side has
    to keep working when the add-on is not installed.

    NOT A DUPLICATED ABSTRACTION, which is the objection worth answering. A
    Protocol is structural: nothing implements it by inheritance, so there is
    no second thing to register or keep in sync -- the add-on's own protocol
    and this one describe the same one-method shape, and any object satisfying
    either satisfies both. What a shared import would buy is a shared NAME, at
    the cost of making ``round`` unimportable without the add-on, which is the
    exact defect #146 fixed.

    IT HAS TO BE A REAL CLASS RATHER THAN A ``TYPE_CHECKING`` alias, for the
    reason the comment above ``_DRAFTING_MODULE`` gives: this types a pydantic
    field, and pydantic resolves the annotation at first instantiation. A name
    that exists only to a type checker fails every construction at runtime.

    ``voice`` is typed ``object`` because its real type (``VoiceProfile``)
    lives in the add-on. The watch side never inspects the value -- it
    originates in the add-on and is handed straight back to it -- so this side
    has no need to name the type and no way to.
    """

    def compose(self, prompt: str, voice: object) -> str:
        """Return reply body text for ``prompt``, written in ``voice``."""
        ...


WRITE_METHODS: Final[tuple[str, ...]] = (
    "file_message",
    "create_draft",
)
"""The ONLY provider methods this module is permitted to call for a write.

=========================================================================
THIS CONSTANT CHANGED SHAPE IN SPRINT 4, AND THE CHANGE IS THE TICKET
=========================================================================

It was ``WRITE_METHOD: Final = "file_message"`` -- singular, one name -- and
``create_draft`` sat in ``FORBIDDEN_METHODS`` below with the stated reason that
it "is not idempotent: a replayed round would leave a second draft in the
owner's mailbox and a second ledger row for one real intent."

THAT REASON WAS CORRECT AND IT WAS NOT DELETED. It was ANSWERED, by
``ledger.thread_has_draft`` and ``_already_drafted`` below, which consult the
append-only ledger for a prior ``create_draft`` row on the thread before any
provider call. The ledger is not cleared by a cursor resync, so the gate holds
in exactly the case the bounded ``seen`` window cannot cover. One draft per
thread, forever (operator ruling, 2026-08-19).

The prohibition and its gate move TOGETHER or not at all. Removing the name
from ``FORBIDDEN_METHODS`` without the gate landing in the same change is how a
second draft reaches a client's mailbox, and the guarding test
(``tests/test_watcher.py``) changed in the same deliverable, deliberately and
visibly, rather than being relaxed afterwards to make a green run.

Filing moves the message out of the inbox, which is the product behavior rather
than a prohibition; Sprint 1's reset contract is the recovery boundary that
makes unattended moves safe to repeat. A draft has a weaker recovery story --
nothing here deletes one -- which is precisely why its gate is stricter than a
replay window.
"""

FORBIDDEN_METHODS: Final[tuple[str, ...]] = ("set_important",)
"""Provider methods this module must never call, though they exist on the interface.

``set_important`` is a different product decision and stays out. ``file_message``
and ``create_draft`` are absent because they are this module's write surface,
enumerated in ``WRITE_METHODS`` above; ``create_draft`` left this tuple in
Sprint 4 together with the ledger idempotency gate that answers the reason it
was here. Send and delete are absent because they remain absent from the
provider interface entirely -- an absence, never a guard.
"""


class RoundState(StrEnum):
    """How one round ended. Closed set, so a caller's match is checkable.

    A ``StrEnum`` rather than a bare string because the set is closed and a
    hand-typed ``"resynced"`` would ship silently
    (``_DOCS/STANDARDS-python.md`` ## LAW: flat control flow).
    """

    RESYNCED = "resynced"
    """The provider refused the stored token. The cursor was dropped and its
    generation bumped, and none of the messages returned ALONGSIDE that
    refusal were processed.

    The counts are not necessarily zero. A refusal on the first poll ends the
    round before anything is read, but a multi-page round can be refused on a
    later page, and the pages already drained filed and receipted real
    messages. Those counts are reported as they stand."""

    PAGED = "paged"
    """The round finished its pages and NO cursor was stored. TWO ways to get
    here, and the state does not distinguish them because the consequence is
    identical: the stream stays exactly where it was and the next round
    re-reads from it.

    1. The provider never offered a final cursor -- every page drained and the
       last still carried none. See ``_store_cursor``.
    2. The round produced at least one FAILED result with retries still left,
       so the cursor it DID receive was deliberately withheld. See
       ``_settle_cursor``: a committed cursor would move the stream past a
       message that never left the inbox, and the retry the replay guard
       permits would never be offered one. A round whose only failures are
       EXHAUSTED does NOT land here -- it commits.

    This is no longer "more pages remain": ``poll_once`` fetches to
    exhaustion, so a round that ends here has nothing left to fetch."""

    COMMITTED = "committed"
    """The provider returned a final position. The cursor was stored. This is
    the only state in which a cursor is written."""


class RoundSummary(BaseModel):
    """What one round did, in counts. Frozen: a completed round is a fact.

    COUNTS ONLY, AND THAT IS A SECURITY PROPERTY RATHER THAN A STYLE CHOICE.
    This value is returned to a caller that may log it, print it, or ship it
    to a dashboard. A subject line or a sender in any of those places is
    mailbox content leaving the mailbox through the monitoring channel, which
    is the same leak the ledger's ``error_code``-not-message rule exists to
    close. There is no field here that can hold message text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    container_id: str | None
    """Which stream this round polled. ``None`` is the mailbox-wide stream."""

    state: RoundState
    seen: int
    """Messages the provider reported on this page, before any gate."""

    skipped_out_of_scope: int
    """Discarded at the scope gate. NEVER classified -- the read never happened.

    The SCOPE refused it: every container the message carries was enumerated,
    and the installed rules exclude at least one of them. A message whose
    container the listing never carried is counted in
    ``skipped_unenumerated_container`` instead (#198)."""

    skipped_unenumerated_container: int = 0
    """Refused because a container id on the message was not in the listing.

    Also never classified, and refused for the same safety reason -- an
    unreadable placement is indistinguishable from an excluded one, and only
    one of the two mistakes reads privileged mail. Counted SEPARATELY because
    the operator's fix is different: nothing in the scope section is wrong, and
    ``list-folders`` is the command that shows what the connector can actually
    enumerate.

    Defaulted so a caller constructing a summary by hand (a test, a stub pass)
    predates the field harmlessly; ``_summary`` always passes it."""

    skipped_missing: int = 0
    """Ids the page listed that no longer existed when they were fetched (#57).

    Summed across the round's pages from ``ChangeSet.skipped_missing``, and NOT
    part of the ``seen`` partition -- these ids never became messages, so no
    gate saw them and the accounting validator has nothing to place. Carried
    here so a mailbox shedding 404s is visible in the round log instead of only
    inside a page object the caller never sees. Defaulted for the same reason
    ``skipped_unenumerated_container`` is."""

    skipped_already_seen: int
    """Suppressed by the replay guard. Already processed in an earlier round."""

    classified: int
    """Messages a classification was produced for. Equals the number that
    passed both gates."""

    applied: int
    """Filings the provider accepted. One ``Status.APPLIED`` ledger row each."""

    failed: int
    """Messages that were NOT filed. One ``Status.FAILED`` row each, and the
    round CONTINUED -- one contended message must not block the rest.

    Not only a provider refusal since #157: a classifier that raised counts
    here too, receipted at ``_UNCLASSIFIED``. A filing abandoned at
    ``MAX_FILING_ATTEMPTS`` counts here too, receipted with
    ``RETRY_EXHAUSTED`` -- that message is not filed either, and a summary that
    hid it would make a round that gave up look healthier than the four that
    kept trying.

    A non-zero value here usually withholds the round's cursor
    (``_settle_cursor``), so a failure is normally visible in ``cursor_stored``
    as well as in this count. The one exception is a round whose failures have
    ALL exhausted their retries: nothing there can be offered again, so the
    stream is allowed to move and this count is the only signal left. Read this
    field, not ``cursor_stored``, to answer "did every message get filed?"."""

    cursor_stored: bool
    """True only when ``state`` is ``COMMITTED``. See ``_store_cursor`` for the
    position rule and ``_settle_cursor`` for the failure rule."""

    resynced: bool
    """True when the provider demanded a full resync this round."""

    planned: tuple[PlannedFiling, ...] = ()
    """The filings a DRY round decided on and did not perform. EMPTY otherwise.

    PREVIEW-ONLY BY CONSTRUCTION, and the emptiness on a real round is the
    point rather than an omission. A round that filed already has a record --
    the ledger -- and populating this on the normal path would make every
    summary carry a second, unreceipted copy of it, printable by anything that
    reads a summary and covered by none of the ledger's own handling.

    Counted in NEITHER ``applied`` NOR ``failed``, and in neither
    ``classified``: those three describe a round that tried to move mail, and a
    dry round did not try. A previewed message is its own outcome in the
    ``seen`` partition (``previewed``), which is what keeps the partition
    validator below honest without loosening ``classified == applied +
    failed`` -- an equality that exists to catch a filing outcome added with no
    bucket, and would stop catching it the moment a third kind of ending was
    allowed to hide inside it.
    """

    @model_validator(mode="after")
    def _every_message_seen_is_accounted_for(self) -> RoundSummary:
        """EVERY message counted ``seen`` lands in exactly one outcome. Enforced.

        ``seen == skipped_out_of_scope + skipped_unenumerated_container +
        skipped_already_seen + classified``,
        and ``classified == applied + failed``. That is not an observation about
        the current tally, it is the shape of the tally: ``_summarise`` counts
        one ``_MessageResult`` per message over a closed enum, and every member
        of that enum lands in one of these four buckets -- OUT_OF_SCOPE,
        UNENUMERATED_CONTAINER, ALREADY_SEEN, and APPLIED/FAILED/EXHAUSTED
        which together are ``classified``.

        WHY IT IS A VALIDATOR AND NOT A COMMENT (#201.3). Downstream code
        reasoned about this partition without it being enforced anywhere.
        ``PassResult.saw_only_out_of_scope_mail`` carried an "unexplained
        remainder" clause -- a message seen that is none of the three -- and
        stayed silent for it, with a test constructing that round by hand to
        prove the clause worked. No ``poll_once`` tally can produce such a
        round, so the clause guarded against nothing and the test described a
        mailbox state that cannot exist: honest-looking coverage of fiction.
        Stating the invariant HERE, where the counts are made, is what lets the
        consumer stop hedging about a case the type forbids -- and turns a
        future ``_MessageResult`` member that forgets a bucket into a loud
        failure at construction instead of a silently mis-scoped alarm.

        Raises:
            ValueError: The counts do not partition ``seen``.
        """
        accounted = (
            self.skipped_out_of_scope
            + self.skipped_unenumerated_container
            + self.skipped_already_seen
            + len(self.planned)
            + self.classified
        )
        if accounted != self.seen:
            raise ValueError(
                f"RoundSummary counts do not account for every message seen: seen="
                f"{self.seen} but skipped_out_of_scope={self.skipped_out_of_scope} + "
                f"skipped_unenumerated_container={self.skipped_unenumerated_container} + "
                f"skipped_already_seen={self.skipped_already_seen} + previewed="
                f"{len(self.planned)} + classified="
                f"{self.classified} = {accounted}. Every message a round reports "
                "lands in exactly one outcome; a mismatch means a new message "
                "outcome was added without a bucket to count it in."
            )
        if self.classified != self.applied + self.failed:
            raise ValueError(
                f"RoundSummary classified={self.classified} does not equal applied="
                f"{self.applied} + failed={self.failed}. A classified message either "
                "filed or did not; there is no third ending."
            )
        return self


class RoundContext(BaseModel):
    """Everything one round needs, bundled so no function grows past 5 arguments.

    A model rather than loose keyword arguments for the reason
    ``ledger.Outcome`` is one: these travel together always, and the parameter
    ceiling in ``_DOCS/STANDARDS-python.md`` is the signal that they are one
    concept. ``arbitrary_types_allowed`` because ``MailProvider`` is an ABC,
    not a pydantic model -- it is held, never validated or serialised.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    provider: MailProvider
    scope: MailboxScope
    taxonomy: Taxonomy
    state: WatchState
    container_id: str | None = None
    run_id: str = ""
    """This round's own id, minted ONCE by ``poll_once`` and stamped on every receipt.

    THE FIELD ``undo --run`` SELECTS ON (#66). A ledger row's timestamp cannot
    stand in for it: two rounds can start inside the same second, and a paged
    round's rows span many seconds, so no time window names exactly one round's
    filings. A uuid4 does, and it is minted rather than derived so a re-run
    against the same mailbox at the same moment is still a different run.

    DEFAULTS TO EMPTY so a caller that builds a context by hand -- a test, a
    stub pass -- predates the field harmlessly and writes rows that ``undo``
    honestly reports as belonging to no run. ``poll_once`` always fills it.
    """

    classifier: Classifier | None = None
    """The INJECTED decision-maker. Never constructed here, so a test supplies
    a deterministic one and no model call happens in the suite."""

    draft_backend: DraftComposer | None = None
    """The INJECTED reply composer, or None. None DISABLES DRAFTING ENTIRELY.

    Off by default, and that default is the product posture rather than a
    convenience: a deployment that has not deliberately turned drafting on gets
    a watcher that files and nothing else, which is what every pre-Sprint-4
    caller already expects. Never constructed here, so the suite composes no
    reply through a model.
    """

    routing: VoiceRouting | None = None
    """Installed voice routing, or None. Validates the voice NAME; never picks it.

    Optional because ``VoiceRouting`` has required fields and a watcher that
    does not draft has no routing to supply. ``_draft`` refuses when this is
    None for the same reason it refuses on a missing backend: an absent
    installation is not a permissive one.
    """

    voice: str = ""
    """The routed voice name for this stream's drafts.

    A NAME, resolved outside this module, and the AI cannot override it -- the
    profile is looked up from this name inside ``drafting.generate_draft_body``
    rather than passed in as an object a caller could substitute.
    """

    owner_address: str = ""
    """The owner's own address, from installed config and never from mail.

    Read for two decisions only: whether the thread has anyone on it besides
    the owner, and whether the owner has already replied.
    """

    account: str = ""
    """WHICH configured mailbox this round is polling, stamped on every receipt.

    SEPARATE FROM ``owner_address`` because they answer different questions and
    a single field would answer neither on a multi-account install (#69). This
    one names the mailbox being WATCHED, so one ledger holding two accounts'
    rows can be split back apart; ``owner_address`` is the identity the
    drafting path compares recipients against. On a one-account install they
    hold the same string, which is exactly why merging them looks harmless
    until a second account exists.

    DEFAULTS TO EMPTY so every caller that predates the field -- a test, the
    single-account path before its config lists accounts -- keeps working and
    writes rows that name no mailbox, which is the honest reading of a round
    that was never told which one it was serving.
    """

    shed: list[int] = []
    """One ``ChangeSet.skipped_missing`` tally per page fetched this round.

    A mutable field on a frozen model for exactly the reason ``plans`` below is
    one, and read at ``_summary``'s single construction site so neither ending
    has to thread the count through its own signature (#57). Its LENGTH is the
    round's page count, which is why ``poll_once`` carries no separate counter:
    one append per fetch is the same fact both readings need.
    """

    plans: list[PlannedFiling] = []
    """A DRY round's planned filings, accumulated across pages. EMPTY otherwise.

    A MUTABLE FIELD ON A FROZEN MODEL, deliberately. ``frozen`` stops the field
    being REBOUND, which is the guarantee that matters here -- no code path can
    swap one round's preview for another's -- while the list itself is the
    round's accumulator, written only by ``preview.plan_only`` and only under
    ``dry_run``. Threading it as a parameter instead would put an argument on
    ``_process_page`` and ``_process_message`` that is unused on every real
    round, which is the shape that invites a caller to pass the wrong one.

    Pydantic gives each instance its own list (it copies the default), so this
    is not the mutable-default trap it resembles.
    """

    dry_run: bool = False
    """PREVIEW ONLY. Classify every in-scope message and mutate NOTHING (#66).

    DEFAULT FALSE, AND THE DEFAULT IS THE WHOLE SAFETY ARGUMENT. A watcher is a
    thing that files mail; a flag that had to be turned OFF to get that would
    make every caller predating it into a silent no-op, which is a worse
    failure than the one this closes because nothing would look wrong.

    WHAT IT SKIPS IS THE WRITE, NOT THE READ, AND NOT THE GATES. The scope gate
    still refuses out-of-scope mail before ``classify`` sees it -- a preview
    buys no permission the real pass does not have, and the read is the event a
    privileged mailbox's owner cares about. The replay guard still suppresses
    what an earlier round already handled. What a dry round does not do is
    reach ``_apply`` at all: no ``file_message``, no write-ahead intent, no
    ledger receipt, no trace row, no replay mark, and -- the one that is easy
    to forget -- NO STORED CURSOR. A preview that committed its position would
    move the stream past the mail it only looked at, and the real pass the
    operator runs next would never be offered it.

    WHY THE FLAG EXISTS. Both adapters create the taxonomy's folders LAZILY,
    inside their own filing call (``providers/gmail/provider.py``
    ``_create_label``, ``providers/outlook/provider.py`` ``create_folder``), so
    the first real pass against a live mailbox builds folders in the owner's
    mail client with no warning beforehand. ``watch --once --dry-run`` is that
    warning: the CLI compares the planned folders against the mailbox's own
    listing and marks the ones that do not exist yet.
    """

    learned_rules: tuple[LearnedRule, ...] | None = None
    """The owner's learned-rule table, loaded ONCE per round in ``poll_once``.

    Held on the context rather than read per message: the file does not change
    inside a round, so a per-message load would multiply one read by the page
    size for an identical answer -- the same reasoning as
    ``_container_index``. ``None`` means "not loaded yet" and is what
    ``poll_once`` fills in; a caller that passes a table explicitly (a test, or
    a future caller with its own source) is honoured and no file is read.
    """


class _MessageResult(StrEnum):
    """What happened to one message. Internal; a table row, not a branch tree."""

    OUT_OF_SCOPE = "out_of_scope"
    UNENUMERATED_CONTAINER = "unenumerated_container"
    """The message sits in a container id the provider's listing did not carry.

    SPLIT FROM ``OUT_OF_SCOPE`` BECAUSE THEY HAVE DIFFERENT FIXES (#198). Both
    refuse the message and both refuse it for the right reason -- an unreadable
    placement must never resolve to permission -- but one says the operator's
    scope did what it was told and the other says the watcher could not tell
    where the mail is. Counted together, an ``all_folders`` install whose mail
    lands in a folder created since the listing was taken reported
    ``skipped_out_of_scope``, and every signal built on that count sent the
    operator to edit a folder list that was already correct, or, on
    ``all_folders``, to edit a list that does not exist.
    """

    ALREADY_SEEN = "already_seen"
    PREVIEWED = "previewed"
    """Classified by a DRY round and deliberately not filed (#66).

    A SEPARATE MEMBER RATHER THAN A REUSED ONE, because every existing member
    already means something a preview did not do. ``APPLIED`` claims the mail
    moved. ``FAILED`` claims the provider refused it, which alarms a scheduler
    and withholds the cursor for a retry that is not owed. ``ALREADY_SEEN``
    claims an earlier round handled it, which is the one lie an operator could
    not detect from the output. This member is what lets ``_settle_cursor``
    withhold the position for the honest reason -- nothing was committed -- and
    lets the summary report a round that attempted no filing as neither a
    success nor a failure at filing.
    """

    APPLIED = "applied"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    """A failure whose retry budget just ran out. Counted with the failures.

    Split from ``FAILED`` for exactly ONE reason, and it is the cursor:
    ``_settle_cursor`` withholds the position for a failure that still has
    attempts left and stores it for one that does not. Everywhere else the two
    are the same outcome -- both receipt a ``Status.FAILED`` row, both count in
    ``RoundSummary.failed``, and both leave the mail in the inbox -- so the
    round still reports unhealthy and #160's exit grammar is unchanged.
    """


def poll_once(context: RoundContext) -> RoundSummary:
    """Run ONE round for ONE stream and return. THE public entry point.

    The round, in order:

    0. Load the owner's learned-rule table ONCE, unless the caller already
       supplied one. This is the file I/O ``classify`` refuses to do: the
       decision layer is a pure function of its arguments, so the table is read
       here, at the round boundary, and handed down. Once per round rather than
       once per message, on the same reasoning as ``_container_index``.
    1. Read this stream's stored cursor from ``WatchState``. ``None`` is a
       cold start and is not an error -- it is the state of every stream
       before its first successful round and just after a resync.
    2. Ask the provider what changed since that cursor.
    3. IF THE PROVIDER DEMANDED A RESYNC, RESYNC AND END THE ROUND WITHOUT
       PROCESSING THE MESSAGES THAT ARRIVED WITH THAT ANSWER. The token the
       caller replayed has already been refused, so any messages alongside it
       describe a position the provider does not stand behind; acting on them
       would file messages from a stream state that was rejected, and the
       cold-start round that follows re-reads everything the stream covers
       anyway. Processing them would buy nothing and would spend replay-window
       slots on ids the next round must process again. Ending the round is also
       what keeps the resync free of a second failure mode:
       ``WatchState.resync_required`` deletes the cursor, and a round that then
       went on to write one would resurrect the position that was just thrown
       away. On a LATER page the counts already earned are reported as they
       stand -- those writes happened, and a summary claiming zero would
       disagree with the ledger.
    4. For each returned message: drop it if the replay guard has already
       processed it, and drop it if scope does not cover it -- scope FIRST,
       always, because classification is a read.
    5. Classify what survives, and file it out of the inbox with
       ``file_message``. BOTH steps sit inside the per-message boundary, so a
       classifier that raises fails ONE message rather than the round (#157).
    6. Receipt every attempt, success or failure, and keep going.
    7. Mark each APPLIED message in the replay guard, after the write -- under
       BOTH the id it arrived with and the id the write left behind, when a move
       reassigned it (#161). A failed message is NOT marked: it never left the
       inbox, so a replay slot spent on it would orphan it forever (#158). It
       spends one of ``MAX_FILING_ATTEMPTS`` instead, and the attempt that
       empties that budget IS marked -- receipted ``RETRY_EXHAUSTED``, left in
       the inbox, and no longer holding the stream (#158 round 2).
    8. WHILE PAGES REMAIN, FETCH THE NEXT ONE AND REPEAT FROM 3, replaying the
       SAME cursor with the continuation handle the page reported. A round that
       stopped at the first page would leave the rest of the change feed
       unfetched and the cursor unmoved forever, because nothing above this
       function re-enters on a partial state -- the stream would stall at page
       one while every summary looked like an ordinary partial read. There is
       NO page cap: a cap is a silent truncation of exactly the backlog paging
       exists to drain. A round that runs past ``_PAGE_WARN_THRESHOLD`` pages
       logs once and keeps going. It DOES stop opening NEW pages once the round
       has processed ``UNCOMMITTED_WINDOW_CAP`` messages -- see
       ``_drain_bound_reached``. That is backpressure, not truncation: no cursor
       is stored, so the remainder is re-offered on the next poll.
    9. Store the new cursor only if the FINAL page carried one AND no message
       in the round failed with retries still left, and store it exactly once
       for the round. A round carrying such a failure re-polls from the old
       position next time, which is what offers the failed message again; the
       messages that applied are suppressed by the replay guard on that
       re-read. A round whose failures have all exhausted their retries stores
       its cursor, because nothing in it can be usefully offered again.
    10. Return the counts accumulated across every page.

    Args:
        context: The provider, scope, taxonomy, state, stream, and injected
            classifier for this round.

    Returns:
        A ``RoundSummary``. Counts and a container id; never message content.

    Raises:
        ProviderError: The change-feed POLL failed for a reason that is not a
            refused token. Not caught here: a fetch that failed reported
            nothing, so there is nothing to process and nothing to store. A
            ProviderError from filing an individual message does NOT reach
            here -- it is contained per message and receipted as FAILED.
        OSError: A ledger write failed. Never swallowed, and it is the ONE
            carve-out in the per-message boundary (``_apply``) -- a mutation
            without its receipt breaks the audit invariant the kit is sold on.
    """
    if context.learned_rules is None:
        context = context.model_copy(update={"learned_rules": learned.load_rules()})
    # MINTED ONCE, HERE, AND CARRIED ON EVERY RECEIPT THIS ROUND WRITES (#66).
    # Once per ROUND rather than once per message is the whole point: the id is
    # what groups a paged round's filings into one reversible unit, and a
    # per-message id would give `undo --run` nothing to select on. Minted only
    # when the caller did not supply one, so a caller that wants to name its own
    # run (a test, a future caller replaying a round) is honoured.
    if not context.run_id:
        context = context.model_copy(update={"run_id": uuid.uuid4().hex})
    cursor = context.state.cursor_for(context.container_id)
    token = cursor.token if cursor is not None else None
    containers = _container_index(context.provider)
    results: list[_MessageResult] = []
    # THE CONTAINER RIDES ON EVERY POLL, NOT JUST THE CURSOR LOOKUP (#143).
    # ``cursor_for`` above is keyed by container and always was; the POLL was
    # not, and the two disagreeing is the defect. On Gmail the omission is
    # invisible -- ``container_id`` is ignored and the mailbox-wide feed is the
    # only feed. On Graph it is the whole bug: there is no mailbox-wide delta,
    # so on a cold start the container SELECTS THE PATH, and no container means
    # ``me/mailFolders/inbox/messages/delta``. Every per-folder stream
    # therefore read the inbox, processed it once per watched folder, never
    # read the folder it was dispatched for, and filed the inbox's delta link
    # under that folder's cursor key.
    changes = context.provider.poll_changes(token, container_id=context.container_id)
    while True:
        # SUMMED ACROSS PAGES, not read off the last one (#57): a 404 shed on
        # page one is as real as one on page three, and the page carrying the
        # cursor is routinely the quiet one. One append per fetch, so the
        # list's LENGTH is also this round's page count.
        context.shed.append(changes.skipped_missing)
        if changes.resync_required:
            return _resync(context, results)
        results.extend(_process_page(context, changes, containers))
        if not changes.has_more:
            break
        if _drain_bound_reached(context, results, len(context.shed)):
            break
        # The SAME container as the first page. A round that named a stream on
        # page one and dropped it on page two would read one folder's opening
        # page and another folder's continuations, then settle a cursor over
        # the mixture. A live page token already encodes its own stream, so
        # passing the container here is a redundancy the adapter ignores --
        # which is exactly what makes it safe, and cheaper than a reader having
        # to prove the two calls agree.
        changes = context.provider.poll_changes(
            token,
            page_token=changes.next_page_token,
            container_id=context.container_id,
        )
        # ``len(context.shed)`` is the count of pages ALREADY appended, and the
        # page just fetched above is not one of them -- so this fires on the
        # round about to process page ``_PAGE_WARN_THRESHOLD + 1``, exactly as
        # the retired ``pages`` counter did.
        if len(context.shed) == _PAGE_WARN_THRESHOLD:
            # Counts and a stream id, never a message: the same rule
            # ``RoundSummary`` enforces structurally applies to the log line
            # that reports the same round. ONCE per round, on the crossing
            # rather than per page, so a genuine backlog does not turn one
            # condition into thousands of identical lines.
            logger.warning(
                f"WATCH: round is running long container_id={context.container_id} "
                f"pages={_PAGE_WARN_THRESHOLD + 1} messages={len(results)} "
                f"threshold={_PAGE_WARN_THRESHOLD} "
                "(draining to exhaustion -- a page cap here would silently "
                "truncate the backlog paging exists to clear)"
            )
    stored = _settle_cursor(context, changes, results)
    return _summary(
        context,
        results,
        state=RoundState.COMMITTED if stored else RoundState.PAGED,
        cursor_stored=stored,
        resynced=False,
    )


MAX_FILING_ATTEMPTS: Final = 5
"""How many rounds a message may fail to file before the stream moves past it.

THE BOUND ON #158'S WITHHOLDING, and the retry is incomplete without it. A
FAILED filing keeps the round's cursor so the next round is offered the message
again -- correct for the TRANSIENT refusal that rule was written for (a 412, a
throttle, a folder briefly locked), and catastrophic for a PERMANENT one. A
message that can never be filed pins the cursor for the life of the deployment:
every round re-reads the entire window from the same position, the mailbox keeps
growing past it, and once that growth exceeds ``SEEN_LIMIT``'s carryover the
already-filed messages beyond the window are offered as new and RE-FILED.
Observed applied-per-round on the probe: 0, then 300, then 900, then 1500 --
unbounded provider writes and unbounded ledger rows, from one stuck message.

FIVE BECAUSE THE FAILURES THIS DEFENDS SEPARATE CLEANLY AT ABOUT THAT MANY. A
transient refusal clears within a round or two on both providers; nothing
observed here recovers on the fifth attempt but not the second. A larger number
buys a longer window in which the cursor is pinned and the re-file cascade is
loading, and the cost of being wrong in the other direction is one message left
in the inbox with a receipt saying so -- visible, and recoverable by hand.

The count lives in ``WatchState.attempts`` and is PERSISTED, so a restart
between rounds does not hand the message a fresh budget.

A STATE RESET DOES RE-ARM IT, AND THAT IS THE RULING (#185). ``WatchState.load``
starts fresh on a corrupt file, which discards ``attempts`` with everything
else, so an already-abandoned message comes back at attempt 0 and may pin the
cursor for one more bounded cycle. It is not persisted around that reset because
the only place to read the counts from is the file already judged untrustworthy,
and refusing the retry instead abandons mail that may now file on the strength of
a count from that same file. The re-arm is bounded, receipted, and loses nothing;
``state._fresh_start_line`` is what stops it being silent.

A WINDOW EVICTION DOES NOT, AND THAT IS THE SECOND HALF OF THE RULING (#185
point 2). The bound was reachable a second way, and that way had no ruling
behind it at all: ``_exhaust`` recorded the abandonment with ``mark_seen``,
whose first act is to prune the attempts row, so the only surviving record was
a slot in the bounded FIFO replay window -- and the abandoned id is the OLDEST
entry there by construction. ``SEEN_LIMIT`` ordinary messages later it was
evicted, the re-offer was indistinguishable from a message never processed, and
the message drew a COMPLETE second budget: a second ``retry_exhausted`` row and
four more rounds of a pinned cursor, repeating on a period set by mailbox volume
for the life of the deployment. Measured: 800 ids on a normal mailbox, 510 on a
quiet one, or ONE page of ``SEEN_LIMIT`` + 1.

The reset ruling explicitly does NOT extend to it. That ruling rests on the
counts living inside an object being thrown away as untrustworthy with no second
source; on eviction nothing is thrown away, the state file is valid throughout,
and the count was deleted by the connector's own bookkeeping. "At most one more
bounded cycle" does not describe a cycle that recurs forever either. So
abandonment now lives in ``WatchState.exhausted`` -- persisted, bounded by
``state.EXHAUSTED_LIMIT``, and read by ``_process_message`` before it classifies
-- and a corrupt-state reset is the only thing that re-arms this budget.

ABANDONMENT IS THEREFORE PERMANENT, WHICH IS A REAL BEHAVIOUR CHANGE (#185.2
round 5). Before the record existed, the window eviction above was the de-facto
recovery path for a message whose refusal later CLEARED -- a quota that reset, a
label that was recreated, a folder that was unlocked, a 412 that stopped. It was
never designed as one, it recurred on a schedule nobody chose, and it cost a
full second budget every time; closing it is correct. But it did recover such a
message, and nothing does now: an abandoned id is skipped by
``_process_message`` before classification for as long as the state file lives,
even when the provider would file it happily today. The message stays in the
inbox with its ``retry_exhausted`` receipt, the round counts it
``skipped_already_seen``, and the summary reports healthy.

That is the deliberate trade -- a bounded, receipted, visible non-recovery beats
an unbounded re-file cascade recurring forever -- and both ways out belong to
the operator: file the message by hand, or reset the watch state, which is the
corrupt-state door ``state.WatchState.load`` documents. The skip is logged at
DEBUG once per stream and message so the round is never SILENTLY healthy about
it, which was the only part of this that was not a decision.

A STATE FILE FROM BEFORE THE RECORD RE-ARMS ONCE, ANNOUNCED. See
``state._announce_exhausted_introduced``: an upgraded deployment has no verdicts
to inherit and none can be honestly invented from the replay window, so a
message abandoned before the upgrade may draw one more bounded budget and the
INFO line on load is what connects the two.
"""

RETRY_EXHAUSTED: Final = "retry_exhausted"
"""The ledger error code for a filing abandoned at ``MAX_FILING_ATTEMPTS``.

A CODE THIS MODULE OWNS, and the only ledger error code here that is not an
exception's type name. The type-name rule is unchanged and is not being bent:
it exists so provider prose -- which can quote the subject it refused -- never
reaches the receipt, and this string carries no message content at all. The
reason it cannot BE a type name is that no exception describes this event. The
last attempt's own refusal has its own type, and writing that would say
"ProviderError" for the fifth time and lose the one fact the row exists to
record: that the connector stopped trying, and the mail is still in the inbox.
"""

_PAGE_WARN_THRESHOLD: Final = 10
"""Pages in one round past which the round says so, ONCE, and keeps going.

Not a cap and deliberately not one. A cap would stop fetching with pages still
outstanding, which is the mid-page stall this module's cursor rule exists to
make impossible; the honest response to an unusually deep backlog is to drain
it and to leave a line saying the round was deep. An operator reading that line
learns the stream is behind -- which is the fact -- rather than inheriting a
round that quietly stopped short.

The MESSAGE bound in ``_drain_bound_reached`` is a different thing and does not
contradict this. That one stops opening new pages, stores no cursor, and is
re-offered next poll; this one is about never stopping while a cursor could be
mistaken for a complete read.
"""


def _drain_bound_reached(
    context: RoundContext,
    results: list[_MessageResult],
    pages: int,
) -> bool:
    """Has this round processed enough messages to stop opening NEW pages? (#82)

    THE MID-PASS HALF OF #194'S CAP. ``UNCOMMITTED_WINDOW_CAP`` bounds what the
    replay window carries across a pass BOUNDARY, and ``begin_pass`` is the only
    place it can bind: ``mark_seen`` may never evict an id the current pass
    recorded (#161b), because a page larger than the bound that evicts its own
    marks replays in full. So mid-pass occupancy is cap + (ids this pass marked),
    and on a stuck stream that second term was the whole re-offered backlog --
    ``loop.run_pass`` opens ONE pass per poll and this round drains to
    exhaustion inside it. Measured 6,000 and climbing against a cap of 2,000.

    The fix binds the DRAIN rather than the carve-out, because the carve-out is
    load-bearing and this is not: once the round has processed the cap's worth of
    messages, the rest of the backlog waits for the next poll. Nothing is lost.
    ``changes.has_more`` is still true when this returns, so ``ChangeSet`` is
    carrying no cursor, ``_store_cursor`` stores nothing, and the round reports
    ``RoundState.PAGED`` -- the partial-read ending that already existed. The
    next poll re-polls from the same position and the replay guard suppresses
    everything already filed. Backpressure, not truncation.

    Reusing ``UNCOMMITTED_WINDOW_CAP`` rather than inventing a drain constant is
    deliberate: the number IS the window's capacity, and a second number would
    let the two drift into a bound that no longer bounds the thing it names.
    """
    if len(results) < UNCOMMITTED_WINDOW_CAP:
        return False
    # Counts and a stream id, never a message. ONCE per round -- the loop breaks
    # immediately after, so this cannot repeat within a round.
    logger.info(
        f"WATCH: drain bound reached container_id={context.container_id} "
        f"messages={len(results)} cap={UNCOMMITTED_WINDOW_CAP} pages={pages} "
        "(pausing the drain with pages outstanding -- no cursor is stored, so "
        "the remainder is re-offered on the next poll)"
    )
    return True


def _resync(context: RoundContext, results: list[_MessageResult]) -> RoundSummary:
    """Drop the refused cursor and end the round. NOT an error path.

    Resume tokens have no lifetime guarantee on either provider, so expiry is
    ordinary operation on a quiet mailbox -- a watcher that raised here would
    raise on a perfectly healthy mailbox (``watchstate.resync_required``). The
    replay window is deliberately KEPT: a resync re-reads everything the
    stream covers, which is precisely when the guard earns its keep.

    ``results`` carries whatever earlier pages of this round already earned,
    and is empty on the ordinary first-poll refusal. A resync demanded on page
    three does not un-file the messages on pages one and two: those provider
    writes landed and their ledger rows exist, so the summary reports them.
    Reporting zeros instead would put the round's own account at odds with the
    receipt trail the kit is sold on. NO cursor is stored either way.
    """
    context.state.resync_required(context.container_id)
    # NO PLANNED FILINGS ON A RESYNC, and that is not an omission: the provider
    # refused the position this round was reading from, so whatever a dry pass
    # decided describes a stream state that no longer exists. Printing it would
    # show the operator a preview of mail the next poll may not offer. Cleared
    # rather than filtered at the summary, so the accumulator and what the
    # summary reports cannot disagree.
    context.plans.clear()
    return _summary(context, results, state=RoundState.RESYNCED, cursor_stored=False, resynced=True)


def _process_page(
    context: RoundContext,
    changes: ChangeSet,
    containers: dict[str, Container],
) -> list[_MessageResult]:
    """Run every message on ONE page through the gates. Settles no cursor.

    Cursor settlement is the ROUND's decision, not a page's, and this function
    not owning it is what makes storing one mid-page unrepresentable here: only
    the loop that has seen the final page can call ``_store_cursor``.

    A dry round's planned filings accumulate on ``context.plans`` rather than
    being returned, for the same reason ``results`` accumulates in the caller:
    the preview is the ROUND's product, not a page's, so a per-page list would
    report only the last page of a paged mailbox.
    """
    return [_process_message(context, message, containers) for message in changes.messages]


def _summary(
    context: RoundContext,
    results: list[_MessageResult],
    *,
    state: RoundState,
    cursor_stored: bool,
    resynced: bool,
) -> RoundSummary:
    """Tally one round's per-message outcomes into the counts it reports.

    ONE construction site for ``RoundSummary``, so the committed, partial, and
    resynced endings cannot drift into three different accounts of the same
    table. The tally is a count per outcome rather than a branch tree, which is
    why a new ``_MessageResult`` member is a data change here and not a control
    change (``_DOCS/STANDARDS-python.md`` ## LAW: flat control flow).
    """
    tally = {outcome: results.count(outcome) for outcome in _MessageResult}
    # EXHAUSTED IS A FAILURE IN EVERY COUNT IT APPEARS IN. The member exists to
    # answer one question and one only -- whether the cursor may move
    # (``_settle_cursor``) -- and a summary that reported it separately, or not
    # at all, would let a round abandoning mail look healthier than a round
    # retrying it. #160's exit grammar reads ``failed``, so this addition is
    # what keeps an exhausted-only pass reporting unhealthy.
    failed = tally[_MessageResult.FAILED] + tally[_MessageResult.EXHAUSTED]
    return RoundSummary(
        container_id=context.container_id,
        state=state,
        seen=len(results),
        skipped_out_of_scope=tally[_MessageResult.OUT_OF_SCOPE],
        skipped_unenumerated_container=tally[_MessageResult.UNENUMERATED_CONTAINER],
        skipped_missing=sum(context.shed),
        skipped_already_seen=tally[_MessageResult.ALREADY_SEEN],
        planned=tuple(context.plans),
        classified=tally[_MessageResult.APPLIED] + failed,
        applied=tally[_MessageResult.APPLIED],
        failed=failed,
        cursor_stored=cursor_stored,
        resynced=resynced,
    )


def _settle_cursor(
    context: RoundContext,
    changes: ChangeSet,
    results: list[_MessageResult],
) -> bool:
    """Decide whether this round has earned the right to move the stream.

    THE SECOND HALF OF THE #158 RETRY, and it is the half that makes the first
    half mean anything. Not marking a FAILED message seen permits a retry;
    this is what guarantees the retry is OFFERED one. The cursor is what the
    next round polls from, so committing it moves the stream past a message
    that never left the inbox -- and the replay guard, however permissive,
    never sees an id the provider does not return.

    So a round containing an UNEXHAUSTED failure stores no cursor and re-polls
    from the old position next round. The messages that DID apply are re-read
    and suppressed by the replay guard, which is exactly the case that guard
    exists for: re-reading a message is free, never re-offering one is the mail
    sitting in the inbox forever.

    UNEXHAUSTED IS THE WHOLE QUALIFIER, and #158's round-2 defect is what it
    answers. A withheld cursor with no ceiling means one permanently unfileable
    message pins the stream forever, and the re-read that pin produces
    eventually re-files every message past the replay window. So a failure that
    has spent its budget (``_MessageResult.EXHAUSTED``, see
    ``MAX_FILING_ATTEMPTS``) no longer withholds: it was marked seen, the round
    still reports it in ``failed``, and the stream is allowed to move.

    A MIXED ROUND IS GOVERNED BY THE FRESH FAILURE. One message with retries
    left is reason enough to keep the position, exactly as it would be alone --
    the exhausted message beside it is already guarded by the replay window and
    cannot be re-offered by the re-read.
    """
    # A DRY ROUND COMMITTED NOTHING, SO IT MAY NOT MOVE THE STREAM (#66). This
    # is checked FIRST and unconditionally, ahead of the failure rule, because
    # it does not depend on what the round decided: a preview that stored the
    # position would consume the mail it only looked at, and the real pass the
    # operator runs after reading WOULD CREATE would be offered a stream that
    # had already advanced past it. Nothing else here can express that -- the
    # round's results are all ``PREVIEWED``, which is not a failure, so every
    # other rule below reads it as a clean round and stores.
    if context.dry_run:
        preview.announce_withheld_cursor(context.container_id, len(results))
        return False
    if _MessageResult.FAILED not in results:
        return _store_cursor(context, changes)
    # Counts only. The same rule ``RoundSummary`` enforces structurally.
    logger.debug(
        f"WATCH: cursor withheld, round carried failures container_id={context.container_id} "
        f"failed={results.count(_MessageResult.FAILED)} "
        f"exhausted={results.count(_MessageResult.EXHAUSTED)} messages={len(results)} "
        "(re-polling from the old position is what offers the failed message again)"
    )
    return False


def _store_cursor(context: RoundContext, changes: ChangeSet) -> bool:
    """Store the cursor IF AND ONLY IF the provider returned a final one.

    THIS IS THE MOST IMPORTANT LINE IN THE MODULE. A cursor stored mid-page
    marks a partial read as a completed one: the next round resumes from a
    position claiming everything before it was processed, and every change on
    the pages that were never fetched is gone -- permanently, with no error,
    no exception, and no log line. The mailbox looks healthy, the watcher
    looks healthy, and the mail is simply missing with nothing in the system
    able to say which.

    ``ChangeSet`` already refuses to carry a cursor while pages remain, so the
    mid-page case arrives here as ``cursor is None`` and this function stores
    nothing. That validator and this check are two mechanisms for one rule on
    purpose: the model stops an adapter producing an illegal value, and this
    stops a caller inventing one. Neither constructs, parses, normalises, or
    compares a token -- it is opaque and is replayed byte for byte.
    """
    if changes.cursor is None:
        logger.debug(
            f"WATCH: no cursor stored container_id={context.container_id} "
            f"has_more={changes.has_more} (partial read -- storing here would "
            "silently lose every change on the unfetched pages)"
        )
        return False
    context.state.record_cursor(context.container_id, changes.cursor)
    return True


def _container_index(provider: MailProvider) -> dict[str, Container]:
    """Provider containers by id, fetched ONCE per round and never per message.

    Needed to translate the provider-native ids on ``Message.containers`` into
    the display names and well-known tags ``MailboxScope`` consumes. Fetching
    per message would multiply a per-round read by the page size for an answer
    that does not change inside a round.
    """
    return {container.id: container for container in provider.list_containers()}


def _process_message(
    context: RoundContext,
    message: Message,
    containers: dict[str, Container],
) -> _MessageResult:
    """One message, in the fixed order: scope, replay, classify, write, receipt, seen.

    SCOPE PRECEDES CLASSIFY AND THE ORDER IS NOT NEGOTIABLE. ``classify``
    consumes subject, sender, and snippet, all of which are message content,
    so an out-of-scope message that reached the classifier has already been
    read -- and for a mailbox that may hold privileged material the read is
    the event the client cares about, and it cannot be taken back. Filtering
    afterwards discards a variable, not the read.

    An out-of-scope message is not classified, not filed, not receipted,
    and NOT marked seen. It caused no action, so spending a replay slot on it
    would evict an id that records one.

    NOR IS A FAILED ONE (#158). The replay guard records what left the inbox,
    not what was attempted: a message that was read, receipted FAILED, and is
    still sitting in the inbox must be offered to the next round, and the
    ``_settle_cursor`` gate is what makes sure the provider offers it.

    UNTIL ITS BUDGET RUNS OUT (#158 round 2). The retry above is right for a
    transient refusal and ruinous for a permanent one, so each failure spends
    one of ``MAX_FILING_ATTEMPTS`` and the attempt that empties the budget is
    handled by ``_exhaust``: a ``retry_exhausted`` receipt, a replay mark, and a
    result that still counts as a failure but no longer withholds the cursor.

    AN APPLIED ONE SPENDS UP TO TWO SLOTS, not one. A move that reassigns the
    id gives the same real message a second way back into the watcher, from the
    destination folder's own delta stream. See ``_mark_applied_seen`` (#161a).
    """
    placement = _covered(message, context.scope, containers)
    if placement is not _Placement.COVERED:
        # Container ids only. A log line naming what we refused to read must
        # not quote the thing we refused to read.
        logger.debug(
            f"WATCH: not classified placement={placement} "
            f"message_containers={len(message.containers)}"
        )
        return _REFUSAL_RESULT[placement]
    # THE ABANDONMENT RECORD IS CONSULTED BEFORE THE REPLAY WINDOW (#185.2).
    # The window forgets on a schedule set by mailbox volume -- an abandoned id
    # is its OLDEST entry, so ``SEEN_LIMIT`` ordinary messages evict it and a
    # re-offer looked exactly like a message never processed, drawing a full
    # second ``MAX_FILING_ATTEMPTS``. Checked FIRST because it is the stronger
    # claim of the two and its answer does not expire.
    if context.state.message_exhausted(context.container_id, message.id):
        # THE ROUND MUST NOT BE SILENTLY HEALTHY ABOUT THIS (#185.2 round 5).
        # The skip is permanent until a state reset, so a message whose refusal
        # later cleared is never filed by this watcher again -- it sits in the
        # inbox, the summary counts it as ``skipped_already_seen``, and the
        # round reports healthy. DEBUG rather than louder because the condition
        # is expected and already receipted once, at ``RETRY_EXHAUSTED``; what
        # was missing is any way to answer "why is this message still here?"
        # without reading the ledger. Counts and a stream id only, never
        # content. See ``state.WatchState.load`` for the ruling.
        # Once per (stream, message), not once per round: a permanently
        # abandoned message is offered by every poll, and the same line a
        # thousand times a day is noise an operator learns to filter.
        if context.state.announce_skip_once(context.container_id, message.id):
            logger.debug(
                "WATCH: skipped, filing was abandoned for this message "
                f"container_id={context.container_id} error_code={RETRY_EXHAUSTED} "
                "(the verdict is permanent until the watch state is reset; the message "
                "stays in the inbox even if the provider would now accept it)"
            )
        return _MessageResult.ALREADY_SEEN
    if context.state.seen_message(context.container_id, message.id):
        return _MessageResult.ALREADY_SEEN
    if context.dry_run:
        # THE PREVIEW BRANCH RETURNS BEFORE ``_apply``, NOT INSIDE IT (#66).
        # ``_apply`` is the mutation seam: it begins a write-ahead intent,
        # calls the provider, writes a receipt, writes a trace row, and may
        # compose a draft. A dry flag threaded THROUGH it would have to switch
        # each of those off individually, and the next effect added there would
        # be live in a preview until somebody remembered to add the sixth
        # branch. Not entering the function is what makes the guarantee hold
        # for effects that do not exist yet.
        preview.plan_only(
            message, context.taxonomy, context.classifier, context.learned_rules, context.plans
        )
        return _MessageResult.PREVIEWED
    folder_before = _folder_names(message, containers)
    outcome = _apply(context, message, folder_before=folder_before)
    result = outcome.result
    # ONLY AN APPLIED RESULT SPENDS A REPLAY SLOT (#158). Marked AFTER the write
    # and its receipt, never before: a crash between marking and writing would
    # suppress the retry on the next round. A FAILED result is the same argument
    # taken one step further -- the message did NOT leave the inbox, so marking
    # it seen orphans it forever: the next round reports it as
    # ``skipped_already_seen``, the mail sits in the inbox, and the watcher
    # reports healthy. Re-attempting a filing is a no-op move when it succeeds;
    # never attempting it again is the mail never leaving.
    if result is _MessageResult.APPLIED:
        _mark_applied_seen(context, message, outcome, containers)
        return result
    return _count_attempt(context, message)


def _count_attempt(context: RoundContext, message: Message) -> _MessageResult:
    """Spend one retry from this message's budget. Returns the honest result.

    Reached only on a FAILED apply, so the increment and the ceiling test are
    one step and cannot drift apart: a caller that incremented without testing
    would restore the unbounded pin, and one that tested without incrementing
    would never reach the ceiling at all.

    Below the ceiling the answer is unchanged from #158 -- FAILED, not seen,
    cursor withheld, offered again next round. AT the ceiling the message is
    exhausted; see ``_exhaust`` for what that costs and what it buys.
    """
    attempts = context.state.record_attempt(context.container_id, message.id)
    if attempts < MAX_FILING_ATTEMPTS:
        return _MessageResult.FAILED
    return _exhaust(context, message, attempts)


def _exhaust(context: RoundContext, message: Message, attempts: int) -> _MessageResult:
    """Stop retrying this message. Receipt it, guard it, let the stream move.

    THE MAIL STAYS IN THE INBOX, UNFILED, and that is the deliberate outcome
    rather than a shortfall of this function. Nothing here can file a message
    the provider has refused five times, and the alternatives are worse: filing
    it somewhere else invents a destination the classifier never chose, and
    deleting it is not a capability this connector has or wants. Leaving it
    where the owner can see it is the honest end state, and the
    ``retry_exhausted`` receipt is the record that says why it is still there.

    FOUR EFFECTS, and each one is load-bearing:

    1. A ledger row with ``RETRY_EXHAUSTED``. Without it the connector's giving
       up is invisible -- the round's ``failed`` count says a message failed,
       and every previous round said the same thing, so nothing distinguishes
       "still trying" from "stopped trying."
    2. An entry in ``WatchState.exhausted``. This is the DURABLE half of the
       verdict and it is why the replay mark below is not enough on its own: the
       window is bounded and evicts oldest-first, an abandoned id is its oldest
       entry by construction, and ``SEEN_LIMIT`` ordinary messages later a
       re-offer drew a complete second ``MAX_FILING_ATTEMPTS`` (#185 point 2).
       ``_process_message`` reads this before it classifies.
    3. A replay mark. This is what stops the message burning a classification
       and a provider call on every future round, and -- because
       ``WatchState.mark_seen`` prunes the attempt entry -- what keeps the retry
       table bounded by the failing set.
    4. An ``EXHAUSTED`` result, so ``_settle_cursor`` may store the cursor. The
       pin is released here and nowhere else.

    Effects 2 and 3 are BOTH made, rather than one replacing the other, because
    they answer different questions. The record says "never attempt this again"
    and must not expire; the mark keeps the id inside the replay window's own
    accounting for as long as that window holds it, so nothing downstream sees a
    message that is simultaneously unseen and unattemptable.

    The message is NOT counted as applied and its result still lands in
    ``RoundSummary.failed``, so the pass reports unhealthy exactly as it did on
    the four rounds before it.
    """
    _receipt(context, message.id, _UNCLASSIFIED, refusal=receipts.Refusal(code=RETRY_EXHAUSTED))
    # BEFORE ``mark_seen``, which prunes the attempts row and is therefore the
    # step that leaves the window as the only record. Recording the verdict
    # first means no ordering of these two lines can produce a moment where the
    # message is abandoned and nothing durable says so.
    context.state.mark_exhausted(context.container_id, message.id)
    context.state.mark_seen(context.container_id, message.id)
    # Counts and a stream id, never message content -- the same rule the
    # summary enforces structurally. ERROR rather than WARNING because a
    # message the owner will now never see filed is exactly what an operator
    # reading the log is there to find.
    logger.error(
        f"WATCH: filing abandoned after the attempt bound container_id={context.container_id} "
        f"attempts={attempts} limit={MAX_FILING_ATTEMPTS} error_code={RETRY_EXHAUSTED} "
        "(the message stays in the inbox, unfiled; the ledger row is the record)"
    )
    return _MessageResult.EXHAUSTED


def _mark_applied_seen(
    context: RoundContext,
    message: Message,
    outcome: _ApplyOutcome,
    containers: dict[str, Container],
) -> None:
    """Spend a replay slot on EVERY id the filed message can come back under.

    THE REFILE LOOP (#161a), and it is why one mark is not enough. Graph's
    ``/move`` is copy-then-delete: the message that exists after the write
    carries a NEW id, and its destination folder is its own delta stream under
    the default watch scope. So the message this round just filed re-enters the
    watcher on the next round -- from the folder it was moved INTO, under an id
    the replay window has never been shown. It is filed again, minted again, and
    the observed result is ``AAA`` -> ``AAA-MOVED`` -> ``AAA-MOVED-MOVED``,
    unbounded provider writes and unbounded ledger rows, forever.

    Marking the PRE-move id alone cannot close that, because the provider never
    offers the pre-move id again -- it no longer exists. Marking the post-move id
    alone is worse: the pre-move id is what a replay of THIS round's own page
    carries, which is exactly the case the window was built for. Both ids are
    the same real message, so both spend a slot.

    ``post_move_id`` is ``None`` on an honest Graph move receipt -- the write
    landed and the adapter could not report the new id (a9e1167) -- and equals
    the original on a provider that never reassigns. Neither case is an error
    and neither invents an id.

    A STABLE ID STILL NEEDS THE DESTINATION MARK (#81). The equal-ids case used
    to return early on the reasoning that Gmail's ids do not change, so the mark
    above had already covered it -- but that reasoning is about Gmail's ONE
    stream, not about stable ids. The two properties are independent: a
    per-folder provider whose ids survive a move offers the SAME id back through
    the destination folder's own window, which the source mark never reached, so
    the early return left it unguarded and reopened #161a/#173 for that shape.
    Marking the id in both windows is correct for every provider -- ``mark_seen``
    is idempotent, so the mailbox-wide case where the two keys resolve to the
    same window spends the same one slot it always did.

    AND THE POST-MOVE MARK BELONGS IN THE DESTINATION STREAM (#173). Marking it
    in THIS stream's window closed nothing on Graph: each folder is its own
    delta stream with its own window, so the id the destination folder offers on
    its next poll was never shown to the window that poll consults. The message
    was refiled on every round -- the same unbounded loop #161a described,
    reached by the other door.

    THE DESTINATION COMES FROM THE PROVIDER, NOT FROM A NAME (#173 round 3).
    The adapter POSTs a container id to move into and reports that same id back
    on its result, so the destination is a FACT of the write rather than a
    lookup that can disagree with it. This started as a name match against the
    round's container index and the name match could not survive contact with a
    real install: the adapter files into ``folder_prefix + folder`` and Graph
    lists that prefixed string as the container's displayName, while the
    classification names the RAW taxonomy folder. Every install shipping a
    prefix (``rTech/`` is the default) resolved NOTHING, fell back to the source
    window, and refiled forever -- the fix looked correct and covered only the
    empty-prefix case. Graph additionally permits one displayName under two
    parents, where a name match takes whichever the index happens to yield
    first.

    WHEN THE PROVIDER REPORTS NO DESTINATION the name match still runs
    (``_destination_container_id``), and then the current stream. Both are right
    for the cases that reach them: a Gmail mailbox is ONE stream, so the source
    window IS the window the id returns through, and a listing that carries
    neither an id nor a matching name gives nothing to mark against -- inventing
    a key would put the mark in a window nothing reads.

    ON A PER-FOLDER STREAM THAT LAST FALLBACK IS A WARNING (#194). "Nothing to
    mark against" is a correct answer on Gmail and a REFILE RISK on Graph, and
    the two were indistinguishable because both were silent. Marking in the
    source window on a per-folder provider puts the id in a window the
    destination folder's own delta poll never consults, which reopens the exact
    #161a/#173 loop -- and it reopens it quietly, because the round then reports
    ``applied`` and the cursor commits, so nothing else in this module notices.
    The shipped Graph adapter reports its id, so the path is dormant; a warning
    is what makes it stop being dormant AND silent at the same time, which is
    the combination that made #173 cost three rounds to find.
    ``_warn_unresolved_destination`` owns the line and the bound.

    THE GUARD IS ON THE ANSWER, NOT ON ``None`` (#194 round 5), which is why it
    sits ABOVE the ``post_move_id`` return and reads the OUTCOME's two fields
    rather than the resolved id. Round 4 warned only when the name match ALSO
    came back None, and that left three doors open on the same harm:

    - A NAME MATCH ONTO THE WRONG CONTAINER answers non-None, so the guard never
      fired. ``_destination_container_id`` is first-match-wins over display
      names and this module's own docstring records that Graph permits one
      displayName under two parents, so a decoy folder silently captures the
      mark -- the full #161a/#173 refile loop with the warning structurally
      unreachable. The adapter reporting no id is the whole risk; whether a name
      guess then produced SOMETHING is not evidence it produced the right thing.
    - AN HONEST MOVE RECEIPT WITH NO NEW ID returns at the line below, before
      round 4's check ran, and it is the shipped Graph adapter's own documented
      no-id path (``providers/outlook/provider.py``, "reported no id, receipt id
      UNKNOWN"). No destination mark is made AT ALL, which is the same unguarded
      window with even less in it -- and that adapter reports its DESTINATION on
      the same receipt, so keying on the destination alone cannot see it either.
      Hence two conditions: no destination reported, or no id to mark.
    - The bound was keyed on the folder NAME alone, so a second source stream
      hitting the identical condition for the same taxonomy folder said nothing.

    So the question asked is "did the write give this stream what it needs to
    guard the destination?", which is the fact that determines the risk, and it
    is asked before any early return.

    Args:
        context: This round's stream and state.
        message: The message that was just filed.
        outcome: The apply result, carrying the id the write reported (``None``
            when it reported none), the container it filed into (``None`` from
            an adapter that does not report one), and the folder name. NONE is
            derived here -- ``_apply`` reads them off the one site that has them.
        containers: This round's container index, used ONLY by the name-match
            fallback for adapters that report no destination container id.
    """
    context.state.mark_seen(context.container_id, message.id)
    post_move_id = outcome.post_move_id
    if outcome.destination_id is None or post_move_id is None:
        _warn_unresolved_destination(context, outcome.destination_folder)
    if post_move_id is None:
        return
    destination_id = outcome.destination_id or _destination_container_id(
        outcome.destination_folder, containers
    )
    context.state.mark_seen(
        context.container_id if destination_id is None else destination_id,
        post_move_id,
    )


def _destination_container_id(folder: str, containers: dict[str, Container]) -> str | None:
    """The container id whose delta stream the filed message now belongs to.

    FALLBACK ONLY, for an adapter that does not report the container id it
    wrote into; ``_mark_applied_seen`` prefers the reported id. It stays
    name-based on purpose: a taxonomy folder is a NAME, the classification
    names one, and an adapter that reports no id has given this function
    nothing else to match on. It is deliberately NOT made prefix-aware --
    reconstructing ``folder_prefix + folder`` here would be a second, independent
    guess at the string the adapter actually used, and the whole lesson of #173
    round 3 is that a second construction of that string drifts from the write.
    The one adapter with a prefix (Graph) now reports its id, so the prefix
    never reaches this path.

    Matching is case-folded because a display name's case is the mailbox
    owner's, not the taxonomy's, and a container named ``business-action`` is
    the same folder as ``Business-Action``.

    ``None`` when nothing matches, and that is an ordinary answer rather than
    an error: see ``_mark_applied_seen`` for why the caller then marks in its
    own stream instead.
    """
    if not folder:
        return None
    wanted = folder.casefold()
    for container in containers.values():
        if container.name.casefold() == wanted:
            return container.id
    return None


_WARNED_UNRESOLVED_DESTINATIONS: Final[set[tuple[str, str]]] = set()
"""``(source container id, destination folder)`` pairs already warned about.

PROCESS-SCOPED AND DELIBERATELY UNBOUNDED-BY-DESIGN-BUT-BOUNDED-IN-FACT: it can
only ever hold a container id crossed with a taxonomy folder name, and both are
configuration -- a mailbox has a bounded set of folders and a taxonomy has a
handful of names. This is not a cache of anything mail-derived and never holds a
message id, an address, or a subject.

The alternative was a per-message warning, which is the shape #189 spent a round
removing from the loader: a real occurrence would put one WARNING per message on
every round forever, and an operator who cannot read the log cannot act on it.

THE KEY CARRIES THE SOURCE STREAM SINCE #194 ROUND 5. It was the folder name
alone, and the docstring's own claim was that the diagnosis is "the folder plus
the adapter" -- the adapter half is what a bare folder name drops. On Graph every
folder is its own stream and one taxonomy folder is the destination of writes
from many of them, so a second source stream hitting the identical condition was
silenced by the first stream's line and the operator saw one report for a defect
affecting several streams. Keyed on the pair, each affected stream says it once.

``_reset_destination_warnings`` clears it; tests own both ends of it that way.
"""


def _reset_destination_warnings() -> None:
    """Forget which destination folders have been warned about. TESTS ONLY.

    Module state that survives a test is a test that passes because an earlier
    one already fired the line, or fails because it did. This is the seam that
    lets a test own both ends rather than reaching into the set by name, and it
    is why the set is not simply cleared inline in the suite.
    """
    _WARNED_UNRESOLVED_DESTINATIONS.clear()


def _warn_unresolved_destination(context: RoundContext, folder: str) -> None:
    """Say once per stream and folder that the adapter reported no destination.

    #194's second clause. ``_mark_applied_seen`` has just been handed a filing
    result that does not say WHICH container the write landed in, so the replay
    mark for the post-move id is placed by a guess or not placed at all. Whether
    that is fine or a defect depends entirely on the provider's stream shape,
    and nothing downstream can tell the two apart, which is why the distinction
    is drawn here:

    A MAILBOX-WIDE STREAM (``container_id is None``, which is Gmail) has exactly
    one window, so the source window IS the window the id comes back through.
    There is no risk and nothing to say, and saying it anyway would put a
    WARNING in front of every Gmail operator for correct behaviour.

    A PER-FOLDER STREAM (any ``container_id``, which is Graph) polls the
    destination folder separately with its own window, and that window has not
    been shown this id. The message re-enters as new, is filed again, and mints
    another id -- the unbounded #161a/#173 refile loop, with the round reporting
    ``applied`` and the cursor committing normally, so no other signal in this
    module fires. The cap warning added for #194's first clause cannot cover it
    either: nothing fails, so the uncommitted window never grows.

    THE TRIGGER IS THE RECEIPT'S SILENCE, NOT A ``None`` FURTHER DOWN (round 5).
    The caller asks this before resolving anything, because all three unguarded
    shapes share one cause and only one of them survived to round 4's check: a
    name match onto a decoy container answers non-None and is WRONG, a move
    receipt with no new id returns before any resolution happens, and only "no id
    and no name matched" reached the old guard. What they have in common is that
    the write did not hand this stream what it needs to guard the destination --
    either the container it wrote into, or an id to mark there.

    THE LINE IS SPENT ON A REPORTED DESTINATION TOO, when the receipt carried no
    post-move id, and that is not a false alarm: the shipped Graph adapter emits
    exactly that pair, the destination window ends the round with nothing in it,
    and the adapter's own WARNING is about the RECEIPT id rather than the replay
    window, so nothing else tells an operator that stream is unguarded.

    WARNING rather than ERROR because filing itself succeeded and the mail is
    where the owner expects it; what is at risk is a duplicate write on the next
    round. Names the folder and the source stream because the folder plus the
    adapter is the whole diagnosis, and both are configuration rather than mail
    content, so they are safe to log in full. No message id, no subject, no
    sender.

    Args:
        context: This round's stream. ``container_id`` is the discriminator and
            half the once-bound's key.
        folder: The destination folder the classification named. Empty when the
            outcome carried none, which is its own unresolvable case and still
            worth one line.
    """
    container_id = context.container_id
    if container_id is None:
        return
    if (container_id, folder) in _WARNED_UNRESOLVED_DESTINATIONS:
        return
    _WARNED_UNRESOLVED_DESTINATIONS.add((container_id, folder))
    logger.warning(
        f"WATCH: filed message's write reported no destination container, so the replay mark "
        f"is a guess or was not made folder={folder or '-'} source_container_id="
        f"{container_id} (this stream polls per folder, so the destination folder's "
        "own poll may refile the message; the adapter reported no destination id, and a "
        "container-name match can resolve nothing or resolve the wrong folder)"
    )


class _Placement(StrEnum):
    """WHY the watcher may or may not read a message, not merely whether.

    The boolean this replaces answered two questions with one word (#198). A
    refusal because the installed scope excludes a folder and a refusal because
    the container listing never carried the id are the same DECISION -- do not
    read -- and different DIAGNOSES, and the caller needs the diagnosis to tally
    the round honestly.
    """

    COVERED = "covered"
    OUT_OF_SCOPE = "out_of_scope"
    UNENUMERATED = "unenumerated_container"


_REFUSAL_RESULT: Final[dict[_Placement, _MessageResult]] = {
    _Placement.OUT_OF_SCOPE: _MessageResult.OUT_OF_SCOPE,
    _Placement.UNENUMERATED: _MessageResult.UNENUMERATED_CONTAINER,
}
"""One refusal reason to one tally bucket. A table, not a branch."""


def _covered(
    message: Message,
    scope: MailboxScope,
    containers: dict[str, Container],
) -> _Placement:
    """Whether the watcher may read this message, and WHY NOT when it may not.

    ``covers_all`` rather than ``covers`` because a Gmail message carries many
    containers at once, and the question is whether the watcher may read a
    message holding ALL of them -- any-exclusion-wins, per ``covers_all``. On
    Graph the message carries exactly one, so the same call is the same
    question with one element. Scope is never re-derived from
    ``MailboxScope``'s raw fields here: a second implementation of that
    decision is a second chance to read a privileged folder.

    AN ID THE PROVIDER DID NOT ENUMERATE REFUSES THE MESSAGE OUTRIGHT, and
    that is a correction rather than a caution. The id used to resolve to
    ITSELF as the folder name with no well-known tag, and the scope rules then
    answered a question about a name no folder has -- so an ``all_except``
    scope excluding "Client Matters" admitted a message sitting in exactly
    that folder whenever the container listing did not carry its id. A listing
    that is stale, truncated, filtered, or simply raced by a folder created
    since is ordinary provider behaviour, so the gap is not exotic. The right
    answer to "I cannot tell where this message is" is not permission: an
    unreadable placement and an excluded one are indistinguishable from here,
    and only one of the two mistakes reads privileged mail.

    IT IS A DIFFERENT ANSWER, THOUGH, AND THAT IS #198. The two refusals used
    to return the same ``False`` and were counted in one bucket, so a round
    that could not tell where the mail was reported the scope had refused it.
    Every signal downstream then named the scope as the cause and sent the
    operator to edit folder names that were already right -- and under
    ``all_folders`` there is no folder list to edit at all. The decision is
    unchanged; only the reason now survives the return.

    An empty container tuple reaches ``covers_all`` as an empty sequence,
    which is NOT covered for the same reason -- a message we know nothing
    about must not resolve to permission. It is reported as OUT_OF_SCOPE
    rather than unenumerated: no id went unresolved, the message named no
    placement at all, and the scope rules are what refuse it.

    Args:
        message: The message whose placement is in question. Never read for
            content here.
        scope: The installed scope. Consulted, never re-derived.
        containers: This round's container listing, by provider-native id.

    Returns:
        ``_Placement.COVERED`` when the watcher may read the message,
        ``_Placement.UNENUMERATED`` when a container id was absent from the
        listing, and ``_Placement.OUT_OF_SCOPE`` when every id resolved and
        the installed scope refuses the placement.
    """
    known = [containers.get(identifier) for identifier in message.containers]
    if any(container is None for container in known):
        # Counts only, never an id's contents and never the message: a line
        # about mail we refused to read must not quote it.
        logger.debug(
            f"WATCH: container not enumerated, message refused "
            f"message_containers={len(message.containers)} "
            f"unknown={sum(1 for container in known if container is None)} "
            "(run list-folders: the scope is not what refused this)"
        )
        return _Placement.UNENUMERATED
    names = [container.name for container in known if container is not None]
    tags = [container.well_known for container in known if container is not None]
    if scope.covers_all(names, well_known=tags):
        return _Placement.COVERED
    return _Placement.OUT_OF_SCOPE


def _folder_names(message: Message, containers: dict[str, Container]) -> str:
    """The folder(s) the message sat in when the round read it, names for the trace.

    An unknown container is reported by its id rather than dropped: a
    container the provider did not enumerate is still a place the message was,
    and silently omitting it would make the trace claim the message was
    somewhere it was not.

    THIS IS NOT THE STANCE ``_covered`` TAKES, and the divergence is the
    point. There an unknown id refuses the message, because a permission
    decision made on a placement we cannot resolve is a decision made about
    the wrong folder. Here nothing is being permitted -- a trace row is
    diagnostics, written only for a message already admitted -- so the honest
    report of an unresolved id is the id, and falling back to it costs no
    access. A reporting fallback and a permission fallback are different
    things and only one of them can read privileged mail.
    """
    known = [containers.get(identifier) for identifier in message.containers]
    return ",".join(
        container.name if container is not None else identifier
        for identifier, container in zip(message.containers, known, strict=True)
    )


_UNCLASSIFIED: Final = receipts.UNCLASSIFIED
"""The receipt destination for a failed CLASSIFY step. Re-exported, not redefined.

One definition, in the module that writes the row it goes into
(``receipts.UNCLASSIFIED``). This alias exists so a reader of ``round`` still
finds the name at the two call sites that use it.
"""


class _ApplyOutcome(BaseModel):
    """What ``_apply`` did to one message, plus the id the write left behind.

    A model rather than a bare ``_MessageResult`` because the caller needs TWO
    facts and only this function has the second one. The post-move id lives on
    the provider's return value, which exists nowhere outside ``_apply``'s
    success arm -- and ``_process_message`` is what owns the replay marks
    (``_mark_applied_seen``). Recomputing it at the caller would mean a second
    ``file_message`` call, and passing it back through a mutable argument would
    hide a write in a signature that reads as a query.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    result: _MessageResult
    post_move_id: str | None = None
    """The id the message carries AFTER the write, or ``None``.

    ``None`` on every failure arm, because no write landed and there is no new
    id to name, and on a move receipt that honestly reported none.
    """

    destination_folder: str = ""
    """The taxonomy folder the write filed the message INTO, or "" on a failure.

    Carried for ONE reason: the replay mark for the post-move id belongs in the
    DESTINATION stream's window, and the destination is a fact only the success
    arm of ``_apply`` holds (#173). A NAME, and only the FALLBACK answer -- see
    ``destination_id`` for the one the caller prefers.
    """

    destination_id: str | None = None
    """The container id the write filed into, or ``None`` if none was reported.

    PREFERRED OVER ``destination_folder`` by ``_mark_applied_seen``, because it
    is the id the adapter POSTed rather than a name the caller has to re-resolve.
    A Graph adapter files into ``folder_prefix + folder`` and lists that prefixed
    string as the container's displayName, so the raw folder name matches no
    container at all in a prefixed mailbox (#173 round 3). ``None`` on every
    failure arm and from any adapter that reports no container -- Gmail returns a
    plain ``str``, and its mailbox is one stream anyway.
    """


def _apply(context: RoundContext, message: Message, *, folder_before: str = "") -> _ApplyOutcome:
    """Classify, file, and receipt one in-scope message. Never raises on refusal.

    ONE EXCEPTION, ADDED WITH #157: an ``OSError`` from the LEDGER propagates.
    Everything else -- a refused filing, a classifier backend that raised, a
    malformed local-model response -- is receipted with ``Status.FAILED`` and
    the round continues to the next message. Aborting the round here would let
    one contended message block every message behind it, and the inbox would
    silently stop clearing. The audit carve-out goes the other way: a mutation
    with no receipt breaks the record, so the failure that PREVENTS a receipt
    is the failure that must not be contained.

    THAT CARVE-OUT IS SCOPED BY CALL SITE NOW, NOT BY EXCEPTION TYPE (#179).
    It used to read ``except OSError: raise`` at the head of the boundary
    below, and that is wrong by inheritance rather than by intent:
    ``ConnectionError``, ``ConnectionResetError``, and ``TimeoutError`` are
    ``OSError`` subclasses, so a socket blip inside ``classify`` or inside the
    provider's write took the audit exit and killed the stream with no FAILED
    receipt at all -- the precise outcome the carve-out exists to prevent. The
    ledger calls sit OUTSIDE the try instead (``_receipt`` on both arms, and
    ``_draft``'s own append behind ``_LedgerWriteError``), so an audit-write
    failure still propagates and nothing else can borrow its exit.

    THE DRAFTING SEAM IS INSIDE THE CONTAINMENT NOW (#174). ``_draft`` used to
    be called after this function's ``try`` had closed, so anything raised
    before its internal catch opened -- an add-on that fails to import -- flew
    out of ``poll_once`` and abandoned every message behind this one.

    A ``classify`` FAILURE IS RECEIPTED TOO, at ``_UNCLASSIFIED``: the message
    was read and no filing followed, which is a fact the ledger owes the owner
    even though no provider call was made. The error CODE is the exception's
    type name, never its text, exactly as a provider refusal is -- classifier
    prose can quote the subject it choked on.

    The destination is the taxonomy folder from the classification. ``classify``
    already coerces an illegal or low-confidence answer to ``Review``, so
    ambiguity leaves the inbox without this module reopening that decision.

    The trace row is written AFTER the ledger receipt in both branches -- the
    ledger is the record of record and the trace is diagnostics on top of it,
    so a trace write can never come at the cost of a receipt. The trace uses
    the same message id the receipt used (``receipt_id`` on success, so a
    Graph move's new id matches the ledger delta).

    Returns:
        An ``_ApplyOutcome``: the per-message result, and on the APPLIED arm the
        id the write left behind so ``_mark_applied_seen`` can guard the message
        under both of its ids (#161a). Never message content.
    """
    decision = _UNCLASSIFIED
    # `None` until the intent is written, because `classify` can raise BEFORE
    # the write-ahead row exists -- a classifier failure never touched the
    # mailbox, so there is no intent to settle and nothing is owed.
    intent_id: str | None = None
    try:
        # INSIDE the boundary since #157, and that placement is the ticket.
        # `classify` reaches an INJECTED classifier, so it raises whatever that
        # backend raises -- `ClassifierBackendError` from
        # `classifiers/openai_compatible.py`, or a bare TypeError/ValueError off
        # a malformed local-model response. Sitting above this try, any of them
        # killed the whole round and abandoned every message behind this one.
        decision = classify(
            message,
            context.taxonomy,
            context.classifier,
            learned_rules=context.learned_rules,
        )
        # WRITE-AHEAD, then move. The intent is on disk BEFORE the provider
        # call, so a crash between the move and its receipt leaves an unsettled
        # row the next daemon start reports rather than moved mail with no
        # record at all. It is settled once `_receipt` returns, on BOTH arms:
        # a FAILED receipt discharges the intent exactly as an APPLIED one
        # does, because in either case the ledger now accounts for the attempt.
        #
        # This site does not call `intents.file_with_receipt` -- the shared
        # helper appends the receipt itself, and this round's receipt carries
        # the classification's `deterministic` flag through `_receipt` and must
        # also survive the broad catch below as a FAILED row. Same ordering,
        # spelled out here because the two halves straddle a try boundary.
        intent_id = intents.begin(context.provider.name, message.id, decision.folder)
        applied = context.provider.file_message(message.id, decision.folder)
    # A BROAD CATCH IS THE RULE HERE, not a lapse. Any refusal from any adapter
    # OR CLASSIFIER must become a FAILED receipt and let the round continue;
    # narrowing to ProviderError is exactly the defect #157 named -- it let one
    # backend's own exception type abandon every message behind this one. The
    # suppression is INLINE and scoped to this line -- written on its own line
    # it becomes a FILE-level directive and silently disables the rule for the
    # whole module.
    except Exception as error:  # ruff: ignore[blind-except]
        # DOCUMENTED FAIL-OPEN: the broad catch is the rule here (see above),
        # and the refusal is HANDLED rather than swallowed -- `_receipt` writes
        # a Status.FAILED ledger row, so the mutation is accounted for in the
        # audit trail the kit is sold on.
        #
        # The receipt is not a substitute for this line, and that is the whole
        # reason it exists. The ledger deliberately records only the exception's
        # TYPE NAME, never its text, because provider prose can quote the
        # subject line it refused and a leaked ledger must not be a leaked
        # mailbox. So the receipt proves THAT the filing failed and names the
        # class; the traceback -- the only thing that says WHY -- is discarded
        # at that boundary and exists nowhere else.
        #
        # ERROR with exc_info, because this is a mutation the owner asked for
        # that did not happen. The loop continuing is correct, but a round that
        # refuses every message must not look identical in the logs to one that
        # applied every message. Logged from HERE rather than from `_receipt`
        # so the traceback is captured while the exception is still live.
        # The whole refusal arm -- log, receipt, settle, trace -- lives in
        # `receipts` because every statement in it is an audit write. The
        # per-message result stays HERE, because only the round knows what a
        # refusal means to its cursor.
        receipts.record_refusal(
            context.provider.name,
            context.run_id,
            receipts.Attempt(
                message=message,
                decision=decision,
                folder_before=folder_before,
                intent_id=intent_id,
            ),
            error,
        )
        return _ApplyOutcome(result=_MessageResult.FAILED)
    # Graph's /move is copy-then-delete: the id handed to `file_message` is dead
    # the instant the move lands, and the receipt must name the message that
    # EXISTS afterwards or `reset restore` can never resolve the row back to a
    # message. The adapter reports that id on its return value
    # (`providers.outlook.provider.FiledMessage.message_id`); Gmail returns a plain `str`
    # with no such attribute, and its ids are stable, so `getattr` degrades to
    # exactly the id we were given.
    #
    # THE RETURN VALUE IS READ TWICE, AND THE TWO READS DIFFER DELIBERATELY.
    #
    # The RECEIPT id is `ledger.receipt_id_for`'s answer, not this module's.
    # The ledger owns what a row is allowed to say, and centralising the rule
    # is what stops this caller and the MCP tool boundary from disagreeing
    # about it again. The case this line used to get wrong: when Graph moves a
    # message and declines to name the result (`204 No Content`), falling back
    # to `message.id` resurrects an id the move already DELETED -- a wrong row
    # wearing the shape of a right one, which `reset restore` cannot tell from
    # a real receipt. `MESSAGE_ID_UNKNOWN` can be told apart, so the row is
    # honest about what is unknown rather than confident about what is false
    # (#169).
    #
    # The REPLAY mark takes the attribute RAW, and must keep doing so. `None`
    # there means the provider reported no new id, so no second id enters the
    # stream and there is nothing to guard against; substituting anything --
    # the requested id or the unknown sentinel -- would mark a message that
    # was never created.
    post_move_id = getattr(applied, "message_id", message.id)
    # SAME `getattr` PATTERN, SAME REASON: the adapter that reassigns an id is
    # also the one that knows which container it moved into, and an adapter
    # that reports neither degrades to the name fallback rather than erroring.
    # `or None` so an empty string cannot become a replay-window key.
    destination_id = getattr(applied, "destination_id", None) or None
    receipt_id = ledger.receipt_id_for(applied, message.id)
    _receipt(context, receipt_id, decision, source_container=folder_before)
    _settle(intent_id)
    trace.append(
        receipts.subject(context.provider.name, message, folder_before, receipt_id), decision
    )
    # AFTER the filing and its receipt, never instead of either. Filing is the
    # product; a draft is an addition on top of it, so a drafting refusal or
    # failure must leave a filed, receipted message behind rather than undoing
    # one -- which is why the seam cannot move up into the try above, where a
    # drafting failure would rewrite an APPLIED message into a FAILED one.
    #
    # IT GETS ITS OWN BOUNDARY INSTEAD (#174). `_draft`'s internal catch is
    # broad, but it opens by calling `_drafting()`, and everything BEFORE that
    # catch is uncontained -- an add-on that fails to load used to propagate
    # from here, out of `_apply`, out of `poll_once`, abandoning every message
    # behind this one with no boundary in between. `_drafting()` now answers
    # None for any import failure, so this try is the belt to that braces: any
    # future call added ahead of `_draft`'s own catch is contained the same
    # way, and the guarantee stops depending on where the callee's try begins.
    #
    # THE LEDGER CARVE-OUT IS NAMED, NOT ASSUMED (#179). `_draft`'s last
    # statement is a `ledger.append` for the draft it just created, and it
    # sits outside `_draft`'s own try on purpose: a draft that exists in the
    # owner's mailbox with no receipt is the audit break the carve-out exists
    # for, so an `OSError` from THAT append must propagate. It is named by the
    # one call it can come from rather than by catching `OSError` broadly,
    # which is exactly the defect #179 records -- `ConnectionError` and
    # `TimeoutError` are `OSError` subclasses, so a broad carve-out here would
    # let a transport blip in `create_draft` abort the round.
    try:
        _draft(context, message, decision)
    except _LedgerWriteError:
        raise
    except Exception as error:  # ruff: ignore[blind-except]
        # DOCUMENTED FAIL-OPEN, at WARNING for the reason `_draft`'s own catch
        # gives: the filing the owner asked for HAPPENED, and what did not land
        # is the optional addition on top of it. Type name only -- backend and
        # importer prose can quote the mail or the install path.
        logger.opt(exception=True).warning(
            f"WATCH: drafting raised outside its own boundary, message stays filed and "
            f"the round continues message={message.id} error_type={type(error).__name__}"
        )
    return _ApplyOutcome(
        result=_MessageResult.APPLIED,
        post_move_id=post_move_id,
        destination_folder=decision.folder,
        destination_id=destination_id,
    )


# ---------------------------------------------------------------------------
# Drafting -- gated on the ledger, after the filing, never instead of it.
# ---------------------------------------------------------------------------


class _LedgerWriteError(Exception):
    """The draft landed and its ledger row did not. Never contained.

    A PRIVATE MARKER, and the reason it exists rather than a bare ``OSError``
    is #179. The ledger carve-out has always been "an audit write that fails
    must abort the round", and it used to be spelled as ``except OSError:
    raise`` at the whole per-message boundary. That spelling is wrong by
    inheritance: ``ConnectionError``, ``ConnectionResetError``, and
    ``TimeoutError`` are all ``OSError`` subclasses, so a transport blip
    anywhere under the boundary -- in the classifier's HTTP call, in the
    provider's write -- took the audit exit and abandoned the stream with no
    FAILED receipt, which is the opposite of what the carve-out protects.

    Raising a type nothing else in the process raises makes the carve-out name
    the ONE call it means. ``_draft`` wraps its ``ledger.append`` and nothing
    else; ``_apply`` re-raises this and contains everything else.

    It carries the thread key and no message content, the same rule every
    ledger row and log line in this module follows.
    """

    def __init__(self, thread_id: str) -> None:
        """Build the error around the thread key, with no message content in it.

        The thread id is the only identifier an operator needs to find the
        orphaned draft. Taking it as the sole argument makes it impossible to
        construct this exception with a subject or body in its text -- the same
        content rule every ledger row and log line in this module follows.
        """
        super().__init__(f"the draft ledger row for thread={thread_id} could not be written")


def _already_drafted(thread_id: str) -> bool:
    """THE IDEMPOTENCY GATE. True when the ledger already holds a draft row.

    ``create_draft`` is NOT idempotent at the provider: calling it twice for
    one real intent leaves two drafts in the owner's mailbox and two receipts.
    That is the reason the method sat in ``FORBIDDEN_METHODS`` until Sprint 4,
    and this function is what answers it.

    THE REPLAY WINDOW IS NOT THIS GUARANTEE, and reaching for it instead is the
    mistake this docstring exists to prevent. ``WatchState``'s ``seen`` set is
    bounded and a cursor resync re-reads everything the stream covers, so a
    message can legitimately arrive at ``_apply`` a second time. Filing twice
    is a no-op move; drafting twice is a second draft. The ledger is
    append-only, is written by the connector itself at the moment the provider
    call succeeds, and is cleared by nothing -- including a resync -- so it is
    the only record in this package that outlives the window.

    ITS WEAKNESS IS STATED IN ``ledger.thread_has_draft`` AND IS DELIBERATE:
    the ledger knows what this connector drafted, not what is in the mailbox.
    An owner who deleted a draft gets no replacement, which is the correct
    default -- a deleted draft is a draft they did not want.

    Args:
        thread_id: The thread key, as ``create_draft`` receipts it.

    Returns:
        True when this thread has been drafted on before. Refuse if so.
    """
    return ledger.thread_has_draft(thread_id)


def _draft(context: RoundContext, message: Message, decision: Classification) -> bool:
    """Draft one reply, if every gate says yes. Returns whether a draft landed.

    The gates run in this order and the order is the cost gradient: a table
    lookup, then a local file read, then the network. Nothing consults the
    provider until both cheap answers have passed.

      1. no composer injected, OR the voice add-on is not installed -> no
         drafting in this deployment at all;
      2. ``should_draft`` -> the folder and thread refusal tables;
      3. ``_already_drafted`` -> the ledger idempotency gate;
      4. the owner has already replied -> their reply IS the answer, and a
         draft under it argues with the correspondence they conducted;
      5. compose and enforce constraints -> refuses BEFORE any API call.

    NEVER RAISES ON REFUSAL, the same contract ``_apply`` holds. A thread that
    could not be drafted must not abandon the messages behind it -- filing is
    the product and drafting is the addition, so a drafting failure degrades to
    a filed message with no draft rather than to a stalled round.

    Args:
        context: This round's provider, state, and injected composer.
        message: The message just filed.
        decision: Its classification, which names the folder to gate on.

    Returns:
        True when a draft was created and receipted.
    """
    drafting = _drafting()
    # THE TIER GATE SITS BESIDE THE INJECTION GATE, not in place of it. An
    # absent add-on and an uninjected composer are the same answer -- this
    # deployment does not draft -- so they are refused at the same line and
    # before any thread is inspected.
    if drafting is None or context.draft_backend is None or context.routing is None:
        return False
    thread_id = message.thread_id or message.id
    if not drafting.should_draft(
        decision.folder, _thread_context(drafting, context, message)
    ).should_draft:
        return False
    if _already_drafted(thread_id):
        logger.debug(f"WATCH: thread already drafted, refusing a second draft thread={thread_id}")
        return False
    try:
        thread = context.provider.get_thread(thread_id)
        if _owner_already_replied(thread, context.owner_address):
            return False
        body = drafting.generate_draft_body(
            context.draft_backend,
            "\n\n".join(thread.bodies),
            context.routing,
            context.voice,
        )
        draft = context.provider.create_draft(thread_id, body)
    # A BROAD CATCH IS THE RULE HERE, for the reason `_apply` states: any
    # refusal from any adapter, any backend, or the profile store must leave
    # the round running. The suppression is INLINE and scoped to this line.
    except Exception as error:  # ruff: ignore[blind-except]
        # DOCUMENTED FAIL-OPEN. The message is already FILED and receipted, so
        # nothing is lost; what did not happen is the optional draft. Logged at
        # WARNING rather than ERROR because unlike a refused filing this is not
        # a mutation the owner asked for that failed -- it is an addition that
        # did not land. Type name only: backend prose can quote the mail.
        logger.opt(exception=True).warning(
            f"WATCH: draft not created, message stays filed and the round continues "
            f"thread={thread_id} error_type={type(error).__name__}"
        )
        return False
    # THE DRAFT'S RECEIPT, AND THE ONE FAILURE THIS FUNCTION MAY NOT CONTAIN.
    # The draft now EXISTS in the owner's mailbox; a round that carried on
    # after failing to record it would leave a provider write with no ledger
    # row, which is the audit invariant the kit is sold on. So an `OSError`
    # from this append is re-raised, wrapped in a type only this module's
    # boundary catches (#179). The wrapper is what makes the carve-out
    # NARROW: `ConnectionError` and `TimeoutError` are `OSError` subclasses
    # too, and a caller that carved out `OSError` by type would abort the
    # round on a transport blip in `create_draft` -- the exact defect the
    # narrowing fixes. Only a failure of the append itself gets here.
    try:
        ledger.append(
            context.provider.name,
            ledger.ACTION_DRAFT,
            thread_id,
            f"draft:{draft.draft_id}",
            voice=context.voice,
        )
    except OSError as error:
        raise _LedgerWriteError(thread_id) from error
    return True


def _owner_already_replied(thread: Thread, owner_address: str) -> bool:
    """True when the owner's own address appears as a sender on the thread.

    THE OWNER ANSWERING IS THE ANSWER. A draft appended under their reply
    argues with correspondence they already conducted, and they would have to
    read the whole thread to discover the draft is stale -- which is exactly
    the inbox time the kit exists to give back.

    Reads ``Message.sender``, provider-stated structure, and never body text.

    An EMPTY owner address answers False, and that is the conservative reading
    in only one direction, so it is worth naming: with no configured owner this
    cannot tell a reply from an incoming message, so it does not claim to. The
    thread still has to pass every other gate, and ``_is_solo_thread`` in
    ``drafting`` is what catches the degenerate case.
    """
    target = owner_address.strip().lower()
    if not target:
        return False
    return any(message.sender.strip().lower() == target for message in thread.messages)


def _thread_context(drafting: ModuleType, context: RoundContext, message: Message) -> object:
    """Build the narrow input ``should_draft`` is allowed to read.

    Constructed HERE rather than passed in so the enumerated fields are filled
    from provider structure and owner config at one site. ``subject`` and
    ``snippet`` are not on ``ThreadContext`` at all, so this function has
    nowhere to put them even by accident.

    ``drafting`` ARRIVES AS A PARAMETER rather than being loaded here, so the
    caller's single tier check covers this function too -- there is no second
    place that could reach a missing add-on. For the same reason the return is
    typed ``object``: the real type is ``ThreadContext``, which lives in the
    add-on and cannot be named from a module that must import without it. The
    value is handed straight back to ``drafting`` and never inspected here.
    """
    return drafting.ThreadContext(
        sender=message.sender,
        participants=(message.sender, context.owner_address),
        owner_address=context.owner_address,
    )


_settle: Final = receipts.settle
"""Discharge the write-ahead intent. See ``receipts.settle`` for the contract."""


def _receipt(
    context: RoundContext,
    message_id: str,
    decision: Classification,
    *,
    refusal: receipts.Refusal = receipts.NOT_REFUSED,
    source_container: str = "",
) -> None:
    """Append one ledger row for this round's attempted mutation.

    A THIN ADAPTER OVER ``receipts.append_receipt``, which owns the rules -- the
    type-name-not-text error code, the deterministic flag, the FAILED arm. This
    function exists to supply the two things only a ROUND knows: its provider
    name and its ``ledger.Provenance``, which pairs the round's own ``run_id``
    with the container this message is leaving (#66) and the mailbox it is
    leaving it in (#69 -- one ledger now receives every configured account's
    rows, and ``provider`` cannot tell two Gmail accounts apart).

    ``source_container`` DEFAULTS TO EMPTY AND THAT IS A REAL ANSWER. The
    classify-failure receipt in ``_process_message`` has no placement to name --
    nothing read where the message was -- and an empty source is what tells
    ``undo`` the row is not reversible, instead of handing it a folder to guess
    with.
    """
    receipts.append_receipt(
        context.provider.name,
        message_id,
        decision,
        ledger.Provenance(
            run_id=context.run_id,
            source_container=source_container,
            account=context.account,
        ),
        refusal,
    )
