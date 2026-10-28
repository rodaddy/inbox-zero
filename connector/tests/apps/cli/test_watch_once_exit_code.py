"""``watch --once`` must exit non-zero when the round recorded errors.

The defect this pins: a one-shot poll that raised ``AuthError`` before touching
any stream logged ``streams_polled=1 errors=1 seen=0 classified=0`` and returned
0, so a cron wrapper or installer smoke test read a dead credential as success.
The continuous loop keeps its own semantics -- it survives errors by design --
so only the ``--once`` exit status changes.
"""

from __future__ import annotations

from typing import Any

import pytest

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.apps.cli import watch_accounts
from inbox_zero_connector.providers.base import MailProvider
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.watch import loop as watchloop
from inbox_zero_connector.services.watch.round import RoundContext, RoundState, RoundSummary


class StubProvider(MailProvider):
    """Held by the context, never called: the loop that would use it is patched out."""

    name = "google"

    def _unreachable(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("_cmd_watch must not call the provider")

    authenticate = _unreachable
    create_draft = _unreachable
    create_self_draft = _unreachable
    file_message = _unreachable
    get_thread = _unreachable
    list_containers = _unreachable
    list_inbox = _unreachable
    poll_changes = _unreachable
    read_sent_sample = _unreachable
    set_important = _unreachable
    tag_message = _unreachable


class SentinelClassifier:
    """A structural ``Classifier``. A bare ``object()`` fails RoundContext validation."""

    def classify(self, message: Any, folders: tuple[str, ...]) -> Classification:
        return Classification(folder="", confidence=0.0, reason="sentinel", deterministic=False)


def _stub_pass(*, errors: int) -> watchloop.PassResult:
    """A counts-only pass carrying ``errors`` failed streams."""
    return watchloop.PassResult(
        streams=tuple(watchloop.StreamResult(None, "AuthError", None) for _ in range(errors))
    )


def _summary(*, applied: int, failed: int) -> RoundSummary:
    """A completed round's counts. The stream itself did not raise."""
    # A failed filing withholds the cursor (``_settle_cursor``), which is
    # exactly the ``PAGED`` state -- the stream stays where it was.
    return RoundSummary(
        container_id=None,
        state=RoundState.COMMITTED if failed == 0 else RoundState.PAGED,
        seen=applied + failed,
        skipped_out_of_scope=0,
        skipped_unenumerated_container=0,
        skipped_already_seen=0,
        classified=applied + failed,
        applied=applied,
        failed=failed,
        cursor_stored=failed == 0,
        resynced=False,
    )


def _stub_filed_pass(*, applied: int, failed: int) -> watchloop.PassResult:
    """A pass whose single stream COMPLETED but could not file every message."""
    return watchloop.PassResult(
        streams=(
            watchloop.StreamResult(
                None,
                RoundState.COMMITTED.name,
                _summary(applied=applied, failed=failed),
            ),
        )
    )


@pytest.fixture
def _wired_watch(monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None) -> None:
    monkeypatch.setattr(
        watch_accounts.factory, "from_settings", lambda _active: SentinelClassifier()
    )
    monkeypatch.setattr(watch_accounts, "get_provider", lambda *args, **kwargs: StubProvider())


def test_watch_once_exits_non_zero_after_an_errored_round(
    monkeypatch: pytest.MonkeyPatch, _wired_watch: None
) -> None:
    def _errored(context: RoundContext, **kwargs: Any) -> watchloop.PassResult:
        return _stub_pass(errors=1)

    monkeypatch.setattr("inbox_zero_connector.services.watch.loop.run", _errored)
    assert cli.main(["watch", "--once"]) == 1


def test_watch_once_exits_zero_after_a_clean_round(
    monkeypatch: pytest.MonkeyPatch, _wired_watch: None
) -> None:
    """A pass that polled a stream and raised nothing is the success case.

    The stream is REQUIRED, not decoration: ``_stub_pass(errors=0)`` builds an
    empty ``streams`` tuple, which is now the zero-stream failure (#175), so
    the clean case has to carry a stream that actually completed.
    """

    def _clean(context: RoundContext, **kwargs: Any) -> watchloop.PassResult:
        return _stub_filed_pass(applied=0, failed=0)

    monkeypatch.setattr("inbox_zero_connector.services.watch.loop.run", _clean)
    assert cli.main(["watch", "--once"]) == 0


def test_watch_once_exits_non_zero_when_the_pass_could_not_file_anything(
    monkeypatch: pytest.MonkeyPatch, _wired_watch: None
) -> None:
    """``failed>0 applied=0`` with no stream error is still a mailbox in trouble.

    The stream completed, so ``PassResult.errors`` is 0 -- and every message it
    saw went to a ``Status.FAILED`` receipt instead of a folder. A scheduler
    reading only the stream-level error count never alarms on a mailbox where
    nothing can be filed (#160).
    """

    def _all_filings_failed(context: RoundContext, **kwargs: Any) -> watchloop.PassResult:
        return _stub_filed_pass(applied=0, failed=3)

    monkeypatch.setattr("inbox_zero_connector.services.watch.loop.run", _all_filings_failed)
    assert cli.main(["watch", "--once"]) == 1


def test_watch_once_exits_non_zero_when_no_pass_ran_at_all(
    monkeypatch: pytest.MonkeyPatch, _wired_watch: None
) -> None:
    """``run()`` returning ``None`` means a stop landed before any poll happened.

    Nothing was observed, so nothing can be reported clean. Exiting 0 here tells
    a cron entry the mailbox was checked and found tidy when it was never read.
    """

    def _no_pass(context: RoundContext, **kwargs: Any) -> watchloop.PassResult | None:
        return None

    monkeypatch.setattr("inbox_zero_connector.services.watch.loop.run", _no_pass)
    assert cli.main(["watch", "--once"]) == 1


def test_watch_once_exits_zero_when_a_pass_filed_everything_it_saw(
    monkeypatch: pytest.MonkeyPatch, _wired_watch: None
) -> None:
    """A completed pass with no failed filing and no stream error is success."""

    def _clean(context: RoundContext, **kwargs: Any) -> watchloop.PassResult:
        return _stub_filed_pass(applied=4, failed=0)

    monkeypatch.setattr("inbox_zero_connector.services.watch.loop.run", _clean)
    assert cli.main(["watch", "--once"]) == 0


def test_the_continuous_watch_still_exits_zero_after_errors(
    monkeypatch: pytest.MonkeyPatch, _wired_watch: None
) -> None:
    """Without ``--once`` the loop owns error handling; a clean return is success."""

    def _errored(context: RoundContext, **kwargs: Any) -> watchloop.PassResult:
        return _stub_pass(errors=1)

    monkeypatch.setattr("inbox_zero_connector.services.watch.loop.run", _errored)
    assert cli.main(["watch"]) == 0
