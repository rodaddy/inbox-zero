"""Fan-out polling passes driven through the watcher module's existing fakes."""

from __future__ import annotations

import logging
import signal

import pytest
from loguru import logger

from _watch_harness import FOLDER, INBOX_ID, CountingClassifier, FakeProvider, _context, _message
from inbox_zero_connector.providers.base import ChangeSet, Container, Message, ProviderError
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.triage.decide import REVIEW_FOLDER
from inbox_zero_connector.services.watch import loop as watchloop
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import WatchState

FIRST_ID = "folder-first"
SECOND_ID = "folder-second"
THIRD_ID = "folder-third"
EXCLUDED_ID = "folder-excluded"
SECOND_CURSOR = "cursor-second"
ACTION_CONTAINER_ID = "Label_Business_Action"
REVIEW_CONTAINER_ID = "Label_Review"
DESTINATION_CONTAINER_IDS = {
    FOLDER: ACTION_CONTAINER_ID,
    REVIEW_FOLDER: REVIEW_CONTAINER_ID,
}


class FanOutProvider(FakeProvider):
    """The existing watcher fake shaped as a per-container provider."""

    name = "microsoft"

    def __init__(
        self,
        changes: ChangeSet,
        *,
        containers: tuple[Container, ...],
        failing_cursor: str | None = None,
    ) -> None:
        super().__init__(changes, containers=containers)
        self.failing_cursor = failing_cursor

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        if cursor == self.failing_cursor:
            self.polled.append(cursor)
            raise ProviderError("scripted stream failure")
        return super().poll_changes(cursor, page_token=page_token, container_id=container_id)


class ScriptedProvider(FakeProvider):
    """A mailbox-wide stream with one scripted outcome per polling pass."""

    def __init__(self, outcomes: tuple[ChangeSet | ProviderError, ...]) -> None:
        super().__init__(ChangeSet())
        self.outcomes = iter(outcomes)

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        del page_token
        self.polled.append(cursor)
        outcome = next(self.outcomes)
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome


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


class StopDuringPassProvider(FanOutProvider):
    """Request the injected SIGTERM path while the first stream is in flight."""

    def __init__(
        self,
        changes: ChangeSet,
        *,
        containers: tuple[Container, ...],
        control: watchloop.LoopControl,
    ) -> None:
        super().__init__(changes, containers=containers)
        self.control = control

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        if not self.polled:
            self.control.request_stop(signal.SIGTERM, None)
        return super().poll_changes(cursor, page_token=page_token, container_id=container_id)


class MailboxStateProvider(FakeProvider):
    """A Gmail fake exposing each message's current container membership."""

    def __init__(self, messages: tuple[Message, ...]) -> None:
        containers = (
            Container(
                id=INBOX_ID,
                name="Inbox",
                well_known="inbox",
                parent_id=None,
                exclusive=False,
            ),
            Container(
                id=ACTION_CONTAINER_ID,
                name=FOLDER,
                well_known=None,
                parent_id=None,
                exclusive=False,
            ),
            Container(
                id=REVIEW_CONTAINER_ID,
                name=REVIEW_FOLDER,
                well_known=None,
                parent_id=None,
                exclusive=False,
            ),
        )
        super().__init__(
            ChangeSet(messages=messages, cursor="cursor-after-filing"),
            containers=containers,
        )
        self.mailbox = {message.id: message for message in messages}

    def file_message(self, message_id: str, folder: str) -> str:
        """Record a successful filing and expose the resulting placement."""
        result = super().file_message(message_id, folder)
        message = self.mailbox[message_id]
        remaining = tuple(
            container_id for container_id in message.containers if container_id != INBOX_ID
        )
        destination_id = DESTINATION_CONTAINER_IDS[folder]
        self.mailbox[message_id] = message.model_copy(
            update={"containers": (*remaining, destination_id)}
        )
        return result


def _container(container_id: str, name: str) -> Container:
    return Container(
        id=container_id,
        name=name,
        well_known=None,
        parent_id=None,
        exclusive=True,
    )


def test_one_pass_polls_exactly_the_in_scope_streams() -> None:
    """Set equality catches both a missing allowed stream and an extra excluded one."""
    containers = (
        _container(FIRST_ID, "First"),
        _container(EXCLUDED_ID, "Client Matters"),
        _container(SECOND_ID, "Second"),
        _container(THIRD_ID, "Third"),
    )
    provider = FanOutProvider(ChangeSet(), containers=containers)
    scope = MailboxScope(mode="all_except", excluded_folders=("Client Matters",))

    result = watchloop.run_pass(_context(provider, scope=scope, classifier=CountingClassifier()))

    expected_streams = {FIRST_ID, SECOND_ID, THIRD_ID}
    assert {stream.container_id for stream in result.streams} == expected_streams


def test_a_provider_error_on_one_stream_does_not_abandon_the_rest() -> None:
    containers = tuple(
        _container(container_id, name)
        for container_id, name in (
            (FIRST_ID, "First"),
            (SECOND_ID, "Second"),
            (THIRD_ID, "Third"),
        )
    )
    state = WatchState()
    state.record_cursor(FIRST_ID, "cursor-first")
    state.record_cursor(SECOND_ID, SECOND_CURSOR)
    state.record_cursor(THIRD_ID, "cursor-third")
    provider = FanOutProvider(
        ChangeSet(),
        containers=containers,
        failing_cursor=SECOND_CURSOR,
    )

    result = watchloop.run_pass(_context(provider, state=state, classifier=CountingClassifier()))

    assert [stream.container_id for stream in result.streams] == [
        FIRST_ID,
        SECOND_ID,
        THIRD_ID,
    ]
    assert result.streams[0].summary is not None
    assert result.streams[1].outcome == ProviderError.__name__
    assert result.streams[1].summary is None
    assert result.streams[2].summary is not None
    assert result.errors == 1


def test_a_resynced_stream_is_a_normal_outcome_not_an_error() -> None:
    container = _container(FIRST_ID, "First")
    state = WatchState()
    state.record_cursor(FIRST_ID, "stale-cursor")
    provider = FanOutProvider(
        ChangeSet(resync_required=True),
        containers=(container,),
    )

    result = watchloop.run_pass(_context(provider, state=state, classifier=CountingClassifier()))

    assert result.errors == 0
    assert result.resyncs == 1
    assert result.streams[0].outcome == watcher.RoundState.RESYNCED.name
    assert result.streams[0].summary is not None
    assert result.streams[0].summary.state is watcher.RoundState.RESYNCED


def test_once_mode_polls_every_stream_once_then_returns_without_sleeping() -> None:
    """Unique cursors prove each selected stream ran exactly once in the pass."""
    containers = tuple(
        _container(container_id, name)
        for container_id, name in (
            (FIRST_ID, "First"),
            (SECOND_ID, "Second"),
            (THIRD_ID, "Third"),
        )
    )
    state = WatchState()
    state.record_cursor(FIRST_ID, "cursor-first")
    state.record_cursor(SECOND_ID, SECOND_CURSOR)
    state.record_cursor(THIRD_ID, "cursor-third")
    provider = FanOutProvider(ChangeSet(), containers=containers)
    delays: list[float] = []

    watchloop.run(
        _context(provider, state=state, classifier=CountingClassifier()),
        once=True,
        sleep=delays.append,
        control=watchloop.LoopControl(install_handler=None),
    )

    assert provider.polled == ["cursor-first", SECOND_CURSOR, "cursor-third"]
    assert delays == []


def test_stop_during_an_in_flight_pass_finishes_and_records_every_stream() -> None:
    """Calling the SIGTERM handler path cannot abandon the active pass midway.

    REWRITTEN (#162): the checkpoint assertion used to read ``== [3]``, encoding
    one save per PASS. That is the defect -- a kill anywhere in a three-folder
    fan-out discarded all three folders' cursors and replay marks, and the
    restart re-billed every classification. The contract is now one save per
    STREAM, so the receipt is the growing row count ``[1, 2, 3]``.
    """
    containers = tuple(
        _container(container_id, name)
        for container_id, name in (
            (FIRST_ID, "First"),
            (SECOND_ID, "Second"),
            (THIRD_ID, "Third"),
        )
    )
    state = WatchState()
    state.record_cursor(FIRST_ID, "cursor-first")
    state.record_cursor(SECOND_ID, SECOND_CURSOR)
    state.record_cursor(THIRD_ID, "cursor-third")
    control = watchloop.LoopControl(install_handler=None)
    provider = StopDuringPassProvider(
        ChangeSet(
            messages=(_message("in-flight", containers=(FIRST_ID,)),),
            cursor="cursor-after-stop",
        ),
        containers=containers,
        control=control,
    )
    classifier = CountingClassifier()
    checkpoint_row_counts: list[int] = []
    delays: list[float] = []

    watchloop.run(
        _context(provider, state=state, classifier=classifier),
        sleep=delays.append,
        control=control,
        checkpoint=lambda: checkpoint_row_counts.append(len(ledger.read_all())),
    )

    assert control.stop_requested is True
    assert provider.polled == ["cursor-first", SECOND_CURSOR, "cursor-third"]
    assert provider.filed == [("in-flight", FOLDER)] * 3
    assert classifier.seen_ids == ["in-flight"] * 3
    assert [row.message_id for row in ledger.read_all()] == ["in-flight"] * 3
    assert checkpoint_row_counts == [1, 2, 3]
    stored_tokens: list[str] = []
    for container_id in (FIRST_ID, SECOND_ID, THIRD_ID):
        cursor = state.cursor_for(container_id)
        assert cursor is not None
        stored_tokens.append(cursor.token)
    assert stored_tokens == ["cursor-after-stop"] * 3
    assert delays == []


def test_one_loop_pass_files_messages_out_of_inbox_and_receipts_each_destination() -> None:
    """The loop clears classified and ambiguous mail into their decided folders."""
    classified = _message("classified").model_copy(update={"is_important": True})
    ambiguous = _message("ambiguous")
    provider = MailboxStateProvider((classified, ambiguous))
    classifier = CountingClassifier(confidence=0.10)

    watchloop.run(
        _context(provider, classifier=classifier),
        once=True,
        control=watchloop.LoopControl(install_handler=None),
    )

    assert provider.filed == [
        ("classified", FOLDER),
        ("ambiguous", REVIEW_FOLDER),
    ]
    assert provider.mailbox["classified"].containers == (ACTION_CONTAINER_ID,)
    assert provider.mailbox["ambiguous"].containers == (REVIEW_CONTAINER_ID,)
    assert all(INBOX_ID not in message.containers for message in provider.mailbox.values())
    assert classifier.seen_ids == ["ambiguous"]
    assert [(row.message_id, row.action, row.detail, row.status) for row in ledger.read_all()] == [
        ("classified", ledger.ACTION_FILE, FOLDER, ledger.Status.APPLIED),
        ("ambiguous", ledger.ACTION_FILE, REVIEW_FOLDER, ledger.Status.APPLIED),
    ]


def test_info_logs_never_contain_message_subjects_or_senders(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The completed-pass INFO record exposes counts and no mailbox content."""
    messages = (
        Message(
            id="log-one",
            thread_id="thread-log-one",
            sender="sender-marker-one@example.invalid",
            subject="SUBJECT-MARKER-ALPHA-92741",
            received="2026-08-19T10:00:00Z",
            snippet="fixture-one",
            containers=(FIRST_ID,),
        ),
        Message(
            id="log-two",
            thread_id="thread-log-two",
            sender="sender-marker-two@example.invalid",
            subject="SUBJECT-MARKER-BRAVO-63852",
            received="2026-08-19T10:01:00Z",
            snippet="fixture-two",
            containers=(FIRST_ID,),
        ),
    )
    provider = FakeProvider(
        ChangeSet(messages=messages, cursor="cursor-after-log-pass"),
        containers=(_container(FIRST_ID, "First"),),
    )
    caplog.set_level(logging.INFO)
    sink_id = logger.add(caplog.handler, level="INFO", format="{message}")

    try:
        watchloop.run(
            _context(provider, classifier=CountingClassifier()),
            once=True,
            control=watchloop.LoopControl(install_handler=None),
        )
    finally:
        logger.remove(sink_id)

    sensitive_values = tuple(
        value for message in messages for value in (message.subject, message.sender)
    )
    rendered_records = tuple(record.getMessage() for record in caplog.records)
    assert (
        "WATCH LOOP: round complete streams_polled=1 errors=0 resyncs=0 "
        "seen=2 skipped_out_of_scope=0 skipped_unenumerated_container=0 "
        "skipped_missing=0 skipped_already_seen=0 classified=2 "
        "applied=2 failed=0 cursors_stored=1"
    ) in rendered_records
    assert all(
        sensitive not in rendered for sensitive in sensitive_values for rendered in rendered_records
    )


def test_stop_requested_during_sleep_prevents_another_pass() -> None:
    """A stop after one wait leaves both the pass count and sleep count at one."""
    provider = ScriptedProvider((ChangeSet(),))
    control = watchloop.LoopControl(install_handler=None)
    sleep = RecordingSleep(control, stop_after=1)

    watchloop.run(
        _context(provider, classifier=CountingClassifier()),
        interval_seconds=45.0,
        sleep=sleep,
        control=control,
    )

    assert provider.polled == [None]
    assert sleep.delays == [45.0]


def test_provider_errors_back_off_to_the_cap_then_success_resets_to_base() -> None:
    """The injected sleeper records the full error, cap, and recovery sequence."""
    provider = ScriptedProvider((
        ProviderError("failure-1"),
        ProviderError("failure-2"),
        ProviderError("failure-3"),
        ProviderError("failure-4"),
        ProviderError("failure-5"),
        ChangeSet(),
    ))
    control = watchloop.LoopControl(install_handler=None)
    sleep = RecordingSleep(control, stop_after=6)

    watchloop.run(
        _context(provider, classifier=CountingClassifier()),
        interval_seconds=100.0,
        sleep=sleep,
        control=control,
    )

    assert sleep.delays == [200.0, 400.0, 800.0, 900.0, 900.0, 100.0]


def test_resynced_pass_waits_the_base_interval_without_backoff() -> None:
    """RESYNCED is an error-free pass, so its next delay remains at the base."""
    state = WatchState()
    state.record_cursor(None, "stale-cursor")
    provider = ScriptedProvider((ChangeSet(resync_required=True),))
    control = watchloop.LoopControl(install_handler=None)
    sleep = RecordingSleep(control, stop_after=1)

    watchloop.run(
        _context(provider, state=state, classifier=CountingClassifier()),
        interval_seconds=75.0,
        sleep=sleep,
        control=control,
    )

    assert sleep.delays == [75.0]
