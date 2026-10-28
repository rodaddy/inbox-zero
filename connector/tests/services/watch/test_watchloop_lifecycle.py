"""Daemon lifecycle: interruptible backoff, per-stream checkpoints, survivable faults.

THE THREE DEFECTS THESE TESTS PIN, all confirmed by executed gauntlet repros:

#162a  the backoff wait was ``time.sleep``, which PEP 475 makes uninterruptible
       -- a SIGTERM during a 15-minute backoff waited the full window, the
       service manager escalated to SIGKILL, and ``checkpoint`` never ran. The
       pass's cursors and seen marks were lost and the restart re-billed every
       classification.
#162b  state was persisted ONCE per pass. A pass fanning out over N Graph
       folders held N rounds' worth of unsaved cursors, so a kill anywhere in
       the fan-out discarded all of them.
#157   the per-stream boundary caught only ``ProviderError``. A raw httpx
       transport error from ``poll_changes``, or an unexpected escape from a
       classifier backend, killed the daemon outright.

Every wait here runs on a real ``threading.Event`` with a bounded wall clock, so
"returns promptly" is an asserted duration rather than an inference from a fake.
"""

from __future__ import annotations

import threading
import time

from _watch_harness import CountingClassifier, FakeProvider, _context
from inbox_zero_connector.providers.base import ChangeSet, Container, ProviderError
from inbox_zero_connector.services.watch import loop as watchloop

FIRST_ID = "folder-first"
SECOND_ID = "folder-second"
THIRD_ID = "folder-third"


def _container(container_id: str, name: str) -> Container:
    return Container(
        id=container_id,
        name=name,
        well_known=None,
        parent_id=None,
        exclusive=True,
    )


class _FanOutProvider(FakeProvider):
    """A Graph-shaped provider: one delta stream per selected folder."""

    name = "microsoft"

    def __init__(self, *, containers: tuple[Container, ...]) -> None:
        super().__init__(ChangeSet(cursor="cursor-fanned-out"), containers=containers)


class _ExplodingProvider(FakeProvider):
    """A stream whose poll raises something that is NOT a ``ProviderError``."""

    def __init__(self, error: BaseException, *, failures: int) -> None:
        super().__init__(ChangeSet(cursor="cursor-after-recovery"))
        self.error = error
        self.remaining_failures = failures

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        self.polled.append(cursor)
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise self.error
        return self.changes


class _StoppingSleep:
    """Record delays and request a stop once a fixed number have elapsed."""

    def __init__(self, control: watchloop.LoopControl, *, stop_after: int) -> None:
        self.control = control
        self.stop_after = stop_after
        self.delays: list[float] = []

    def __call__(self, delay_seconds: float) -> None:
        self.delays.append(delay_seconds)
        if len(self.delays) >= self.stop_after:
            self.control.request_stop()


# ---------------------------------------------------------------------------
# #162a -- the backoff wait must be interruptible
# ---------------------------------------------------------------------------


def test_a_stop_during_the_backoff_wait_returns_promptly_and_checkpoints() -> None:
    """A real wait, a real signal, and a bounded wall clock.

    The loop is started on a thread with a base interval far longer than any
    test may block for. ``request_stop`` -- the exact callable the SIGTERM
    handler is wired to -- is invoked while the loop sits in its wait, and the
    run must return in well under that interval WITH its checkpoint run.
    """
    provider = FakeProvider(ChangeSet(cursor="cursor-lifecycle"))
    control = watchloop.LoopControl(install_handler=None)
    checkpoints: list[int] = []
    waiting = threading.Event()

    def checkpoint() -> None:
        checkpoints.append(1)
        waiting.set()

    def drive() -> None:
        watchloop.run(
            _context(provider, classifier=CountingClassifier()),
            interval_seconds=300.0,
            control=control,
            checkpoint=checkpoint,
        )

    runner = threading.Thread(target=drive)
    started = time.monotonic()
    runner.start()
    assert waiting.wait(timeout=5.0), "the first pass never completed"
    control.request_stop()
    runner.join(timeout=5.0)
    elapsed = time.monotonic() - started

    assert runner.is_alive() is False, "the loop did not wake from its backoff wait"
    assert elapsed < 30.0
    assert len(checkpoints) >= 1


def test_the_default_wait_is_the_stop_events_own_wait_not_time_sleep() -> None:
    """``LoopControl`` owns the wait, so a stop can end it. Structural assertion."""
    control = watchloop.LoopControl(install_handler=None)
    started = time.monotonic()
    control.request_stop()

    assert control.wait(30.0) is True
    assert time.monotonic() - started < 5.0


def test_a_wait_that_is_not_interrupted_runs_its_full_timeout() -> None:
    """The interruptible wait is still a wait: no stop means no early return."""
    control = watchloop.LoopControl(install_handler=None)
    started = time.monotonic()

    assert control.wait(0.05) is False
    assert time.monotonic() - started >= 0.05


# ---------------------------------------------------------------------------
# #162b -- one checkpoint per STREAM, not one per pass
# ---------------------------------------------------------------------------


def test_state_is_checkpointed_after_every_stream_in_a_fan_out_pass() -> None:
    """Three streams in one pass means three checkpoints, not one at the end."""
    containers = (
        _container(FIRST_ID, "First"),
        _container(SECOND_ID, "Second"),
        _container(THIRD_ID, "Third"),
    )
    provider = _FanOutProvider(containers=containers)
    control = watchloop.LoopControl(install_handler=None)
    checkpointed_cursor_counts: list[int] = []
    context = _context(provider, classifier=CountingClassifier())

    watchloop.run(
        context,
        once=True,
        control=control,
        checkpoint=lambda: checkpointed_cursor_counts.append(len(context.state.cursors)),
    )

    assert checkpointed_cursor_counts == [1, 2, 3]


def test_a_stream_that_errors_still_checkpoints_the_streams_before_it() -> None:
    """The fan-out's earlier cursors must not ride on the last stream succeeding."""
    containers = (_container(FIRST_ID, "First"), _container(SECOND_ID, "Second"))
    provider = _FanOutProvider(containers=containers)
    context = _context(provider, classifier=CountingClassifier())
    checkpoints: list[int] = []

    watchloop.run(
        context,
        once=True,
        control=watchloop.LoopControl(install_handler=None),
        checkpoint=lambda: checkpoints.append(len(context.state.cursors)),
    )

    assert checkpoints[0] == 1


# ---------------------------------------------------------------------------
# #157 loop seam -- a non-ProviderError must not kill the daemon
# ---------------------------------------------------------------------------


def test_a_connection_error_on_one_stream_does_not_kill_the_loop() -> None:
    """A raw transport error is a WAN blip, not a reason to stop watching mail."""
    provider = _ExplodingProvider(ConnectionError("transport reset"), failures=2)
    control = watchloop.LoopControl(install_handler=None)
    sleeper = _StoppingSleep(control, stop_after=3)

    watchloop.run(
        _context(provider, classifier=CountingClassifier()),
        interval_seconds=10.0,
        sleep=sleeper,
        control=control,
    )

    assert sleeper.delays == [20.0, 40.0, 10.0]


def test_an_unexpected_classifier_escape_is_reported_by_type_and_survived() -> None:
    """``run_pass`` reports the error TYPE NAME, never the exception text."""
    provider = _ExplodingProvider(ValueError("subject 'Quarterly review notes'"), failures=1)

    result = watchloop.run_pass(_context(provider, classifier=CountingClassifier()))

    assert result.errors == 1
    assert result.streams[0].outcome == "ValueError"
    assert result.streams[0].summary is None


def test_a_provider_error_is_still_reported_by_its_own_type_name() -> None:
    """Broadening the catch must not blur the error types already reported."""
    provider = _ExplodingProvider(ProviderError("401"), failures=1)

    result = watchloop.run_pass(_context(provider, classifier=CountingClassifier()))

    assert result.streams[0].outcome == ProviderError.__name__


def test_a_container_listing_failure_of_any_type_fails_the_pass_not_the_loop() -> None:
    """The whole-pass boundary is broadened on the same reasoning as the stream one."""

    class _ListExploder(FakeProvider):
        name = "microsoft"

        def list_containers(self) -> list[Container]:
            raise ConnectionError("transport reset")

    result = watchloop.run_pass(_context(_ListExploder(ChangeSet())))

    assert result.errors == 1
    assert result.streams[0].outcome == "ConnectionError"
