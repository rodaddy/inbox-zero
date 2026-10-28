"""RED contract tests for one Outlook per-folder delta round.

Every test calls the public ``OutlookProvider.poll_changes`` boundary through
the shared mocked Graph transport. The current provider stub raises
``NotImplementedError``; that observed failure is the required RED proof before
the implementation lane begins.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from _outlook_harness import INBOX_FOLDER_ID, MOCK_TRANSPORT_SENTINEL, Recorder
from inbox_zero_connector.providers.base import ChangeSet, MailProvider, ProviderError
from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE, OutlookProvider
from inbox_zero_connector.services.watch.round import FORBIDDEN_METHODS
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState

COLD_DELTA_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=cold-round-1"
)
RESUME_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_INBOX/messages/delta"
    "?$deltatoken=resume-round-1"
)
NEXT_DELTA_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_INBOX/messages/delta"
    "?$deltatoken=resume-round-2"
)
OPAQUE_DELTA_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_INBOX/messages/delta"
    "?$deltatoken=A%2BB%2F9%3D&state=01%2F02%2B03"
)


def _queue_final_page(recorder: Recorder, values: list[dict[str, Any]], delta_link: str) -> None:
    """Queue one successful final delta page in the shared transport."""
    recorder.get_status.append(200)
    recorder.delta_payloads.append({"value": values, "@odata.deltaLink": delta_link})


def test_poll_changes_empty_mailbox_completes_with_no_messages(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    _queue_final_page(recorder, [], COLD_DELTA_LINK)

    changes = provider.poll_changes(None)

    assert changes.messages == ()
    assert changes.cursor == COLD_DELTA_LINK
    assert changes.has_more is False
    assert changes.resync_required is False


def test_poll_changes_cold_start_reports_current_messages(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    _queue_final_page(recorder, [recorder.thread[0]], COLD_DELTA_LINK)

    changes = provider.poll_changes(None)

    assert [message.id for message in changes.messages] == ["m1"]
    assert changes.cursor == COLD_DELTA_LINK
    assert recorder.requests[0].url.path.endswith("/me/mailFolders/inbox/messages/delta")


def test_poll_changes_replays_resumed_cursor_verbatim(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    _queue_final_page(recorder, [recorder.thread[0]], NEXT_DELTA_LINK)

    changes = provider.poll_changes(RESUME_LINK)

    assert [message.id for message in changes.messages] == ["m1"]
    assert str(recorder.requests[0].url) == RESUME_LINK
    assert changes.cursor == NEXT_DELTA_LINK


def test_poll_changes_zero_change_round_returns_empty_change_set(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    _queue_final_page(recorder, [], NEXT_DELTA_LINK)

    changes = provider.poll_changes(RESUME_LINK)

    assert changes == ChangeSet(cursor=NEXT_DELTA_LINK)


def test_poll_changes_captures_delta_link_verbatim_as_cursor(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    _queue_final_page(recorder, [], OPAQUE_DELTA_LINK)

    changes = provider.poll_changes(RESUME_LINK)

    assert changes.cursor == OPAQUE_DELTA_LINK


# ---------------------------------------------------------------------------
# Paging and resync -- the two rounds that do NOT end in a storable cursor
# ---------------------------------------------------------------------------

NEXT_PAGE_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_INBOX/messages/delta?$skiptoken=page-2-of-2"
)

FINAL_PAGE_DELTA_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_INBOX/messages/delta"
    "?$deltatoken=after-the-last-page"
)

ARCHIVE_FOLDER_ID = "F_ARCHIVE"
"""A second in-scope folder. Graph runs ONE delta stream per folder, so a
folder is the unit a 410 can invalidate -- and the unit everything else must
survive it."""


def _queue_more_pages_page(
    recorder: Recorder, values: list[dict[str, Any]], next_link: str
) -> None:
    """Queue one NON-final delta page: an ``@odata.nextLink``, no deltaLink.

    Graph's two continuation keys are mutually exclusive and mean opposite
    things: ``@odata.nextLink`` says "more of THIS round remains",
    ``@odata.deltaLink`` says "this round is complete, here is where the next
    one starts". Serving both, or serving a deltaLink mid-round, is the shape
    that produces the storable-cursor-mid-page bug, so the double is written so
    a page can only ever carry one.
    """
    recorder.get_status.append(200)
    recorder.delta_payloads.append({"value": values, "@odata.nextLink": next_link})


def _queue_sync_state_not_found(recorder: Recorder) -> None:
    """Queue a Graph ``410 Gone`` / ``syncStateNotFound`` for the next delta GET.

    Deliberately queued as a STATUS, not as a raised exception in the double:
    what is under test is that the adapter converts this transport-level
    refusal into a ``resync_required`` FLAG. A double that raised would make an
    adapter that also raises look correct.
    """
    recorder.get_status.append(410)


def test_poll_changes_mid_page_round_offers_no_cursor_to_store(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A round with pages remaining reports ``has_more`` and NO cursor.

    ``ChangeSet._final_pages_only`` (base.py:378) refuses this combination at
    construction, so an adapter that tried to store a cursor mid-page would
    raise a ``ValidationError`` rather than return one. The assertion here is
    the other half: the adapter must never ASK for that construction, and the
    round it returns must still be resumable via ``next_page_token``.
    """
    _queue_more_pages_page(recorder, [recorder.thread[0]], NEXT_PAGE_LINK)

    changes = provider.poll_changes(RESUME_LINK)

    assert changes.has_more is True
    assert changes.cursor is None
    assert changes.next_page_token is not None
    assert changes.resync_required is False


def test_poll_changes_resumes_the_next_page_from_its_page_token(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """``page_token`` continues the SAME round; only its last page has a cursor.

    Two calls, because one page per call is the contract (base.py:487): the
    page budget belongs to the loop, not to the adapter. The first call yields
    a token and no cursor, the second consumes that token and yields the
    deltaLink -- and only then is there anything to store.
    """
    _queue_more_pages_page(recorder, [recorder.thread[0]], NEXT_PAGE_LINK)
    _queue_final_page(recorder, [recorder.thread[0]], FINAL_PAGE_DELTA_LINK)

    first = provider.poll_changes(RESUME_LINK)
    second = provider.poll_changes(RESUME_LINK, page_token=first.next_page_token)

    assert first.cursor is None
    assert second.cursor == FINAL_PAGE_DELTA_LINK
    assert second.has_more is False
    assert str(recorder.requests[-1].url) == NEXT_PAGE_LINK


def test_poll_changes_never_reports_a_cursor_on_any_non_final_page(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Across a three-page round, every page but the last carries no cursor.

    One mid-page round could pass by accident; the invariant is that it holds
    for EVERY page of an arbitrarily long round, which is the case that matters
    on a cold start over a large folder.
    """
    _queue_more_pages_page(recorder, [recorder.thread[0]], NEXT_PAGE_LINK)
    _queue_more_pages_page(recorder, [recorder.thread[0]], NEXT_PAGE_LINK)
    _queue_final_page(recorder, [recorder.thread[0]], FINAL_PAGE_DELTA_LINK)

    cursors: list[str | None] = []
    page_token: str | None = None
    for _ in range(3):
        page = provider.poll_changes(RESUME_LINK, page_token=page_token)
        cursors.append(page.cursor)
        page_token = page.next_page_token

    assert cursors == [None, None, FINAL_PAGE_DELTA_LINK]
    assert page_token is None


def test_poll_changes_reports_expired_token_as_a_flag_not_an_exception(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A refused resume token comes back as ``resync_required``, never a raise.

    Graph evicts delta tokens with no lifetime guarantee, so this is ordinary
    operation on a HEALTHY mailbox (base.py:474). An adapter that raised would
    raise on a mailbox with nothing wrong with it, and the watcher would log an
    error where it should have run a cold start.
    """
    _queue_sync_state_not_found(recorder)

    changes = provider.poll_changes(RESUME_LINK)

    assert changes.resync_required is True
    assert changes.cursor is None
    assert changes.has_more is False
    assert changes.messages == ()


def test_poll_changes_resync_round_is_followed_by_a_usable_cold_start(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """After the flag, a ``cursor=None`` round produces a fresh storable cursor.

    The resync flag is only worth anything if the recovery it names actually
    works, so the test drives the whole recovery rather than stopping at the
    flag: refuse the token, then cold start, then assert there is once again
    something to store.
    """
    _queue_sync_state_not_found(recorder)
    _queue_final_page(recorder, [recorder.thread[0]], COLD_DELTA_LINK)

    refused = provider.poll_changes(RESUME_LINK)
    cold = provider.poll_changes(None)

    assert refused.resync_required is True
    assert cold.resync_required is False
    assert cold.cursor == COLD_DELTA_LINK


def test_poll_changes_410_on_one_folder_leaves_other_folder_cursors_intact(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A refused token invalidates ONE folder's stream, never the mailbox.

    Graph has no mailbox-wide delta: watchstate keys every cursor by container
    (``watchstate.WatchState.cursors``), so a 410 is scoped to the folder whose
    token was refused. Treating it as mailbox-wide would throw away good
    cursors and force a full re-read of folders that were never asked.
    """
    state = WatchState()
    state.record_cursor(INBOX_FOLDER_ID, RESUME_LINK)
    state.record_cursor(ARCHIVE_FOLDER_ID, NEXT_DELTA_LINK)
    _queue_sync_state_not_found(recorder)

    changes = provider.poll_changes(RESUME_LINK)
    assert changes.resync_required is True
    state.resync_required(INBOX_FOLDER_ID)

    assert state.cursor_for(INBOX_FOLDER_ID) is None
    archive = state.cursor_for(ARCHIVE_FOLDER_ID)
    assert archive is not None
    assert archive.token == NEXT_DELTA_LINK


def test_poll_changes_410_bumps_only_the_refused_folders_generation(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The resync counter moves for the refused folder and for no other.

    Generation is the operator's signal that a stream was thrown away rather
    than advanced (``watchstate.WatchCursor.generation``). A 410 handled as
    mailbox-wide would bump every folder at once, which reads to an operator
    as a mailbox-level fault instead of one expired token.
    """
    state = WatchState()
    state.record_cursor(INBOX_FOLDER_ID, RESUME_LINK)
    state.record_cursor(ARCHIVE_FOLDER_ID, NEXT_DELTA_LINK)
    _queue_sync_state_not_found(recorder)

    changes = provider.poll_changes(RESUME_LINK)
    assert changes.resync_required is True
    state.resync_required(INBOX_FOLDER_ID)

    assert state.generation_for(INBOX_FOLDER_ID) == 1
    assert state.generation_for(ARCHIVE_FOLDER_ID) == 0
    assert state.generation_for(None) == 0


def test_the_delta_module_logs_under_the_same_provider_name() -> None:
    """The duplicated literal cannot drift from the one the adapter uses.

    ``outlook_delta`` restates the provider name instead of importing it,
    because ``outlook`` imports ``outlook_delta`` and taking the name back the
    other way would be a circular import. A restatement is only safe if
    something fails when the two disagree -- this is that something.
    """
    from inbox_zero_connector.providers.outlook import delta as outlook_delta
    from inbox_zero_connector.providers.outlook import provider as outlook

    assert outlook_delta.PROVIDER_LOG_NAME == outlook.PROVIDER


# ---------------------------------------------------------------------------
# Scope and folder lifecycle -- the cases that justify a stream PER FOLDER
#
# Everything above this banner drives ONE stream. These drive the consequences
# of there being SEVERAL: a folder that stops existing, a message that changes
# which folder it is in, and a message that leaves the covered set entirely.
# Each is a case timestamp polling could not express, because a timestamp asks
# "what is newer than T" of a mailbox, and every question here is about WHICH
# CONTAINER a thing is in -- which a timestamp does not carry.
# ---------------------------------------------------------------------------

CLIENTS_FOLDER_ID = "F_CLIENTS"
"""A third in-scope folder, used as the one that DISAPPEARS mid-run."""

VANISHED_FOLDER_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_CLIENTS/messages/delta"
    "?$deltatoken=clients-round-1"
)

ARCHIVE_DELTA_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_ARCHIVE/messages/delta"
    "?$deltatoken=archive-round-2"
)


def _queue_gone_folder(recorder: Recorder) -> None:
    """Make the NEXT delta GET answer ``404`` for a folder that no longer exists.

    Written as a route override in this file rather than through
    ``recorder.get_status``, and the reason is the whole point of the case.
    ``Recorder._delta`` serves EVERY queued non-200 status with the body
    ``{"error": {"code": "SyncStateNotFound"}}``, which is correct for the 410
    the tests above need and wrong here: ``_is_sync_state_gone`` matches that
    code on ANY 4xx, so a 404 queued that way would come back as
    ``resync_required`` and the test would assert the opposite of a deleted
    folder. A deleted folder is NOT a refused token -- one says "start this
    stream over", the other says "this stream has no subject any more" -- and a
    double that could not tell them apart would make them look identical.

    The override wraps ``recorder.handle`` instead of replacing it, so every
    non-delta route still behaves normally and the request log still records
    the call.
    """
    inner = recorder.handle

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages/delta") and "F_CLIENTS" in str(request.url):
            recorder.requests.append(request)
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        return inner(request)

    recorder.handle = _handle  # type: ignore[method-assign]


def _reprovision(recorder: Recorder) -> OutlookProvider:
    """A provider bound to ``recorder`` AFTER its handler has been overridden.

    ``httpx.MockTransport`` captures the handler by value at construction, so
    the ``provider`` fixture is already holding the original bound method by the
    time a test overrides it. Rebuilding the client is what makes the override
    take effect, and doing it here keeps that mechanic in one place instead of
    in each test that needs a failing route.
    """
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(recorder.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    return OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)


def test_poll_changes_reports_a_deleted_folder_as_a_failure_not_a_resync(
    recorder: Recorder,
) -> None:
    """A folder that stopped existing is a bounded ``ProviderError``, not a flag.

    The two ways a delta stream can stop being usable have OPPOSITE recoveries
    and must not collapse into one answer. A refused token (410) means the
    position is gone but the folder is not, and the recovery is a cold start of
    the same folder. A deleted folder (404) means there is nothing left to cold
    start -- re-walking it would 404 again forever -- and the recovery is for
    the caller to drop the folder from its set. An adapter that answered
    ``resync_required`` here would send the watcher into exactly that loop, with
    no error and no log line saying why the round never produced anything.
    """
    _queue_gone_folder(recorder)
    provider = _reprovision(recorder)

    with pytest.raises(ProviderError) as caught:
        provider.poll_changes(VANISHED_FOLDER_LINK)

    assert "404" in str(caught.value)


def test_a_404_on_one_folder_does_not_disturb_another_folders_round(
    recorder: Recorder,
) -> None:
    """The vanished folder fails ALONE; the next folder's round still completes.

    This is the containment property per-folder delta exists to provide, and it
    is not implied by the test above: one folder failing is only useful if the
    failure is SCOPED, and a shared client, a shared retry budget, or a cached
    transport error are all ways one folder's 404 could poison the calls after
    it. The two rounds are driven through the SAME provider on purpose -- a test
    that built a fresh provider for the second folder would prove nothing about
    the first one's effect, because there would be nothing left to affect.
    """
    _queue_gone_folder(recorder)
    _queue_final_page(recorder, [recorder.thread[0]], ARCHIVE_DELTA_LINK)
    provider = _reprovision(recorder)

    with pytest.raises(ProviderError):
        provider.poll_changes(VANISHED_FOLDER_LINK)
    archive = provider.poll_changes(NEXT_DELTA_LINK)

    assert [message.id for message in archive.messages] == ["m1"]
    assert archive.cursor == ARCHIVE_DELTA_LINK
    assert archive.resync_required is False


def test_a_404_on_one_folder_leaves_every_other_folder_cursor_untouched(
    recorder: Recorder,
) -> None:
    """A deleted folder invalidates no OTHER folder's stored position.

    The cursor-side half of containment, and the one that costs real money when
    it is wrong. ``WatchState`` keys a cursor per container, so a 404 handled as
    mailbox-wide would discard positions for folders that were never asked and
    force a full re-read of each of them. The generation counters are asserted
    at zero as well: a deleted folder is not a resync of anything, so nothing
    may report a thrown-away stream to an operator reading that counter.
    """
    state = WatchState()
    state.record_cursor(INBOX_FOLDER_ID, RESUME_LINK)
    state.record_cursor(ARCHIVE_FOLDER_ID, NEXT_DELTA_LINK)
    state.record_cursor(CLIENTS_FOLDER_ID, VANISHED_FOLDER_LINK)
    _queue_gone_folder(recorder)
    provider = _reprovision(recorder)

    with pytest.raises(ProviderError):
        provider.poll_changes(VANISHED_FOLDER_LINK)

    inbox = state.cursor_for(INBOX_FOLDER_ID)
    archive = state.cursor_for(ARCHIVE_FOLDER_ID)
    assert inbox is not None and inbox.token == RESUME_LINK
    assert archive is not None and archive.token == NEXT_DELTA_LINK
    assert state.generation_for(INBOX_FOLDER_ID) == 0
    assert state.generation_for(ARCHIVE_FOLDER_ID) == 0


def test_a_move_between_two_in_scope_folders_is_observed_by_the_destination(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A moved message appears on the DESTINATION folder's stream, reparented.

    This is the case a timestamp poll cannot see at all. A move changes no
    timestamp -- ``receivedDateTime`` is when the message ARRIVED, and it does
    not move when the message does -- so a poll asking "what is newer than T"
    returns nothing and the message is silently never classified in its new
    home. Delta reports the message as a change on the destination stream, and
    the reparent is legible because ``parentFolderId`` came back as the new
    folder.
    """
    moved = dict(recorder.thread[0])
    moved["parentFolderId"] = ARCHIVE_FOLDER_ID
    _queue_final_page(recorder, [moved], ARCHIVE_DELTA_LINK)

    changes = provider.poll_changes(NEXT_DELTA_LINK)

    assert [message.id for message in changes.messages] == ["m1"]
    assert changes.cursor == ARCHIVE_DELTA_LINK
    assert str(recorder.requests[0].url) == NEXT_DELTA_LINK


def test_a_move_out_of_a_folder_is_reported_by_the_source_as_a_removal(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The SOURCE stream reports the departure, and the adapter drops it.

    Graph reports a message leaving a folder as an ``@removed`` record on that
    folder's stream, and ``_added_messages`` skips it. That skip is the correct
    behaviour rather than a gap: this watcher's entire output is a label and a
    move, both additive, so a record saying a message is no longer here names
    no action it could take. The assertion is paired with a live arrival on the
    same page so a filter that dropped EVERYTHING would fail here too -- an
    empty result would otherwise satisfy the removal half by accident.
    """
    stayed = recorder.thread[0]
    departed = {"id": "m2", "@removed": {"reason": "changed"}}
    _queue_final_page(recorder, [stayed, departed], NEXT_DELTA_LINK)

    changes = provider.poll_changes(RESUME_LINK)

    assert [message.id for message in changes.messages] == ["m1"]
    assert changes.cursor == NEXT_DELTA_LINK


def test_the_scope_gate_refuses_a_folder_the_message_was_moved_into(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Delta REPORTS the move; ``MailboxScope.covers`` is what refuses it.

    The two are deliberately separate and this test drives the seam. The delta
    stream is not a scope decision -- it hands back whatever changed on the
    folder it was pointed at -- so a connector that trusted the feed alone would
    read a message the client excluded the moment somebody dragged it into
    ``Client Matters``. Scope is re-asked about the message's CURRENT container,
    not the one it was in when the round started, because the container is the
    only thing that changed.
    """
    scope = MailboxScope(mode="all_except", excluded_folders=("Client Matters",))
    moved = dict(recorder.thread[0])
    moved["parentFolderId"] = "F_CLIENT_MATTERS"
    _queue_final_page(recorder, [moved], NEXT_DELTA_LINK)

    changes = provider.poll_changes(RESUME_LINK)

    assert [message.id for message in changes.messages] == ["m1"]
    assert scope.covers("Client Matters") is False
    assert scope.covers("Archive", well_known="archive") is False
    assert scope.covers("Action") is True


def test_a_message_moved_out_of_scope_is_never_un_labelled(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Refusing a message is the WHOLE response. There is no removal to run.

    The tempting next step after "this message is now out of scope" is to undo
    the label the connector applied while it was in scope, and the design has no
    primitive for it BY CHOICE: ``tag_message`` is additive and documented never
    to remove a label or clear a category the owner set (``base.py``:536), and
    the watcher's ``FORBIDDEN_METHODS`` fences off the rest of the write
    surface. The invariant asserted here is the mechanical one -- the round that
    observes an out-of-scope message issues GETs and nothing else, so no
    un-labelling could have happened whatever the scope answer was.
    """
    scope = MailboxScope(mode="only_listed", included_folders=("Action",))
    moved = dict(recorder.thread[0])
    moved["parentFolderId"] = "F_CLIENT_MATTERS"
    _queue_final_page(recorder, [moved], NEXT_DELTA_LINK)

    changes = provider.poll_changes(RESUME_LINK)

    assert changes.messages != ()
    assert scope.covers("Client Matters") is False
    assert {request.method for request in recorder.requests} == {"GET"}
    assert recorder.patch_bodies == []
    assert "tag_message" not in FORBIDDEN_METHODS


def test_the_watcher_holds_no_primitive_that_could_remove_a_label() -> None:
    """The absence is checked against the interface, not asserted in prose.

    ``covers`` returning False is only a containment guarantee if there is
    nothing the watcher could call to act on the refusal destructively. This
    reads the provider interface and asserts that the write surface is the two
    additive operations plus the move -- so a later commit adding an
    ``untag_message`` or a ``delete_message`` fails HERE, at the reason, rather
    than passing quietly because no existing test happened to name it.
    """
    writes = {
        name
        for name in vars(MailProvider)
        if not name.startswith("_")
        and any(verb in name for verb in ("tag", "delete", "remove", "move", "file", "set_"))
    }

    assert writes == {"tag_message", "file_message", "set_important"}
    # Sprint 4: ``create_draft`` left FORBIDDEN_METHODS together with its ledger
    # idempotency gate. ``set_important`` did not, and that is what this line
    # still guards.
    assert set(FORBIDDEN_METHODS) == {"set_important"}


# ---------------------------------------------------------------------------
# The expired ACCESS token -- distinct from the expired DELTA token above
#
# Everything above this banner is about the delta position. These two are about
# the CREDENTIAL that fetches it, and the two failures look nothing alike:
# Graph answers a refused delta token with 410/``SyncStateNotFound`` and a
# refused access token with 401/``InvalidAuthenticationToken``. The 410 is
# ordinary operation and comes back as a flag; the 401 is a stale credential
# and is supposed to come back as a silent refresh plus one retry, which is
# what ``reauth.with_reauth_once`` does for every OTHER Graph read in the
# adapter. ``poll_changes`` (provider.py:1128) calls into this module directly
# and is the ONE read that bypasses it.
# ---------------------------------------------------------------------------

STALE_ACCESS_SENTINEL = "stale-mock-transport-authenticates-nothing"
"""Not a credential. Reaches ``httpx.MockTransport`` and nowhere else.

Named the same way ``_outlook_harness.MOCK_TRANSPORT_SENTINEL`` is, and for the
same reason: flake8-bandit's hardcoded-password rule stays ON for the rest of
the suite instead of being suppressed here, which would blind it to a real one.
"""

FRESH_ACCESS_SENTINEL = "fresh-mock-transport-authenticates-nothing"
"""The value a successful refresh installs, so the retry can be told apart."""


class _UnauthorizedThenServed:
    """A delta endpoint that answers 401 for its first N calls, then normally.

    Written here rather than through ``recorder.get_status`` because
    ``Recorder._delta`` serves EVERY queued non-200 with the body
    ``{"error": {"code": "SyncStateNotFound"}}`` -- correct for the 410 cases
    above and fatally wrong here, since ``_is_sync_state_gone`` matches that
    code on ANY 4xx and a 401 queued that way would come back as
    ``resync_required``. A refused ACCESS token is not a refused DELTA token,
    and a double that could not tell them apart would make them look identical.

    Records the ``Authorization`` header of every request, which is what makes
    "the retry carried the NEW token" a provable claim rather than an inference
    from the call succeeding.
    """

    def __init__(self, recorder: Recorder, unauthorized_calls: int) -> None:
        self.recorder = recorder
        self.remaining = unauthorized_calls
        self.authorizations: list[str | None] = []
        self._inner: Callable[[httpx.Request], httpx.Response] = recorder.handle

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages/delta"):
            self.recorder.requests.append(request)
            self.authorizations.append(request.headers.get("Authorization"))
            if self.remaining > 0:
                self.remaining -= 1
                return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return self._inner(request)


class _ReauthSpy:
    """Stands in for MSAL's silent refresh and records how it was called.

    Installs a client carrying ``FRESH_ACCESS_SENTINEL`` over the SAME
    transport, because ``reauth.refresh_credential`` clears the cached client
    precisely so the next ``_http`` rebuilds the ``Authorization`` header from
    the new token. A stub that left the old client in place would let an
    implementation that only updated ``_token`` pass.
    """

    def __init__(self, provider: OutlookProvider, handler: _UnauthorizedThenServed) -> None:
        self.provider = provider
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def __call__(self, interactive: bool = False) -> str:
        self.calls.append({"interactive": interactive})
        self.provider._token = FRESH_ACCESS_SENTINEL
        self.provider._client = _client_over(self.handler, FRESH_ACCESS_SENTINEL)
        return "spy@example.com"


def _client_over(handler: _UnauthorizedThenServed, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {token}"},
    )


def _provider_with_expired_access_token(
    recorder: Recorder, unauthorized_calls: int
) -> tuple[OutlookProvider, _UnauthorizedThenServed, _ReauthSpy]:
    """A provider whose delta GET is refused ``unauthorized_calls`` times.

    Built AFTER the handler exists, because ``httpx.MockTransport`` captures
    its handler by value at construction -- the ``provider`` fixture is already
    holding the recorder's original bound method, so the client has to be
    rebuilt for the override to take effect.
    """
    handler = _UnauthorizedThenServed(recorder, unauthorized_calls)
    provider = OutlookProvider(
        client=_client_over(handler, STALE_ACCESS_SENTINEL), token=STALE_ACCESS_SENTINEL
    )
    spy = _ReauthSpy(provider, handler)
    provider.authenticate = spy  # type: ignore[method-assign]
    return provider, handler, spy


AFTER_REFRESH_DELTA_LINK = "https://graph.microsoft.invalid/delta?$deltatoken=after-the-refresh"
"""The deltaLink the round that SURVIVED the refresh comes back with.

Named for the ROUND rather than for the token inside it -- the same shape every
other link constant in this file uses -- which keeps flake8-bandit's
hardcoded-password rule ON here instead of suppressed: that rule reads the
NAME, so a ``..._TOKEN_...`` constant holding a url trips it, and a per-line
suppression would blind it to a real credential later.
"""


def test_poll_changes_recovers_from_an_expired_token_once(recorder: Recorder) -> None:
    """The delta stream is the watcher's heartbeat and must survive an expiry.

    It is also the ONE Graph read that bypassed the 401 refresh: every other
    call funnels through ``_get``/``_post``/``_patch`` and is wrapped in
    ``with_reauth_once``, while ``poll_changes`` calls this module directly. A
    Graph access token expires in roughly an hour, so an unattended watcher
    died within the hour of its token with a bare ``ProviderError``.
    """
    provider, handler, spy = _provider_with_expired_access_token(recorder, 1)
    _queue_final_page(recorder, [], AFTER_REFRESH_DELTA_LINK)

    changes = provider.poll_changes(None)

    assert changes.cursor == AFTER_REFRESH_DELTA_LINK
    assert [call["interactive"] for call in spy.calls] == [False]
    assert handler.authorizations == [
        f"Bearer {STALE_ACCESS_SENTINEL}",
        f"Bearer {FRESH_ACCESS_SENTINEL}",
    ]


def test_poll_changes_fails_loud_when_the_refreshed_token_is_also_refused(
    recorder: Recorder,
) -> None:
    """A dead credential stops the round; it does not spin against Entra.

    The first 401 says the access token is stale. A second, after a fresh one
    was minted and sent, says the CREDENTIAL is dead -- revoked consent, an
    expired refresh token, a Conditional Access policy -- and none of those
    improve with another attempt. The still-exactly-once assertion is the load
    bearing half: the raise alone already passes today, when the refresh never
    runs at all.
    """
    provider, _handler, spy = _provider_with_expired_access_token(recorder, 2)

    with pytest.raises(ProviderError):
        provider.poll_changes(RESUME_LINK)

    assert len(spy.calls) == 1
