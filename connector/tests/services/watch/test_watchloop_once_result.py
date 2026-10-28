"""``run`` must hand the caller the outcome of the last completed pass.

The defect this pins: ``run`` returned ``None``, so every caller -- including
the ``watch --once`` CLI path a cron wrapper checks -- was structurally unable
to tell a clean round from one that recorded ``errors=1``. A one-shot poll
whose provider fell over logged ``streams_polled=1 errors=1`` and still exited
0, which reads to a scheduler as a healthy mailbox.

The transient fault here is a ``ProviderError``. It used to be ``AuthError``,
until #195 made ``AuthError`` an ``OperatorRefusalError`` that the loop
re-raises instead of counting -- a missing credential is not a fault a retry
can fix, and the CLI owes the operator one refusal line for it.
"""

from __future__ import annotations

import pytest

from _watch_harness import FakeProvider, _context
from inbox_zero_connector.providers.base import (
    AuthError,
    ChangeSet,
    Container,
    ProviderError,
)
from inbox_zero_connector.services.watch import loop as watchloop

CONTAINER_ID = "folder-listed"


def _inbox() -> tuple[Container, ...]:
    """The single listed container the failing providers advertise."""
    return (
        Container(
            id=CONTAINER_ID,
            name="Inbox",
            well_known="inbox",
            parent_id=None,
            exclusive=False,
        ),
    )


class TransientFailingProvider(FakeProvider):
    """A provider whose container listing raises a transient provider fault."""

    name = "microsoft"

    def __init__(self) -> None:
        super().__init__(ChangeSet(), containers=_inbox())

    def list_containers(self) -> list[Container]:
        raise ProviderError("transport reset while listing folders")


class AuthFailingProvider(FakeProvider):
    """A provider whose container listing raises the no-stored-credential refusal."""

    name = "microsoft"

    def __init__(self) -> None:
        super().__init__(ChangeSet(), containers=_inbox())

    def list_containers(self) -> list[Container]:
        raise AuthError("no stored credential")


def test_a_one_shot_run_returns_the_errored_pass() -> None:
    """``run(once=True)`` hands back the failed pass rather than swallowing it."""
    result = watchloop.run(_context(TransientFailingProvider()), once=True)
    assert result is not None
    assert result.errors == 1


def test_a_one_shot_run_lets_an_auth_refusal_escape_uncounted() -> None:
    """#195: a missing credential is fatal, not an errored pass to retry."""
    with pytest.raises(AuthError):
        watchloop.run(_context(AuthFailingProvider()), once=True)


def test_a_one_shot_run_returns_a_clean_pass() -> None:
    """A round with no provider fault reports zero errors."""
    result = watchloop.run(_context(FakeProvider(ChangeSet())), once=True)
    assert result is not None
    assert result.errors == 0
