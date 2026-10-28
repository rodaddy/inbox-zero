"""Every poll in a round names the stream it is polling (#143).

THE DEFECT THIS FILE GUARDS. ``poll_once`` read the stream's cursor with
``context.container_id`` and then called ``poll_changes`` WITHOUT it. On Gmail
that is invisible -- ``container_id`` is ignored there and the mailbox-wide feed
is the only feed. On Microsoft Graph it is the whole bug: Graph has no
mailbox-wide delta, so the container names the delta path on a cold start, and
an omitted one means ``me/mailFolders/inbox/messages/delta``. Every per-folder
stream therefore cold-started against the INBOX, processed the inbox once per
watched folder, never read the folder it was dispatched for, and stored the
inbox's delta link under the folder's cursor key.

The two assertions here are the two call sites. The initial poll is the cold
start that picks the path; the pagination poll continues the SAME round and must
not silently switch streams mid-round when a page token is replayed against a
provider that reads the container.

The second half of the fix is ``state.CURSOR_GENERATION`` and lives in
``test_watchstate.py``: cursors minted before this fix are poisoned, because
they are inbox delta links filed under a folder key.
"""

from __future__ import annotations

from _watch_harness import (
    PAGE_TOKEN,
    CountingClassifier,
    FakeProvider,
    _context,
    _message,
)
from inbox_zero_connector.providers.base import ChangeSet
from inbox_zero_connector.services.watch import round as watcher

FOLDER_ID = "AAMkAGI2-archive"


def test_the_initial_poll_names_the_rounds_container() -> None:
    """The cold start must select the dispatched folder's delta path, not the inbox."""
    provider = FakeProvider(ChangeSet(messages=(), cursor="delta-1"))
    context = _context(provider, classifier=CountingClassifier()).model_copy(
        update={"container_id": FOLDER_ID}
    )

    watcher.poll_once(context)

    assert provider.polled_containers == [FOLDER_ID], (
        "poll_changes must be told which stream it is polling; without it every "
        "per-folder Graph stream cold-starts against me/mailFolders/inbox"
    )


def test_the_pagination_poll_names_the_same_container() -> None:
    """Page two of a round stays on the stream page one opened."""
    pages = [
        ChangeSet(messages=(_message("m1"),), cursor=None, next_page_token=PAGE_TOKEN),
        ChangeSet(messages=(_message("m2"),), cursor="delta-2"),
    ]
    provider = FakeProvider(pages[0])

    def scripted(
        cursor: str | None, *, page_token: str | None = None, container_id: str | None = None
    ) -> ChangeSet:
        provider.polled.append(cursor)
        provider.polled_containers.append(container_id)
        return pages[len(provider.polled) - 1]

    provider.poll_changes = scripted  # type: ignore[method-assign]
    context = _context(provider, classifier=CountingClassifier()).model_copy(
        update={"container_id": FOLDER_ID}
    )

    watcher.poll_once(context)

    assert provider.polled_containers == [FOLDER_ID, FOLDER_ID], (
        "a round that switches streams between pages reads one folder's page one "
        "and another folder's page two"
    )


def test_a_mailbox_wide_round_still_polls_with_no_container() -> None:
    """Gmail's shape is unchanged: one stream, ``container_id=None``."""
    provider = FakeProvider(ChangeSet(messages=(), cursor="history-9"))

    watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    assert provider.polled_containers == [None]
