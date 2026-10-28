"""F3: retryable statuses must actually retry -- and mutations must NOT.

Split out of ``test_outlook_provider.py`` on 2026-08-19 (500-code-line ceiling).
Owns the retry banner and F10 (``/move`` assigns a NEW id, so the ledger must
not record a dead one), which sit together because both are about what a
mutation is allowed to do more than once.

The ``_no_retry_sleep`` autouse fixture lives HERE and nowhere else. It belonged
to this section in the original file, and hoisting it into a shared conftest
during the split would have silently applied it to tests that never had it.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from _outlook_harness import (
    MOCK_TRANSPORT_SENTINEL,
    Recorder,
    _message,
)
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.outlook.provider import (
    GRAPH_BASE,
    OutlookProvider,
)

# ---------------------------------------------------------------------------
# F3: retryable statuses must actually retry -- and mutations must NOT
# ---------------------------------------------------------------------------


def _retry_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry backoff waits seconds. A test suite must not.

    Patching the wait to zero keeps the ATTEMPT accounting real -- the thing
    under test -- while removing the wall-clock cost that would otherwise make
    these two tests the slowest in the suite.
    """
    from inbox_zero_connector.providers import retry as retry_module

    monkeypatch.setattr(retry_module, "INITIAL_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(retry_module, "MAX_WAIT_SECONDS", 0.0)


def test_a_retryable_status_on_a_read_is_actually_retried() -> None:
    """429 then 200 on a GET must succeed. Before the fix it raised on attempt 1.

    httpx does not raise on 4xx/5xx, so a raw call inside the retry wrapper
    returned a 429 Response as a SUCCESS and the policy never saw a status.
    """
    attempts: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, json={"error": {"message": "throttled"}})
        return httpx.Response(200, json={"value": [_message("m1", "First")]})

    provider = OutlookProvider(client=_retry_client(flaky), token=MOCK_TRANSPORT_SENTINEL)
    messages = provider.list_inbox()

    assert [m.id for m in messages] == ["m1"]
    assert len(attempts) == 2, "the 429 must have cost a second attempt"


def test_a_retryable_status_on_a_move_is_never_retried() -> None:
    """The ledger's 1:1 invariant: one action, one row, so no blind re-POST.

    A move that returned 429 may still have moved the message. Retrying is how
    one real change becomes two ledger rows.
    """
    moves: list[httpx.Request] = []

    def throttled_move(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mailFolders"):
            return httpx.Response(200, json={"value": [{"id": "F_EXIST", "displayName": "Action"}]})
        moves.append(request)
        return httpx.Response(429, json={"error": {"message": "throttled"}})

    provider = OutlookProvider(client=_retry_client(throttled_move), token=MOCK_TRANSPORT_SENTINEL)
    with pytest.raises(ProviderError, match="429"):
        provider.file_message("m1", "Action")

    assert len(moves) == 1, "a mutation must be attempted exactly once"


def test_a_retryable_status_on_a_patch_is_never_retried() -> None:
    patches: list[httpx.Request] = []

    def throttled_patch(request: httpx.Request) -> httpx.Response:
        patches.append(request)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    provider = OutlookProvider(client=_retry_client(throttled_patch), token=MOCK_TRANSPORT_SENTINEL)
    with pytest.raises(ProviderError, match="503"):
        provider.set_important("m1", True)

    assert len(patches) == 1, "a mutation must be attempted exactly once"


def test_a_non_retryable_status_on_a_read_is_not_retried() -> None:
    """403 is a scope fact, not a bad second. Retrying it just makes it slow."""
    attempts: list[int] = []

    def denied(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(403, json={"error": {"message": "Access denied"}})

    provider = OutlookProvider(client=_retry_client(denied), token=MOCK_TRANSPORT_SENTINEL)
    with pytest.raises(ProviderError, match="403"):
        provider.list_inbox()

    assert len(attempts) == 1


def test_a_read_exhausts_its_attempt_budget_then_raises() -> None:
    """Bounded, per the observability standard: 3 attempts, then the error."""
    from inbox_zero_connector.providers.retry import MAX_ATTEMPTS

    attempts: list[int] = []

    def always_throttled(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, json={"error": {"message": "throttled"}})

    provider = OutlookProvider(
        client=_retry_client(always_throttled), token=MOCK_TRANSPORT_SENTINEL
    )
    with pytest.raises(ProviderError, match="429"):
        provider.list_inbox()

    assert len(attempts) == MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# F10: /move assigns a NEW id -- the ledger must not record a dead one
# ---------------------------------------------------------------------------


def test_file_message_returns_the_post_move_id(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Graph moves are copy-then-delete: the destination id is a NEW id.

    The pre-move id is dead the instant the move lands, so a receipt carrying
    it cannot be resolved back to the message it describes.
    """
    result = provider.file_message("m1", "Action")
    assert result.filed_as == "Action"
    assert result.message_id == "moved", "the receipt must carry the post-move id"


def test_file_message_reports_an_unknown_id_rather_than_the_dead_one(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A move that returns no body reports UNKNOWN, never the pre-move id.

    Graph documents a 201 carrying the moved message, and a 204 is not a crash
    -- but it leaves the post-move id genuinely unknown. This used to fall back
    to the id we were handed on the reasoning that a stale id beats an empty
    one; issue #150 established that it does not. Graph's ``/move`` is
    copy-then-delete, so that id is DEAD, and a receipt carrying it is
    indistinguishable from a receipt naming a message that still exists --
    which is how ``reset restore`` ends up resolving a row to nothing with
    nothing to say about why. ``None`` is checkable.
    """

    def empty_move(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mailFolders"):
            return httpx.Response(200, json={"value": [{"id": "F_EXIST", "displayName": "Action"}]})
        return httpx.Response(204)

    provider = OutlookProvider(client=_retry_client(empty_move), token=MOCK_TRANSPORT_SENTINEL)
    result = provider.file_message("m1", "Action")
    assert result.message_id is None
    assert result.filed_as == "Action"


def test_file_message_result_is_still_the_folder_string_for_the_tool_layer(
    provider: OutlookProvider,
) -> None:
    """Back-compat: the tool layer reads the return as the applied folder name.

    ``server.py`` puts this straight into ``applied_as``. The richer result has
    to keep behaving like that string, or the fix breaks the receipt it exists
    to correct.
    """
    result = provider.file_message("m1", "Action")
    assert str(result) == "Action"
    assert result == "Action"
