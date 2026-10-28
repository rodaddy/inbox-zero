"""A provider fault while listing containers fails the pass without killing the loop.

The Sprint 2 live soak died when Graph returned HTTP 401 from ``list_containers``
outside any error boundary. These tests pin the boundary: the pass reports an
error, the loop keeps running, and the backoff in ``_next_delay`` engages.
"""

from __future__ import annotations

from _watch_harness import FakeProvider, _context
from inbox_zero_connector.providers.base import ChangeSet, Container, ProviderError
from inbox_zero_connector.services.watch import loop as watchloop

CONTAINER_ID = "folder-listed"


class ListFailingProvider(FakeProvider):
    """A provider whose container listing fails a scripted number of times."""

    name = "microsoft"

    def __init__(self, *, failures: int) -> None:
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
        self.remaining_failures = failures
        self.list_calls = 0

    def list_containers(self) -> tuple[Container, ...]:
        self.list_calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise ProviderError("scripted listing failure")
        return self.containers


class RecordingSleep:
    """Record injected delays and request loop shutdown after a fixed count."""

    def __init__(self, control: watchloop.LoopControl, *, stop_after: int) -> None:
        self.control = control
        self.stop_after = stop_after
        self.delays: list[float] = []

    def __call__(self, delay_seconds: float) -> None:
        self.delays.append(delay_seconds)
        if len(self.delays) >= self.stop_after:
            self.control.request_stop()


def test_a_failed_container_listing_returns_a_failed_pass() -> None:
    """``run_pass`` reports one errored stream instead of raising."""
    provider = ListFailingProvider(failures=1)
    result = watchloop.run_pass(_context(provider))
    assert result.errors >= 1
    assert result.streams_polled == 1
    assert result.streams[0].summary is None
    assert result.streams[0].container_id is None
    assert result.streams[0].outcome == "ProviderError"


def test_the_loop_survives_failed_passes_and_backs_off() -> None:
    """Three consecutive failed passes complete, with the delay doubling."""
    provider = ListFailingProvider(failures=3)
    control = watchloop.LoopControl(install_handler=None)
    sleeper = RecordingSleep(control, stop_after=3)
    watchloop.run(
        _context(provider),
        interval_seconds=10.0,
        sleep=sleeper,
        control=control,
    )
    assert provider.list_calls == 3
    assert sleeper.delays == [20.0, 40.0, 80.0]


def test_a_healthy_pass_after_a_failure_resets_the_delay() -> None:
    """The first success returns the delay to the base interval."""
    provider = ListFailingProvider(failures=1)
    control = watchloop.LoopControl(install_handler=None)
    sleeper = RecordingSleep(control, stop_after=2)
    watchloop.run(
        _context(provider),
        interval_seconds=10.0,
        sleep=sleeper,
        control=control,
    )
    assert sleeper.delays == [20.0, 10.0]
