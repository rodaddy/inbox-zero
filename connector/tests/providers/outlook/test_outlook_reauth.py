"""The 401 refresh: one silent re-authentication, one retry, then fail loud.

The defect these cover killed the Sprint 2 live soak twelve minutes in.
``OutlookProvider`` baked a single access token into a cached ``httpx.Client``
and never refreshed it, so every Graph call after that token's expiry answered
``401 InvalidAuthenticationToken`` forever.

No network and no credential: every test here drives a
``httpx.MockTransport`` and stubs ``authenticate`` outright, so what is being
asserted is the adapter's control flow -- when it refreshes, how many times,
and whether the retried request carries the NEW token -- and never MSAL's.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from inbox_zero_connector.providers.outlook.categories import GraphStatusError
from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE, OutlookProvider
from inbox_zero_connector.providers.outlook.reauth import graph_client

STALE_SENTINEL = "stale-mock-transport-authenticates-nothing"
"""Not a credential. Reaches ``httpx.MockTransport`` and nowhere else.

Named WITHOUT a credential-shaped word for the same reason
``_outlook_harness.MOCK_TRANSPORT_SENTINEL`` is: flake8-bandit's
hardcoded-password rule stays ON for the rest of the suite instead of being
suppressed here, which would blind it to a real one.
"""

FRESH_SENTINEL = "fresh-mock-transport-authenticates-nothing"
"""The value a successful refresh installs, so the retry can be told apart."""


class GraphStub:
    """A mocked Graph endpoint that serves a queued status per request.

    Records the ``Authorization`` header of every request it sees, which is
    what makes "the retry carried the new token" a PROVABLE claim rather than
    an inference from the call succeeding.
    """

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        """One status per call, in order. Exhausted means 200 from then on."""

        self.authorizations: list[str | None] = []
        """The ``Authorization`` header of each request, in order."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.authorizations.append(request.headers.get("Authorization"))
        status = self.statuses.pop(0) if self.statuses else 200
        if status >= 400:
            return httpx.Response(status, json={"error": {"code": "InvalidAuthenticationToken"}})
        return httpx.Response(200, json={"value": [{"id": "m1"}]})


def _client(stub: GraphStub, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=GRAPH_BASE,
        headers={"Authorization": f"Bearer {token}"},
        transport=httpx.MockTransport(stub.handle),
    )


class ReauthSpy:
    """Stands in for MSAL's silent refresh and records how it was called.

    Installs a client carrying ``FRESH_SENTINEL`` over the SAME stub, because the
    real ``_reauthenticate`` clears the cached client precisely so the next
    ``_http`` rebuilds the ``Authorization`` header from the new token. A stub
    that left the old client in place would let a broken implementation pass.
    """

    def __init__(self, provider: OutlookProvider, stub: GraphStub) -> None:
        self.provider = provider
        self.stub = stub
        self.calls: list[dict[str, Any]] = []
        """One entry per invocation, carrying the kwargs it was given."""

    def __call__(self, interactive: bool = False) -> str:
        self.calls.append({"interactive": interactive})
        self.provider._token = FRESH_SENTINEL
        self.provider._client = _client(self.stub, FRESH_SENTINEL)
        return "spy@example.com"


def _provider_with_spy(statuses: list[int]) -> tuple[OutlookProvider, GraphStub, ReauthSpy]:
    stub = GraphStub(statuses)
    provider = OutlookProvider(client=_client(stub, STALE_SENTINEL), token=STALE_SENTINEL)
    spy = ReauthSpy(provider, stub)
    provider.authenticate = spy  # type: ignore[method-assign]
    return provider, stub, spy


class TokenHolder:
    """The whole of ``Refreshable`` and nothing else, for testing the factory.

    ``graph_client`` is declared against a Protocol precisely so it can be
    exercised without an ``OutlookProvider``, so these two tests take it up on
    that: no MSAL, no transport, no mailbox -- only the headers the factory
    bakes into the client it hands back.
    """

    def __init__(self, token: str | None = None, client: httpx.Client | None = None) -> None:
        self._token = token
        self._client = client

    def authenticate(self, interactive: bool = False) -> str:
        """Never reached: ``_token`` is pre-set, so the factory does not ask."""
        raise AssertionError("graph_client must not authenticate when a token is present")


def test_the_built_client_asks_graph_for_immutable_ids() -> None:
    """Graph's default message ids are FOLDER-dependent, and this kit MOVES mail.

    Every id the ledger records dies at the moment of filing, because the move
    that files the message is also what changes its id. The ``ImmutableId``
    preference keeps ids stable across moves, and it has to ride on the client
    at CONSTRUCTION so every request carries it rather than only the ones some
    caller remembered to decorate.
    """
    client = graph_client(
        TokenHolder(token=STALE_SENTINEL),
        "https://graph.microsoft.invalid",
        httpx.Timeout(5),
    )

    assert client.headers.get("Prefer") == 'IdType="ImmutableId"'


def test_an_injected_client_is_not_given_the_preference_header() -> None:
    """An injected client is returned verbatim, header additions included.

    That contract is what lets the whole Graph suite run on
    ``httpx.MockTransport``: a test builds the exact transport it wants to
    assert against, and the factory is a pass-through. Adding the preference
    on the way out would mutate a caller's object and make the injected client
    something other than what the caller built.
    """
    injected = httpx.Client(base_url="https://graph.microsoft.invalid")

    returned = graph_client(
        TokenHolder(token=STALE_SENTINEL, client=injected),
        "https://graph.microsoft.invalid",
        httpx.Timeout(5),
    )

    assert returned is injected
    assert "Prefer" not in returned.headers


def test_a_401_refreshes_the_token_once_and_the_retry_succeeds() -> None:
    """The whole point: an expired token becomes a successful call, silently.

    RED before the fix -- the first 401 propagated as ``GraphStatusError`` and
    the watcher loop died on it.
    """
    provider, stub, spy = _provider_with_spy([401])

    messages = provider.list_inbox(limit=1)

    assert [message.id for message in messages] == ["m1"]
    assert [call["interactive"] for call in spy.calls] == [False]
    assert len(stub.authorizations) == 2


def test_a_second_401_fails_loud_without_a_second_refresh() -> None:
    """A dead credential must stop the process, not spin against Entra.

    The first 401 says the access token is stale; a second, after a fresh one
    was minted and sent, says the CREDENTIAL is dead -- revoked consent, an
    expired refresh token, a Conditional Access policy. None of those improve
    with another attempt, and looping would leave the mailbox unwatched while
    the log stayed busy.
    """
    provider, _stub, spy = _provider_with_spy([401, 401])

    with pytest.raises(GraphStatusError) as raised:
        provider.list_inbox(limit=1)

    assert raised.value.status == 401
    assert len(spy.calls) == 1


def test_a_403_does_not_trigger_a_refresh() -> None:
    """A permission refusal is a configuration fact; a new token cannot fix it.

    Refreshing on it would turn a clear, immediately actionable scope error
    into a slow one that also touches the credential store for no reason.
    """
    provider, _stub, spy = _provider_with_spy([403])

    with pytest.raises(GraphStatusError) as raised:
        provider.list_inbox(limit=1)

    assert raised.value.status == 403
    assert spy.calls == []


def test_the_retried_request_carries_the_refreshed_token() -> None:
    """The load-bearing half: the refresh must REBUILD the transport.

    The access token is baked into an ``Authorization`` header when the client
    is constructed, so a refresh that updated only ``self._token`` would resend
    the expired one and see the same 401 twice. Asserted over the recorded
    headers rather than over the call succeeding, because a stub could be made
    to succeed either way.
    """
    provider, stub, _spy = _provider_with_spy([401])

    provider.list_inbox(limit=1)

    assert stub.authorizations == [f"Bearer {STALE_SENTINEL}", f"Bearer {FRESH_SENTINEL}"]


def test_a_401_on_a_patch_is_retried_too() -> None:
    """Writes get the refresh as well, and that is safe SPECIFICALLY for 401.

    ``_patch`` is otherwise never retried, because a timed-out write may have
    already landed. A 401 is different in kind: Graph refused the request
    before acting, so the resource is provably untouched and there is no
    duplicate a retry could create.
    """
    provider, stub, spy = _provider_with_spy([401])

    provider.set_important("m1", important=True)

    assert len(spy.calls) == 1
    assert stub.authorizations[-1] == f"Bearer {FRESH_SENTINEL}"


def test_the_refresh_never_asks_for_a_terminal() -> None:
    """Unattended by contract: an interactive prompt would hang the watcher.

    Under the watcher loop and the MCP server there is no terminal, so a
    device-code prompt has nobody to answer it and no timeout to end it.
    """
    provider, _stub, spy = _provider_with_spy([401])

    provider.list_inbox(limit=1)

    assert all(call["interactive"] is False for call in spy.calls)
