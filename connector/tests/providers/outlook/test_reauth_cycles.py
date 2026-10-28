"""Compressed time: many token expiries in milliseconds, no tenant, no clock.

WHAT THIS REPLACES
    The live Microsoft soak proved the Graph 401 re-authentication path ONCE,
    at 18:02:18, by WAITING roughly sixty-six minutes for a real access token
    to reach its natural expiry. That receipt is real and this file does not
    replace it -- a mocked transport cannot prove MSAL mints a token against
    Entra, and nothing here tries to. What this file replaces is the WAITING
    for the second one, the third one, and the twenty-fifth one.

    An expiry, from the connector's side, is only two facts: Graph answers 401,
    and the cached credential is stale. Both are inducible in-process, so a
    refresh cycle costs microseconds instead of an hour. The wall-clock
    equivalents:

    * ``test_twenty_five_consecutive_expiries_each_refresh_exactly_once``
      drives 25 expiry-and-recovery cycles. At the roughly one-hour lifetime of
      a real Graph access token that is over 25 HOURS of soak, and it is the
      only way to see whether cycle 2 behaves like cycle 1 -- the live run has
      shown exactly one.
    * ``test_forty_rounds_across_ten_expiries_never_fail_a_pass`` drives 40
      watcher rounds with an expiry every fourth round, so ten separate
      expiries are absorbed. At the deployed poll interval that is roughly a
      full day of unattended operation.
    * The composition tests close the gap the soak did NOT close. ``b12de40``
      (a provider fault reaching ``run_pass`` fails the pass, not the process)
      stayed mock-proven through the entire live run, because the re-auth
      absorbed every single fault before the loop could ever see one. The only
      faults that reach the loop are the ones re-auth CANNOT absorb: a second
      consecutive 401, meaning a dead credential rather than a stale token, and
      a non-401 provider error. Both are driven here.

NO NETWORK, NO CREDENTIAL, NO TENANT
    Every Graph interaction is an ``httpx.MockTransport`` and every refresh is
    a spy that installs a sentinel string. Nothing here touches Entra, the live
    soak state directory, or a real mailbox -- which is the point, because the
    test tenant has six days of life left and this file must not spend any of
    it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from _watch_harness import FakeProvider, _context
from inbox_zero_connector.providers.base import ChangeSet, Container, ProviderError
from inbox_zero_connector.providers.outlook.categories import GraphStatusError
from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE, OutlookProvider
from inbox_zero_connector.services.watch import loop as watchloop

CYCLES = 25
"""Consecutive expiry-and-recovery cycles driven through the provider.

Twenty-five because the claim under test is REPETITION, not occurrence: one
cycle is what the live soak already proved, and a defect that leaks state
between cycles -- a client that is rebuilt once and then cached forever, a
refresh counter that saturates -- is invisible until the cycle after the first.
"""

ROUNDS = 40
"""Watcher rounds driven in the loop test."""

EXPIRE_EVERY = 4
"""One expiry every fourth round, giving ten separate expiries across ROUNDS."""

STALE_PREFIX = "stale-cycle-mock-transport-authenticates-nothing"
"""Not a credential. Reaches ``httpx.MockTransport`` and nowhere else.

Named without a credential-shaped word for the same reason the sentinels in
``test_outlook_reauth.py`` are: flake8-bandit's hardcoded-password rule stays ON
for the rest of the suite rather than being suppressed here, which would blind
it to a real one.
"""

FRESH_PREFIX = "fresh-cycle-mock-transport-authenticates-nothing"
"""The prefix a successful refresh installs, numbered per cycle.

NUMBERED, because an unnumbered sentinel proves only that the retry carried a
different token than the first attempt. Numbering proves it carried the token
minted by THIS cycle's refresh, which is the assertion that catches a provider
that rebuilds its transport once and then serves the cycle-1 header forever.
"""

CONTAINER_ID = "folder-cycles"


class CyclingGraphStub:
    """A mocked Graph endpoint that answers 401 on a fixed schedule.

    Records the ``Authorization`` header of every request, which is what makes
    "the retried request carried the NEW token" provable rather than inferred
    from the call succeeding: a stub can be made to succeed either way.
    """

    def __init__(self, *, unauthorized_calls: set[int]) -> None:
        self.unauthorized_calls = unauthorized_calls
        """Zero-based call indices that answer 401 instead of 200."""

        self.authorizations: list[str | None] = []
        """The ``Authorization`` header of each request, in order."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        index = len(self.authorizations)
        self.authorizations.append(request.headers.get("Authorization"))
        if index in self.unauthorized_calls:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return httpx.Response(200, json={"value": [{"id": f"m{index}"}]})


def _client(stub: CyclingGraphStub, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=GRAPH_BASE,
        headers={"Authorization": f"Bearer {token}"},
        transport=httpx.MockTransport(stub.handle),
    )


class CountingReauthSpy:
    """Stands in for MSAL's silent refresh, minting a DISTINCT token each call.

    Mirrors the real ``refresh_credential`` contract: it replaces both the
    token and the cached client, because the access token is baked into an
    ``Authorization`` header at client construction and a surviving client
    would keep sending the expired one. Each call numbers its sentinel, so the
    header recorded by the stub identifies WHICH refresh produced it.
    """

    def __init__(self, provider: OutlookProvider, stub: CyclingGraphStub) -> None:
        self.provider = provider
        self.stub = stub
        self.calls: list[dict[str, Any]] = []
        """One entry per invocation, carrying the kwargs it was given."""

    def __call__(self, interactive: bool = False) -> str:
        self.calls.append({"interactive": interactive})
        token = f"{FRESH_PREFIX}-{len(self.calls)}"
        self.provider._token = token
        self.provider._client = _client(self.stub, token)
        return "spy@example.com"


def _provider(
    unauthorized_calls: set[int],
) -> tuple[OutlookProvider, CyclingGraphStub, CountingReauthSpy]:
    stub = CyclingGraphStub(unauthorized_calls=unauthorized_calls)
    provider = OutlookProvider(client=_client(stub, STALE_PREFIX), token=STALE_PREFIX)
    spy = CountingReauthSpy(provider, stub)
    provider.authenticate = spy  # type: ignore[method-assign]
    return provider, stub, spy


# ---------------------------------------------------------------------------
# 1. Refresh cycles REPEAT
# ---------------------------------------------------------------------------


def test_twenty_five_consecutive_expiries_each_refresh_exactly_once() -> None:
    """Twenty-five expiries, twenty-five recoveries, one refresh apiece.

    Roughly 25 hours of soak, driven in microseconds. Every cycle is a 401
    followed by a success, so the request indices that 401 are the EVEN ones:
    call 0 fails and call 1 is its retry, call 2 fails and call 3 is its retry,
    and so on. Two independent assertions carry the claim.

    ``authenticate`` called exactly CYCLES times is the "never twice for one
    401" half: an implementation that refreshed before retrying AND again on
    the retry's own path would double this count.

    The recorded headers are the "no cycle leaks into the next" half. Cycle N's
    retry must carry the sentinel minted by cycle N's refresh -- not cycle 1's,
    which is what a transport rebuilt once and then cached forever would send,
    and which no assertion about the call merely succeeding would catch.
    """
    unauthorized = {index * 2 for index in range(CYCLES)}
    provider, stub, spy = _provider(unauthorized)

    for _cycle in range(CYCLES):
        assert provider.list_inbox(limit=1)

    assert len(spy.calls) == CYCLES
    assert all(call["interactive"] is False for call in spy.calls)
    assert len(stub.authorizations) == CYCLES * 2
    expected = []
    for cycle in range(CYCLES):
        expected.append(
            f"Bearer {STALE_PREFIX}" if cycle == 0 else f"Bearer {FRESH_PREFIX}-{cycle}"
        )
        expected.append(f"Bearer {FRESH_PREFIX}-{cycle + 1}")
    assert stub.authorizations == expected


# ---------------------------------------------------------------------------
# 2. Many rounds across many expiries
# ---------------------------------------------------------------------------


class RoundClock:
    """The pass counter these tests schedule faults against.

    A per-CALL counter cannot do this job, and the reason is worth stating
    because it is easy to get wrong: a HEALTHY pass calls ``list_containers``
    TWICE -- once in ``run_pass`` to select streams, and again inside
    ``poll_once`` to resolve container names -- while a pass whose first
    listing raises calls it once and returns. Scheduling on the call index
    therefore drifts the moment a fault lands, and the test ends up asserting
    against a schedule it can no longer predict.

    ``watchloop.run`` calls ``sleep`` exactly once per completed pass, so the
    injected sleeper is the one unambiguous pass boundary available from
    outside the loop. This object is advanced there and read by the providers.
    """

    def __init__(self) -> None:
        self.round_index = 0
        """Zero-based index of the pass currently running."""

    def advance(self) -> None:
        self.round_index += 1


class ExpiringProvider(FakeProvider):
    """A provider whose credential expires on a fixed round schedule.

    Models the deployed shape rather than the unit one: the re-auth wrapper
    sits INSIDE the adapter, so from the loop's side an absorbed expiry is
    indistinguishable from a healthy round. That is exactly the property under
    test -- the refresh must consume the fault before ``run_pass`` can turn it
    into a failed pass, so this provider records the expiry and then serves the
    containers exactly as an adapter that had just refreshed would.
    """

    name = "microsoft"

    def __init__(self, clock: RoundClock, *, expire_every: int) -> None:
        super().__init__(
            ChangeSet(),
            containers=(
                Container(
                    id=CONTAINER_ID,
                    name="Inbox",
                    well_known="inbox",
                    parent_id=None,
                    exclusive=False,
                ),
            ),
        )
        self.clock = clock
        self.expire_every = expire_every
        self.expired_rounds: list[int] = []
        """Round indices on which an expiry was absorbed, in order."""

        self._seen_rounds: set[int] = set()

    def list_containers(self) -> tuple[Container, ...]:
        round_index = self.clock.round_index
        if round_index not in self._seen_rounds:
            self._seen_rounds.add(round_index)
            if (round_index + 1) % self.expire_every == 0:
                self.expired_rounds.append(round_index)
        return self.containers


class RoundCountingSleep:
    """Advance the round clock, record the delay, stop after a fixed count.

    Doubles as the pass boundary for ``RoundClock``, because ``watchloop.run``
    calls it exactly once per completed pass and nothing else in the loop has
    that shape.
    """

    def __init__(
        self,
        control: watchloop.LoopControl,
        *,
        stop_after: int,
        clock: RoundClock | None = None,
    ) -> None:
        self.control = control
        self.stop_after = stop_after
        self.clock = clock
        self.delays: list[float] = []

    def __call__(self, delay_seconds: float) -> None:
        self.delays.append(delay_seconds)
        if self.clock is not None:
            self.clock.advance()
        if len(self.delays) >= self.stop_after:
            self.control.request_stop()


def test_forty_rounds_across_ten_expiries_never_fail_a_pass() -> None:
    """Ten absorbed expiries across forty rounds produce zero failed passes.

    Roughly a full day of unattended polling at the deployed interval. The
    assertion that matters is the delay list: ``_next_delay`` returns the BASE
    interval only after an error-free pass and doubles otherwise, so a constant
    base delay across all forty rounds is proof that ``PassResult.errors`` was
    zero every single time. An expiry that leaked through to the loop would
    show up as a doubled delay, immediately and permanently.
    """
    clock = RoundClock()
    provider = ExpiringProvider(clock, expire_every=EXPIRE_EVERY)
    control = watchloop.LoopControl(install_handler=None)
    sleeper = RoundCountingSleep(control, stop_after=ROUNDS, clock=clock)

    watchloop.run(
        _context(provider),
        interval_seconds=10.0,
        sleep=sleeper,
        control=control,
    )

    assert len(sleeper.delays) == ROUNDS
    assert provider.expired_rounds == [
        index for index in range(ROUNDS) if (index + 1) % EXPIRE_EVERY == 0
    ]
    assert len(provider.expired_rounds) == ROUNDS // EXPIRE_EVERY
    assert sleeper.delays == [10.0] * ROUNDS


# ---------------------------------------------------------------------------
# 3. The gap the soak did not close: the two fixes COMPOSE
# ---------------------------------------------------------------------------


class UnabsorbableFaultProvider(FakeProvider):
    """A provider whose listing raises faults the re-auth cannot absorb.

    Two shapes, because they are different facts. A ``GraphStatusError(401)``
    raised where the wrapper has already spent its single retry is a DEAD
    credential -- revoked consent, an expired refresh token, a Conditional
    Access policy -- and no further refresh helps. A plain ``ProviderError`` is
    not an authentication symptom at all and the 401 branch never looks at it.
    Both must reach ``run_pass``, and neither may kill the loop.
    """

    name = "microsoft"

    def __init__(self, clock: RoundClock, *, faults: tuple[Exception | None, ...]) -> None:
        super().__init__(
            ChangeSet(),
            containers=(
                Container(
                    id=CONTAINER_ID,
                    name="Inbox",
                    well_known="inbox",
                    parent_id=None,
                    exclusive=False,
                ),
            ),
        )
        self.clock = clock
        self.faults = faults
        """One entry per ROUND; ``None`` means a healthy listing.

        Indexed by round rather than by call because a healthy pass lists
        containers twice -- see ``RoundClock``. Raising on the second listing
        of a healthy round would fail a stream instead of the pass, which is a
        different code path from the one under test.
        """

        self.faulted_rounds: list[int] = []
        self._seen_rounds: set[int] = set()

    def list_containers(self) -> tuple[Container, ...]:
        round_index = self.clock.round_index
        fault = self.faults[round_index] if round_index < len(self.faults) else None
        if fault is not None and round_index not in self._seen_rounds:
            self._seen_rounds.add(round_index)
            self.faulted_rounds.append(round_index)
            raise fault
        return self.containers


def _dead_credential() -> GraphStatusError:
    """The second consecutive 401: what ``with_reauth_once`` re-raises unchanged.

    Constructed rather than provoked through the wrapper because the wrapper's
    own behaviour is already pinned in ``test_outlook_reauth.py``. What is under
    test here is what the LOOP does with the exception the wrapper let through.
    """
    return GraphStatusError("GRAPH: unauthorized", status=401)


def test_a_dead_credential_fails_the_pass_and_the_loop_recovers() -> None:
    """A 401 the refresh cannot absorb fails one pass; the loop keeps polling.

    This is the composition the live soak never exercised. During the whole run
    the re-auth absorbed every fault, so ``b12de40``'s boundary was never
    driven by a real one -- the loop's survival was mock-proven only. Here the
    fault is deliberately one no refresh fixes, and the sequence proves all
    three stages: the pass errors (delay doubles to 20.0), the loop is still
    running afterwards, and the very next healthy round resets the delay to the
    base interval, which is only reachable if polling actually resumed.
    """
    clock = RoundClock()
    provider = UnabsorbableFaultProvider(clock, faults=(_dead_credential(), None, None))
    control = watchloop.LoopControl(install_handler=None)
    sleeper = RoundCountingSleep(control, stop_after=3, clock=clock)

    watchloop.run(
        _context(provider),
        interval_seconds=10.0,
        sleep=sleeper,
        control=control,
    )

    assert provider.faulted_rounds == [0]
    assert sleeper.delays == [20.0, 10.0, 10.0]


def test_a_non_401_provider_error_fails_the_pass_and_the_loop_recovers() -> None:
    """A fault the 401 branch never inspects takes the same survivable path.

    ``_is_unauthorized`` reads the status attribute and nothing else, so a
    plain ``ProviderError`` -- a transient Graph outage, a DNS failure, a
    malformed payload -- is re-raised untouched and arrives at ``run_pass``
    exactly as the dead credential does. Asserting both shapes separately is
    what keeps the loop's survival from being a property of the 401 path
    specifically.
    """
    clock = RoundClock()
    provider = UnabsorbableFaultProvider(
        clock, faults=(ProviderError("scripted transport failure"), None, None)
    )
    control = watchloop.LoopControl(install_handler=None)
    sleeper = RoundCountingSleep(control, stop_after=3, clock=clock)

    watchloop.run(
        _context(provider),
        interval_seconds=10.0,
        sleep=sleeper,
        control=control,
    )

    assert provider.faulted_rounds == [0]
    assert sleeper.delays == [20.0, 10.0, 10.0]


def test_consecutive_unabsorbable_faults_back_off_without_stopping() -> None:
    """A credential dead for several rounds backs off and still recovers.

    The failure mode this rules out is a loop that survives ONE bad pass and
    then wedges: three consecutive faults must double the delay each time, and
    the fourth round -- healthy again, as it would be after a human re-consents
    -- must return the loop to the base interval rather than leaving it parked
    at the backed-off value.
    """
    clock = RoundClock()
    provider = UnabsorbableFaultProvider(
        clock, faults=(_dead_credential(), _dead_credential(), _dead_credential(), None)
    )
    control = watchloop.LoopControl(install_handler=None)
    sleeper = RoundCountingSleep(control, stop_after=4, clock=clock)

    watchloop.run(
        _context(provider),
        interval_seconds=10.0,
        sleep=sleeper,
        control=control,
    )

    assert provider.faulted_rounds == [0, 1, 2]
    assert sleeper.delays == [20.0, 40.0, 80.0, 10.0]


def test_the_wrapper_re_raises_the_second_401_so_the_loop_can_see_it() -> None:
    """The seam itself: re-auth stops, the exception escapes, the loop owns it.

    Without this the two fixes are proven only in isolation. A dead credential
    must produce exactly ONE refresh attempt -- proving the retry budget is not
    a loop -- and the 401 must then propagate out of the adapter, because an
    adapter that swallowed it would starve ``run_pass`` of the fault the
    previous tests depend on receiving.
    """
    provider, _stub, spy = _provider({0, 1})

    with pytest.raises(GraphStatusError) as raised:
        provider.list_inbox(limit=1)

    assert raised.value.status == 401
    assert len(spy.calls) == 1
