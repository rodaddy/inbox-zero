"""A message that is deleted BETWEEN being listed and being fetched.

Gmail's read paths are two calls, never one. ``users.messages.list`` and
``users.history.list`` both answer with ids only, so every id is fetched again
by ``users.messages.get`` to obtain headers. The gap between the two calls is
real time on a live mailbox, and the owner may delete a message inside it --
at which point the ``get`` answers ``404 notFound`` for an id the provider was
just told about.

That 404 is ORDINARY OPERATION, not a failure. It says the mailbox moved on,
which is precisely what a watcher exists to observe. Before this file the
whole round died on it: ``cold_start`` and ``poll`` fanned out over the ids
outside their ``except HttpError`` block, so the SDK error escaped raw past
``ProviderError`` entirely, and ``list_inbox`` fanned out INSIDE its block and
turned one vanished message into a failure of the whole listing. Either way a
single deleted message stopped every OTHER message on the page from being
read, on every subsequent round, until someone noticed.

The 404 on ``users.history.list`` is a DIFFERENT 404 and keeps its own
meaning -- the stored POSITION aged out, answered with ``resync_required``.
Both are asserted here so neither handler can be widened into the other's
territory without a test failing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from typing import Any

import pytest

from _gmail_harness import (
    FakeService,
    GmailProvider,
)
from _gmail_harness import (
    provider as provider,
)
from _gmail_harness import (
    store as store,
)
from inbox_zero_connector.providers.base import ProviderError


def _not_found() -> Any:
    """A real ``HttpError`` carrying ``404``, built without a network.

    The adapter reads the code off ``resp.status``, so a stand-in response
    with that attribute is all its check can see. Built from the SDK's own
    class rather than a bespoke exception, because the thing under test is
    whether the adapter recognises what googleapiclient actually raises.
    """
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 404
        reason = "Not Found"

    return HttpError(_Resp(), b'{"error": {"code": 404, "message": "Not found"}}')


class _VanishingMessages:
    """``users.messages`` where ``get`` raises 404 for ids in ``store["gone"]``.

    Wraps the harness resource rather than replacing it so every other call --
    ``list``, ``modify``, and the store lookups behind them -- keeps the
    harness's own behaviour. Only the one path under test diverges.
    """

    def __init__(self, inner: Any, store: dict[str, Any]) -> None:
        self._inner = inner
        self._store = store

    def list(self, **kwargs: Any) -> Any:
        return self._inner.list(**kwargs)

    def modify(self, **kwargs: Any) -> Any:
        return self._inner.modify(**kwargs)

    def get(self, **kwargs: Any) -> Any:
        if kwargs.get("id") in self._store.get("gone", ()):
            self._inner._log.append(("messages.get", kwargs))
            raise _not_found()
        return self._inner.get(**kwargs)


class _VanishingService(FakeService):
    """``FakeService`` whose ``users().messages()`` can lose a message."""

    def users(self) -> Any:
        users = super().users()
        messages = users.messages

        def _messages() -> Any:
            return _VanishingMessages(messages(), self._store)

        users.messages = _messages  # type: ignore[method-assign]
        return users


@pytest.fixture
def vanishing(store: dict[str, Any]) -> GmailProvider:
    """A provider whose mailbox has already lost ``m1`` by fetch time.

    ``m1`` and not ``m2``: the listing fixture reports both, so the deleted one
    being FIRST is what proves the loop continues past it rather than merely
    surviving a failure on its last element.
    """
    store["gone"] = {"m1"}
    return GmailProvider(service=_VanishingService(store))


def test_list_inbox_skips_a_message_deleted_between_listing_and_fetch(
    vanishing: GmailProvider, store: dict[str, Any]
) -> None:
    """One vanished id must not cost the caller the rest of the page.

    The listing named ``m1`` and ``m2``. ``m1`` is gone by the time its
    headers are asked for, and the correct answer is ``m2`` -- not an
    exception, and not an empty list either, which is what a handler wrapped
    around the whole loop instead of around one fetch would produce.
    """
    messages = vanishing.list_inbox(limit=10)

    assert [m.id for m in messages] == ["m2"]


def test_a_cold_start_skips_a_message_deleted_between_listing_and_fetch(
    vanishing: GmailProvider,
) -> None:
    """The same gap on the watcher's first round, which has no cursor.

    A cold start reads the profile position and then a bounded inbox sample,
    so it fans out over ids exactly the way ``list_inbox`` does and fails the
    same way. The cursor must still be returned: the position the profile
    reported is unaffected by a message the mailbox no longer has.
    """
    changes = vanishing.poll_changes(None)

    assert [m.id for m in changes.messages] == ["m2"]
    assert changes.cursor == "9001"
    assert changes.skipped_missing == 1


def test_a_history_round_skips_a_message_deleted_between_listing_and_fetch(
    vanishing: GmailProvider, store: dict[str, Any]
) -> None:
    """The steady-state round: a history page naming an id already deleted.

    Gmail reports ``messagesAdded`` for a message and the owner deletes it
    before the next poll -- a routine ordering on a busy mailbox. The page's
    OTHER ids must still be read, and the round must still commit its cursor,
    because the page WAS fully processed; a message that no longer exists is
    not an unread change.
    """
    store["gone"] = {"m1"}
    store["history"] = {
        "historyId": "9100",
        "history": [
            {"messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "m2"}}]},
        ],
    }

    changes = vanishing.poll_changes("9001")

    assert [m.id for m in changes.messages] == ["m2"]
    assert changes.cursor == "9100"
    assert changes.skipped_missing == 1


def test_a_round_with_nothing_missing_reports_no_skips(provider: GmailProvider) -> None:
    """The complement, so ``skipped_missing`` cannot be a constant.

    Without this a counter hardwired to 1 -- or to any value -- would pass
    every assertion above.
    """
    changes = provider.poll_changes("9001")

    assert [m.id for m in changes.messages] == ["m2"]
    assert changes.skipped_missing == 0


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    """Every WARNING loguru emits during the test, as raw message strings.

    A loguru sink rather than ``caplog``: the app logs through loguru, whose
    records never reach the stdlib ``logging`` handlers pytest captures, so a
    ``caplog`` assertion here would pass on an adapter that logged nothing at
    all. Teardown tolerates the sink already being gone, because a test that
    reconfigures logging removes it as a side effect.
    """
    from loguru import logger

    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING", format="{message}", diagnose=False)
    try:
        yield captured
    finally:
        with suppress(ValueError):
            logger.remove(sink_id)


def test_a_vanished_message_is_named_in_a_warning(
    vanishing: GmailProvider, warnings: list[str]
) -> None:
    """A skip is silent data loss unless something says which id was dropped.

    WARNING rather than ERROR: the run is correct and continues, so ERROR
    would train the operator to ignore the level. The id is the whole payload
    -- a Gmail message id is an opaque handle and carries no mailbox content,
    unlike the subject or sender that ``RoundSummary`` forbids outright.
    """
    vanishing.poll_changes(None)

    blob = "\n".join(warnings)
    assert "m1" in blob, f"the skipped id was never named: {blob!r}"


def test_a_non_404_failure_on_a_message_fetch_still_fails_the_round(
    vanishing: GmailProvider, store: dict[str, Any]
) -> None:
    """The complement to the skip, so "404 is a skip" is not read as "nothing fails".

    A 500 on a message fetch is a genuine transport failure. Swallowing it
    would report a short page as a complete one and let the watcher commit a
    cursor past changes it never actually read -- the silent loss the cursor
    rule exists to prevent, arriving through the fan-out instead.
    """
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 400
        reason = "Bad Request"

    class _AlwaysBad:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def list(self, **kwargs: Any) -> Any:
            return self._inner.list(**kwargs)

        def get(self, **kwargs: Any) -> Any:
            raise HttpError(_Resp(), b'{"error": {"code": 400}}')

    class _BadService(FakeService):
        def users(self) -> Any:
            users = super().users()
            messages = users.messages
            users.messages = lambda: _AlwaysBad(messages())  # type: ignore[method-assign]
            return users

    provider = GmailProvider(service=_BadService(store))
    with pytest.raises(ProviderError):
        provider.poll_changes(None)


def test_a_refused_history_position_is_still_a_resync_not_a_skip(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """The two 404s must not merge.

    ``users.history.list`` answering 404 means the stored POSITION aged out,
    and the answer is a resync. If the message-fetch skip were widened to
    cover it, an aged-out cursor would be reported as a quiet round with a
    storable cursor -- and the watcher would resume from a position Gmail has
    already refused.
    """
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 404
        reason = "Not Found"

    store["history_error"] = HttpError(_Resp(), b'{"error": {"code": 404}}')

    changes = provider.poll_changes("1")

    assert changes.resync_required is True
    assert changes.cursor is None
    assert changes.messages == ()
