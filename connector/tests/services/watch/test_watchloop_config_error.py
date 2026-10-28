"""``ConfigError`` is FATAL to the watch loop, never a transient stream fault (#171).

The broad ``except Exception`` boundaries added in f79d864 exist so a WAN blip or
an unexpected classifier escape cannot kill an unattended daemon. A
``ConfigError`` is the opposite kind of fault: the installation is wrong, and no
amount of backoff makes it right. Swallowing it turned an uninitialized install
into a hot loop wearing a health mask -- two tracebacks per tier in the 0.2.9
smoke -- and hid the one-line ``Configuration error: ...`` refusal that
``apps/cli/main.py`` exists to print.

These tests pin both directions: ``ConfigError`` PROPAGATES, and the f79d864
survive-anything contract still holds for every other exception.
"""

from __future__ import annotations

import pytest

from _watch_harness import FakeProvider, _context
from inbox_zero_connector.config import ConfigError
from inbox_zero_connector.providers.base import ChangeSet, Container, ProviderError
from inbox_zero_connector.services.watch import loop as watchloop

CONTAINER_ID = "folder-listed"


class _MicrosoftProvider(FakeProvider):
    """A non-Gmail provider, so the loop fans out to per-container streams."""

    name = "microsoft"

    def __init__(self) -> None:
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


class ListRaisingProvider(_MicrosoftProvider):
    """Container listing raises the injected error every call."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.list_calls = 0

    def list_containers(self) -> list[Container]:
        self.list_calls += 1
        raise self.error


class PollRaisingProvider(_MicrosoftProvider):
    """Listing succeeds; the stream's own round raises the injected error."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.poll_calls = 0

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        self.poll_calls += 1
        raise self.error


def test_run_pass_propagates_a_config_error_from_a_stream() -> None:
    """A stream's ``ConfigError`` escapes ``run_pass`` instead of becoming a count."""
    provider = PollRaisingProvider(ConfigError("this install is not initialized"))
    with pytest.raises(ConfigError, match="not initialized"):
        watchloop.run_pass(_context(provider))


def test_run_pass_propagates_a_config_error_from_container_listing() -> None:
    """The listing boundary is the same rule: configuration faults are fatal."""
    provider = ListRaisingProvider(ConfigError("this install is not initialized"))
    with pytest.raises(ConfigError, match="not initialized"):
        watchloop.run_pass(_context(provider))


def test_run_propagates_a_config_error_instead_of_backing_off() -> None:
    """``run`` never retries a configuration fault: no sleep, no second pass."""
    provider = ListRaisingProvider(ConfigError("this install is not initialized"))
    control = watchloop.LoopControl(install_handler=None)
    delays: list[float] = []

    def _sleep(delay_seconds: float) -> None:
        # A configuration fault must never reach a wait at all. Stopping the
        # loop here bounds the RED run to one extra pass instead of spinning
        # forever on the very defect under test.
        delays.append(delay_seconds)
        control.request_stop()

    with pytest.raises(ConfigError):
        watchloop.run(
            _context(provider),
            interval_seconds=10.0,
            sleep=_sleep,
            control=control,
        )
    assert provider.list_calls == 1
    assert delays == []


def test_run_pass_still_survives_a_provider_error_from_a_stream() -> None:
    """The f79d864 contract stays: a transport-class fault is a counted error."""
    provider = PollRaisingProvider(ProviderError("scripted stream failure"))
    result = watchloop.run_pass(_context(provider))
    assert result.errors == 1
    assert result.streams[0].container_id == CONTAINER_ID
    assert result.streams[0].outcome == "ProviderError"


def test_run_still_backs_off_after_a_connection_error() -> None:
    """A raw transport error still fails the pass and drives the backoff."""
    provider = ListRaisingProvider(ConnectionError("wan blip"))
    control = watchloop.LoopControl(install_handler=None)
    delays: list[float] = []

    def _sleep(delay_seconds: float) -> None:
        delays.append(delay_seconds)
        control.request_stop()

    result = watchloop.run(
        _context(provider),
        interval_seconds=10.0,
        sleep=_sleep,
        control=control,
    )
    assert result is not None
    assert result.errors == 1
    assert delays == [20.0]
