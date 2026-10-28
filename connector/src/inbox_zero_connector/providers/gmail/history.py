"""Gmail's change feed: ``users.history.list``, and the cold start before it.

WHY THIS IS ITS OWN MODULE
    Split out of ``gmail.py`` for the same reason ``gmail_ids.py`` and
    ``gmail_query.py`` were: that module sits at the 500-code-line ceiling
    ``_DOCS/STANDARDS-python.md`` enforces, with two lines of headroom measured
    before this work began. The alternative -- compressing docstrings to make
    room -- defeats the gate, because the ceiling counts CODE lines precisely
    so that the security argument an auditor reads is never what gets deleted
    to fit.

    The seam is real and not merely convenient. Everything here answers one
    question -- what changed since a position -- and none of it is reachable
    from any other Gmail operation. ``GmailProvider.poll_changes`` stays in
    ``gmail.py`` as the interface method and delegates the mechanics here.

WHAT GMAIL'S CHANGE FEED IS, AND HOW IT DIFFERS FROM GRAPH'S
    Gmail has ONE mailbox-wide feed: ``users.history.list`` walks forward from
    a ``historyId``, and that id covers the whole mailbox rather than one
    folder. Graph has no mailbox-wide delta at all -- ``/me/messages/delta``
    does not exist -- so covering a Graph mailbox costs one stream and one
    independently-expiring token PER FOLDER. That structural difference is
    already modelled by ``watchstate.WatchCursor.container_id``, which Gmail
    stores as ``None`` exactly once.

    The consequence for this module is that there is nothing to fan out over.
    One call, one cursor, one answer.

THE 404 IS NORMAL CONTROL FLOW, AND THE KNOWLEDGE STAYS HERE
    Gmail answers ``404`` when the supplied ``startHistoryId`` is older than
    the mailbox's retained history. That is the provider saying "that position
    is gone, start over" -- it is expected operation on a quiet mailbox, not a
    failure, and this module answers it with ``ChangeSet(resync_required=True)``
    rather than an exception. A watcher that raised on it would raise on a
    perfectly healthy mailbox.

    The status code is named in THIS FILE and nowhere else. Shared code -- the
    ``ChangeSet`` model, ``watchstate``, the loop above them -- learns only the
    concept, because the concept ports between providers and the number does
    not: Graph signals the same thing with ``410 Gone`` and
    ``syncStateNotFound``. Shared code that hardcoded either would silently
    stop recognising the other.

WHAT THIS MODULE DOES NOT DO
    It classifies nothing, labels nothing, moves nothing, and persists nothing.
    It reads the feed and reports it. Scope enforcement, the replay guard, the
    filing decision, and the cursor WRITE all belong to the loop above it and
    to the modules that already own them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from googleapiclient.errors import HttpError

from inbox_zero_connector.providers.base import ChangeSet, ProviderError
from inbox_zero_connector.providers.retry import with_retry
from inbox_zero_connector.utils.logging_config import ProviderCall

if TYPE_CHECKING:
    from collections.abc import Callable

PROVIDER_LOG_NAME: Final = "google"
"""Which provider this module's log lines are attributed to.

Stated here rather than imported from ``gmail`` because that module
imports THIS one -- taking the name back the other way would be a circular
import. It is the same literal ``gmail.PROVIDER`` holds, and
``test_the_history_module_logs_under_the_same_provider_name`` asserts the
two agree so the duplication cannot drift.
"""

HISTORY_GONE_STATUS: Final = 404
"""What Gmail returns when ``startHistoryId`` predates its retained history.

THE ONE PLACE THIS NUMBER APPEARS. It means "your resume position has aged
out, resynchronise from scratch" -- documented behaviour, not an error to
propagate. See the module docstring for why no shared module may learn it.
"""

MESSAGE_GONE_STATUS: Final = 404
"""What Gmail returns when a message id no longer names a message.

THE SAME NUMBER AS ``HISTORY_GONE_STATUS`` AND DELIBERATELY A SECOND
CONSTANT. It is a different fact about a different resource: this one says
"that message is gone", the other says "that POSITION is gone", and the two
have opposite correct answers -- skip one message and carry on, versus discard
the cursor and resynchronise the whole stream. Sharing one name would make a
future reader believe collapsing the two handlers is a simplification, and the
resulting adapter would answer an aged-out cursor with a quiet committed round
that silently resumed from a position Gmail had already refused.
"""

HISTORY_TYPES: Final = ("messageAdded",)
"""The only record type this watcher consumes.

``messageDeleted``, ``labelAdded``, and ``labelRemoved`` are read-side noise
for a watcher whose entire output is an added label: it acts on messages that
ARRIVED, and it never removes anything, so a record about a removal has
nothing for it to do. Filtering server-side keeps the response small and keeps
the loop from having to recognise record types it would only ever discard.
"""

HISTORY_PAGE_LIMIT: Final = 100
"""How many history records one ``users.history.list`` page may carry.

WHY A CEILING AT ALL. Gmail's history feed is MAILBOX-WIDE -- there is no
per-folder feed to ask for -- so every record the mailbox produces shares the
page with the inbox arrivals this watcher acts on. ``historyTypes`` already
drops the label and delete records server-side, but a ``messageAdded`` is
generated by the owner's own SENT and DRAFT writes too, and those cannot be
filtered by type because they ARE additions. Left unbounded, Gmail chooses the
page size, and a busy sending day can fill a page with records the adapter
discards and push real arrivals onto a page the round's page budget never
reaches -- read by the loop as a quiet mailbox, with no error and no log line.
A stated ceiling makes the round's cost per page predictable instead.

WHY NOT ``labelId`` INSTEAD. ``users.history.list`` does accept a ``labelId``,
which would narrow the feed to ``INBOX`` and remove the noise at the source --
and this adapter must not pass one. The parameter takes exactly ONE label,
so sending it declares a single-label read scope, and the watcher's read scope
is decided by ``MailboxScope`` above this module, not here. Gmail's stream has
no single-label identity to borrow either: ``GmailProvider.poll_changes``
accepts ``container_id`` and IGNORES it, storing one cursor under
``container_id=None``, because one mailbox-wide feed is the whole model (see
the module docstring). Narrowing to a label the adapter invented would silently
make a mailbox-wide cursor mean something narrower than what it walked, and any
scope the operator later widened would be dropped by the adapter without
either of them being told.
"""

BOOTSTRAP_LIMIT: Final = 50
"""How many recent inbox messages a cold start reports.

A cold start has NO history to walk -- that is what makes it cold -- so it
cannot answer "what changed" and does not pretend to. It reports the mailbox's
current position (so the next round has somewhere to resume from) plus a
bounded sample of recent mail, which is what the loop can usefully act on
without reading an entire mailbox on first run.

Bounded rather than exhaustive on purpose. An unbounded first read of a
years-old mailbox is a large, slow, expensive operation whose results are
almost all stale by definition, and on a busy business mailbox it is also a much
larger read than the owner expected to authorise. The messages this misses are
not lost: they were already in the mailbox before the watcher existed, and
anything that arrives after the cold start appears on the very next round.
"""


def _history_page(
    call: ProviderCall,
    api: Callable[[], Any],
    cursor: str,
    page_token: str | None,
) -> dict[str, Any]:
    """Fetch ONE page of ``users.history.list``. Raises on a refused position.

    Split from ``poll_changes`` so the transport call and the interpretation of
    its result are separate things a test can drive independently.

    The request is bounded by ``HISTORY_PAGE_LIMIT`` and carries NO ``labelId``;
    that constant's docstring owns both decisions and why the second one is a
    scope this adapter is not entitled to declare.

    Raises:
        HttpError: Passed through untouched -- including the 404 that means
            "resync". Interpreting it is the caller's job, in one place.
    """
    request: dict[str, Any] = {
        "userId": "me",
        "startHistoryId": cursor,
        "historyTypes": list(HISTORY_TYPES),
        "maxResults": HISTORY_PAGE_LIMIT,
    }
    if page_token is not None:
        request["pageToken"] = page_token
    return dict(with_retry(call, lambda: api().users().history().list(**request).execute()))


def _added_message_ids(page: dict[str, Any]) -> list[str]:
    """Message ids from the ``messagesAdded`` records on one history page.

    Deduplicated in order. Gmail documents that the same change can appear more
    than once, and a message that gains several labels in one round produces
    several records naming it. Returning it twice would make the loop fetch and
    process it twice within a SINGLE round, which the seen-ids guard cannot
    help with because nothing has been marked seen yet.
    """
    seen: dict[str, None] = {}
    for record in page.get("history", []) or []:
        for added in record.get("messagesAdded", []) or []:
            message = added.get("message") or {}
            message_id = str(message.get("id", ""))
            if message_id:
                seen.setdefault(message_id, None)
    return list(seen)


def _is_history_gone(exc: HttpError) -> bool:
    """True when Gmail refused the supplied position rather than failing.

    Reads ``resp.status`` from the SDK's own error object. The status is
    compared to ``HISTORY_GONE_STATUS`` rather than to a literal so the number
    has exactly one home, and the check is contained here so no caller has to
    know what number it is.
    """
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) == HISTORY_GONE_STATUS


def _is_message_gone(exc: HttpError) -> bool:
    """True when Gmail says the fetched MESSAGE no longer exists.

    Deliberately narrow. Only a ``404`` on the message fetch qualifies; every
    other status is a genuine transport failure and must keep escaping, because
    a page silently shorter than the feed reported is indistinguishable from an
    adapter losing mail -- and the cursor committed after it would resume past
    changes nothing ever read.
    """
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) == MESSAGE_GONE_STATUS


def fetch_page_messages(
    call: ProviderCall,
    to_message: Callable[[ProviderCall, str], Any],
    message_ids: list[str],
) -> tuple[tuple[Any, ...], int]:
    """Fetch every id on one page, DROPPING the ones already deleted.

    THE OWNING BOUNDARY FOR THE VANISHED-MESSAGE CASE, and the reason it is a
    function here rather than a ``try`` inside ``_message_header``. Every Gmail
    read is two calls -- ids first, headers second -- and the owner may delete a
    message in the gap between them. Only a caller holding SEVERAL ids can
    decide that one 404 is a skip: a single-id fetch has no "rest of the page"
    to continue with, so for it the 404 is the whole answer and must still
    raise. Putting the decision here keeps that distinction intact.

    Shared by ``cold_start``, ``poll``, and ``GmailProvider.list_inbox``
    because all three are the same fan-out and had the same defect; a second
    copy would be a second place for it to come back.

    Args:
        call: The open ``ProviderCall`` this fan-out logs under.
        to_message: Fetches one message header projection by id.
        message_ids: The ids this page reported, in order.

    Returns:
        The messages that were still fetchable, and how many were skipped.

    Raises:
        HttpError: Any failure that is NOT a vanished message, untouched.
    """
    messages: list[Any] = []
    skipped = 0
    for message_id in message_ids:
        try:
            messages.append(to_message(call, message_id))
        except HttpError as exc:
            if not _is_message_gone(exc):
                raise
            skipped += 1
            # WARNING, not ERROR: the run is correct and continues, and an
            # ERROR here would train the operator to ignore the level. The id
            # is the entire payload -- an opaque Gmail handle carrying no
            # mailbox content, unlike the subject or sender the counts-only
            # rule keeps out of every summary.
            call.guard("message vanished before fetch", message_id=message_id)
    return tuple(messages), skipped


def cold_start(
    call: ProviderCall,
    api: Callable[[], Any],
    to_message: Callable[[ProviderCall, str], Any],
) -> ChangeSet:
    """The first round for a stream that has no cursor. READ ONLY.

    There is no history to walk from a position that does not exist, so this
    does NOT call ``users.history.list`` at all -- calling it with no
    ``startHistoryId`` would be asking Gmail to walk from nowhere. It reads the
    mailbox's CURRENT ``historyId`` from the profile, which is the position the
    next round resumes from, and a bounded sample of recent inbox mail.

    The order matters and is not interchangeable. The profile is read FIRST, so
    the stored position is no NEWER than the messages reported. Reading it
    afterwards would store a position past changes this round never saw, and
    those changes would never be reported by any round -- the same silent loss
    the mid-page cursor rule exists to prevent, arriving by a different door.

    Args:
        call: The open ``ProviderCall`` this round logs under.
        api: Returns the authenticated Gmail service.
        to_message: Fetches one message header projection by id.

    Returns:
        A ``ChangeSet`` carrying the current position and the bootstrap sample.

    Raises:
        ProviderError: The profile or the listing call failed.
    """
    try:
        profile = with_retry(call, lambda: api().users().getProfile(userId="me").execute())
        listing = with_retry(
            call,
            lambda: (
                api()
                .users()
                .messages()
                .list(userId="me", q="in:inbox", maxResults=BOOTSTRAP_LIMIT)
                .execute()
            ),
        )
    except HttpError as exc:
        raise ProviderError(
            f"Gmail poll_changes cold start failed: {exc}. ACTION REQUIRED: check the "
            "token is still valid with 'uv run inbox-zero-connector show-scopes', then "
            "re-run 'auth google' if it is not."
        ) from exc
    refs = listing.get("messages", []) or []
    try:
        messages, skipped = fetch_page_messages(call, to_message, [str(ref["id"]) for ref in refs])
    except HttpError as exc:
        raise ProviderError(f"Gmail poll_changes cold start failed: {exc}") from exc
    call.fields["cold_start"] = True
    call.fields["returned"] = len(messages)
    call.fields["skipped_missing"] = skipped
    return ChangeSet(
        messages=messages,
        cursor=str(profile.get("historyId", "")),
        skipped_missing=skipped,
    )


def poll(
    call: ProviderCall,
    api: Callable[[], Any],
    to_message: Callable[[ProviderCall, str], Any],
    cursor: str,
    page_token: str | None = None,
) -> ChangeSet:
    """Walk ONE page of the change feed from ``cursor``. READ ONLY.

    One page, not the whole feed: a method that looped internally would have to
    decide when to stop, and the page budget belongs to the loop that owns the
    round. When Gmail reports another page, this returns it as
    ``next_page_token`` and NO cursor -- ``ChangeSet`` refuses to carry both,
    so a caller cannot store a partial read as a finished one even by mistake.

    A ``404`` from Gmail means the supplied position aged out. That is normal
    operation and is answered with ``resync_required=True``, not an exception.
    See the module docstring for why the number lives only in this file.

    Args:
        call: The open ``ProviderCall`` this round logs under.
        api: Returns the authenticated Gmail service.
        to_message: Fetches one message header projection by id.
        cursor: The opaque ``historyId`` to resume from, replayed verbatim.
        page_token: Continuation handle from a previous page, or ``None``.

    Returns:
        A ``ChangeSet``. ``cursor`` is populated only on the final page, and
        only from the position the RESPONSE reported: a final page carrying no
        ``historyId`` yields ``cursor=None``, which says "nothing new to store"
        rather than handing back the aged position it was asked to walk from.

    Raises:
        ProviderError: The call failed for a reason other than a refused
            position.
    """
    try:
        page = _history_page(call, api, cursor, page_token)
    except HttpError as exc:
        if not _is_history_gone(exc):
            raise ProviderError(f"Gmail poll_changes failed: {exc}") from exc
        # Expected operation, not a failure: the stored position is older than
        # the history Gmail retains. The caller drops the cursor and cold
        # starts. Logged at the call's own level rather than raised, because a
        # raise here would fire on a perfectly healthy quiet mailbox.
        call.fields["resync_required"] = True
        return ChangeSet(resync_required=True)

    try:
        messages, skipped = fetch_page_messages(call, to_message, _added_message_ids(page))
    except HttpError as exc:
        raise ProviderError(f"Gmail poll_changes failed: {exc}") from exc
    next_page_token = page.get("nextPageToken")
    call.fields["returned"] = len(messages)
    call.fields["skipped_missing"] = skipped
    # THE CURSOR RULE (WATCHER-SPEC.md 2.4): a page that is not the last one
    # yields NO storable cursor. `ChangeSet` enforces this too; passing None
    # explicitly here is what makes the rule readable at the site that decides.
    if next_page_token is not None:
        return ChangeSet(
            messages=messages,
            next_page_token=str(next_page_token),
            skipped_missing=skipped,
        )
    # THE FINAL-PAGE POSITION IS THE RESPONSE'S, OR IT IS NOTHING. Re-storing
    # the SUPPLIED cursor when Gmail reports no top-level `historyId` looks
    # harmless and is not: a quiet mailbox would rewrite the same ageing token
    # every round, never advancing, until Gmail evicts it and the watcher takes
    # a full resync it never needed -- and the stored position would claim to be
    # this round's answer while actually being the previous round's input. `None`
    # is the honest report of "nothing new to store"; `ChangeSet` already allows
    # it on a final page, and the caller keeps the position it already holds.
    advanced = page.get("historyId")
    if advanced is None:
        # Reported as a FIELD on the round's own call rather than a bare log
        # line: `logging_config` forbids reaching for the loguru logger
        # directly, and this way the fact rides the same five-point record an
        # operator already reads, next to `returned` and `skipped_missing`.
        call.fields["cursor_unstored"] = True
    return ChangeSet(
        messages=messages,
        cursor=None if advanced is None else str(advanced),
        skipped_missing=skipped,
    )


def poll_changes(
    api: Callable[[], Any],
    to_message: Callable[[ProviderCall, str], Any],
    cursor: str | None,
    page_token: str | None = None,
) -> ChangeSet:
    """ONE round of the Gmail change feed. The whole dispatch lives here.

    ``GmailProvider.poll_changes`` is the interface method and delegates
    straight to this: the provider class states the contract and this module
    owns every mechanic behind it, so the change feed is one file a reviewer
    reads end to end rather than a method in ``gmail.py`` calling helpers here.

    The branch is the cold-start question and nothing else -- ``cursor=None``
    means there is no position to walk from, which is a different operation
    rather than a degenerate case of the same one (see ``cold_start``).

    This is also the log boundary: one ``ProviderCall`` per round, opened here
    so both paths report under the same operation name and a reader of the log
    sees one round as one story.
    """
    with ProviderCall(PROVIDER_LOG_NAME, "poll_changes", has_cursor=cursor is not None) as call:
        if cursor is None:
            return cold_start(call, api, to_message)
        return poll(call, api, to_message, cursor, page_token)
