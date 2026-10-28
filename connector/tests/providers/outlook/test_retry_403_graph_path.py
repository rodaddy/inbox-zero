"""Issue #45: the 403 rate-limit carve-out is unreachable on the Graph path.

``retry.is_retryable`` grants a 403 one extra question -- does the body name a
Google throttling reason? -- and on the Graph path that question is never
asked, because a Graph 403 never becomes an exception in the first place.
``providers/outlook/errors.py:167`` raises ``HTTPStatusError`` only for a
status in ``RETRYABLE_STATUS``, and 403 is not in that frozenset
(``providers/retry.py:82``). The 403 stays a plain ``httpx.Response`` and
travels on to ``_unwrap`` / ``_provider_error``.

These tests pin the REALITY, not an aspiration: a 403 through the Graph
attempt helper is attempted exactly once and surfaces as a ``ProviderError``,
while a 429 through the same helper spends the whole attempt budget. If a
future change puts 403 into ``RETRYABLE_STATUS``, the first test fails and the
carve-out's Gmail-only scope has to be re-argued deliberately.
"""

from __future__ import annotations

import httpx
import pytest

from _outlook_harness import MOCK_TRANSPORT_SENTINEL
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.outlook.delta import _delta_page
from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE
from inbox_zero_connector.providers.retry import MAX_ATTEMPTS
from inbox_zero_connector.utils.logging_config import ProviderCall


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry backoff waits seconds. A test suite must not.

    Same shape as ``test_outlook_retry.py``: the ATTEMPT accounting -- the
    thing under test -- stays real, only the wall clock goes.
    """
    from inbox_zero_connector.providers import retry as retry_module

    monkeypatch.setattr(retry_module, "INITIAL_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(retry_module, "MAX_WAIT_SECONDS", 0.0)


def _graph_client(status: int, attempts: list[int]) -> httpx.Client:
    """A Graph client whose every response is ``status``, counting attempts."""

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(status, json={"error": {"code": "Whatever"}})

    return httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )


def test_a_graph_403_is_never_retried_and_reaches_the_caller_as_a_response() -> None:
    """The carve-out is Gmail-only: a Graph 403 is not raised as retryable.

    ``_raise_if_retryable`` leaves it alone, so ``_delta_page`` RETURNS the 403
    response rather than raising -- the caller decides. One attempt, no
    exception out of the helper.
    """
    attempts: list[int] = []
    client = _graph_client(403, attempts)

    with ProviderCall("microsoft", "delta", method="GET", path="/delta") as call:
        response = _delta_page(call, lambda: client, "/me/mailFolders/inbox/messages/delta")

    assert response.status_code == 403, "the 403 must arrive as a plain response"
    assert len(attempts) == 1, "a Graph 403 must cost exactly one attempt"


def test_a_graph_429_through_the_same_helper_does_retry_and_then_raises() -> None:
    """The contrast case: 429 IS in ``RETRYABLE_STATUS``, so the same helper retries."""
    attempts: list[int] = []
    client = _graph_client(429, attempts)

    with (
        ProviderCall("microsoft", "delta", method="GET", path="/delta") as call,
        pytest.raises(ProviderError, match="429"),
    ):
        _delta_page(call, lambda: client, "/me/mailFolders/inbox/messages/delta")

    assert len(attempts) == MAX_ATTEMPTS, "a 429 must spend the whole attempt budget"
