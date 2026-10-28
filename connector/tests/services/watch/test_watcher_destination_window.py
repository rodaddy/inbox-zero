"""The destination-stream refile loop (#173) -- marks land in the right window.

Split from ``test_watcher.py`` on 2026-08-24 when the #173 tests pushed it
past the repo's 500-code-line ceiling (``scripts/check_code_size.py``) -- the
same seam-preserving cut that produced ``test_watcher_drafting.py``. The
concern here is one thing: after a move, the post-move id must be marked seen
in the DESTINATION container's window, because on Graph each folder is its
own delta stream and the moved message re-enters through the folder it was
moved INTO.

``_FiledIdProvider`` -- the fake whose filing reassigns the id the way
Graph's ``/move`` does -- lives in ``_watch_harness`` with the other shared
watcher fakes and is imported from there.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from loguru import logger

from _watch_harness import (
    CONFIDENTIAL_ID,
    FINAL_CURSOR,
    FOLDER,
    INBOX_ID,
    CountingClassifier,
    _context,
    _FiledIdProvider,
    _message,
    _ReassignedId,
)
from inbox_zero_connector.providers.base import ChangeSet, Container
from inbox_zero_connector.providers.outlook.provider import FiledMessage
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.state import WatchState


@pytest.fixture(autouse=True)
def _clear_destination_warnings() -> Iterator[None]:
    """#194's warning is bounded to once per folder PER PROCESS, so tests own both ends.

    Autouse rather than requested by name because the bound is invisible from a
    test that does not know about it: a row asserting the line fires would pass
    or fail on whether an EARLIER row in this file had already spent that
    folder's one warning, which is a test result that depends on collection
    order. Clearing before and after makes every row start from silence.
    """
    watcher._reset_destination_warnings()
    yield
    watcher._reset_destination_warnings()


@pytest.fixture
def captured_warnings() -> Iterator[list[str]]:
    """Every WARNING-or-above line loguru emitted, as plain strings.

    A loguru SINK rather than pytest's ``caplog``: this package logs through
    loguru, which does not propagate into the stdlib ``logging`` tree ``caplog``
    reads, so these assertions would run against an always-empty list.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING", format="{level} {message}")
    yield lines
    logger.remove(sink_id)


DEST_ID = "Folder_Business_Action"
"""Container id of the folder ``CountingClassifier`` files into by default."""


def _graph_containers() -> tuple[Container, ...]:
    """Inbox, a confidential folder, and the taxonomy destination as real folders.

    A Graph mailbox watches each folder as its own delta stream, so the folder
    a message is filed INTO is a stream of its own -- which is the whole of
    #173.
    """
    return (
        Container(id=INBOX_ID, name="Inbox", well_known="inbox", parent_id=None, exclusive=True),
        Container(
            id=CONFIDENTIAL_ID,
            name="Client Matters",
            well_known=None,
            parent_id=None,
            exclusive=True,
        ),
        Container(id=DEST_ID, name=FOLDER, well_known=None, parent_id=None, exclusive=True),
    )


def test_a_move_marks_the_post_move_id_in_the_destination_stream_window() -> None:
    """THE #173 repro: the destination folder's window is where the id returns.

    The watcher polls the inbox folder as one stream and the destination folder
    as another. Marking the post-move id in the SOURCE stream's window leaves
    the destination stream unguarded, so the next poll of that stream meets the
    moved message as new and files it again.
    """
    provider = _FiledIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=_graph_containers(),
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    watcher.poll_once(context)

    assert state.seen_message(INBOX_ID, "m1") is True
    assert state.seen_message(DEST_ID, "m1-moved") is True


def test_the_destination_stream_does_not_refile_the_moved_message() -> None:
    """The consequence end to end: polling the destination folder files nothing."""
    state = WatchState()
    containers = _graph_containers()
    source = _FiledIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=containers,
    )
    watcher.poll_once(
        _context(source, state=state, classifier=CountingClassifier()).model_copy(
            update={"container_id": INBOX_ID}
        )
    )

    destination = _FiledIdProvider(
        ChangeSet(
            messages=(_message("m1-moved", containers=(DEST_ID,)),),
            cursor="history-2002",
        ),
        containers=containers,
    )
    summary = watcher.poll_once(
        _context(destination, state=state, classifier=CountingClassifier()).model_copy(
            update={"container_id": DEST_ID}
        )
    )

    assert summary.skipped_already_seen == 1
    assert summary.applied == 0
    assert destination.filed == []


class _StableIdProvider(_FiledIdProvider):
    """A per-folder mailbox whose ids SURVIVE the move (#81).

    Graph reassigns and Gmail does not, and the two properties that follow from
    that -- per-folder streams, stable ids -- are independent. A provider with
    both is the shape the equal-ids early return got wrong: it offers the SAME
    id back through the destination folder's own delta stream, which the source
    window never guards.
    """

    def file_message(self, message_id: str, folder: str) -> str:
        self.filed.append((message_id, folder))
        return _ReassignedId(
            f"{self.filed_as_prefix}{folder}",
            message_id=message_id,
            destination_id=self.destination_id,
        )


def test_a_stable_id_still_marks_in_the_destination_stream_window() -> None:
    """A stable id is not a reason to skip the destination mark (#81).

    ``post_move_id == message.id`` used to return before the destination mark
    on the reasoning that Gmail's ids do not change -- but that reasoning is
    about Gmail's ONE stream. On a per-folder provider with stable ids the
    destination window is left unguarded and the message refiles forever.
    """
    provider = _StableIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=_graph_containers(),
        destination_id=DEST_ID,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    watcher.poll_once(context)

    assert state.seen_message(INBOX_ID, "m1") is True
    assert state.seen_message(DEST_ID, "m1") is True


PREFIX = "rTech/"
"""The folder prefix a real install ships (``config_paths.py`` names it).

Every container in a prefixed mailbox carries ``prefix + folder`` as its
display NAME, while the classification names the RAW taxonomy folder -- so a
name match cannot resolve the destination at all in this configuration.
"""

PREFIXED_DEST_ID = "Folder_rTech_Business_Action"


def _prefixed_containers() -> tuple[Container, ...]:
    """A mailbox whose taxonomy folders live under a non-empty folder prefix."""
    return (
        Container(id=INBOX_ID, name="Inbox", well_known="inbox", parent_id=None, exclusive=True),
        Container(
            id=PREFIXED_DEST_ID,
            name=f"{PREFIX}{FOLDER}",
            well_known=None,
            parent_id=None,
            exclusive=True,
        ),
    )


def test_a_prefixed_mailbox_marks_the_post_move_id_in_the_destination_stream() -> None:
    """THE #173 round-3 refutation: a non-empty folder_prefix broke the name match.

    The adapter files into ``prefix + folder`` and ``list_containers`` reports
    that prefixed displayName, but the classification names the RAW folder. A
    name match therefore resolves NOTHING in the configuration real installs
    ship, the mark falls back to the source window, and the refile loop
    survives untouched. The provider-reported destination id is what closes it.
    """
    provider = _FiledIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=_prefixed_containers(),
        filed_as_prefix=PREFIX,
        destination_id=PREFIXED_DEST_ID,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    watcher.poll_once(context)

    assert state.seen_message(PREFIXED_DEST_ID, "m1-moved") is True
    assert state.seen_message(INBOX_ID, "m1-moved") is False


def test_a_prefixed_destination_stream_does_not_refile_the_moved_message() -> None:
    """The consequence in a prefixed mailbox: polling the destination files nothing."""
    state = WatchState()
    containers = _prefixed_containers()
    source = _FiledIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=containers,
        filed_as_prefix=PREFIX,
        destination_id=PREFIXED_DEST_ID,
    )
    watcher.poll_once(
        _context(source, state=state, classifier=CountingClassifier()).model_copy(
            update={"container_id": INBOX_ID}
        )
    )

    destination = _FiledIdProvider(
        ChangeSet(
            messages=(_message("m1-moved", containers=(PREFIXED_DEST_ID,)),),
            cursor="history-2002",
        ),
        containers=containers,
        filed_as_prefix=PREFIX,
        destination_id=PREFIXED_DEST_ID,
    )
    summary = watcher.poll_once(
        _context(destination, state=state, classifier=CountingClassifier()).model_copy(
            update={"container_id": PREFIXED_DEST_ID}
        )
    )

    assert summary.skipped_already_seen == 1
    assert summary.applied == 0
    assert destination.filed == []


def test_a_duplicate_display_name_does_not_divert_the_mark() -> None:
    """Graph allows the same displayName under different parents; the id decides.

    A name match takes the first dict-order hit, so a decoy folder sharing the
    destination's display name silently captures the mark. The provider-reported
    id names exactly one container and cannot collide.
    """
    decoy_id = "Folder_Decoy_Business_Action"
    containers = (
        Container(id=INBOX_ID, name="Inbox", well_known="inbox", parent_id=None, exclusive=True),
        Container(id=decoy_id, name=FOLDER, well_known=None, parent_id=None, exclusive=True),
        Container(id=DEST_ID, name=FOLDER, well_known=None, parent_id="p2", exclusive=True),
    )
    provider = _FiledIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=containers,
        destination_id=DEST_ID,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    watcher.poll_once(context)

    assert state.seen_message(DEST_ID, "m1-moved") is True
    assert state.seen_message(decoy_id, "m1-moved") is False


def test_an_unindexed_destination_falls_back_to_the_current_stream_window() -> None:
    """No container matches the taxonomy folder -- today's behaviour, unchanged.

    ``FakeProvider``'s default container listing has no folder named ``FOLDER``,
    so the destination cannot be resolved and the mark must land where it always
    did rather than being dropped.
    """
    provider = _FiledIdProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    watcher.poll_once(context)

    assert state.seen_message(INBOX_ID, "m1") is True
    assert state.seen_message(INBOX_ID, "m1-moved") is True


def test_an_unresolved_destination_on_a_per_folder_stream_warns(
    captured_warnings: list[str],
) -> None:
    """RED (#194): the fallback to the source window was completely SILENT.

    #194's second clause, on the exact shape it names: a prefixed mailbox, so
    the name match cannot resolve, AND an adapter reporting no destination id,
    so the fallback is what runs. The post-move mark then lands in the SOURCE
    stream's window -- a window the destination folder's own delta poll never
    consults -- and the round reports ``applied`` with a committed cursor, so
    nothing else in the module notices. Two INFO lines that both read like
    success were the operator's entire signal.

    The line must NAME THE FOLDER AND THE SOURCE STREAM, because the folder plus
    the adapter is the whole diagnosis, and must say the write reported no
    destination container, because that is the fact a reader needs to connect it
    to a refile.

    RED-FIRST, observed before the fix::

        AssertionError: the fallback to the source window must be reported;
        captured=[]
    """
    provider = _FiledIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=_prefixed_containers(),
        filed_as_prefix=PREFIX,
        destination_id=None,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    watcher.poll_once(context)

    assert state.seen_message(PREFIXED_DEST_ID, "m1-moved") is False
    assert state.seen_message(INBOX_ID, "m1-moved") is True

    assert captured_warnings, (
        f"the fallback to the source window must be reported; captured={captured_warnings}"
    )
    line = captured_warnings[0]
    assert FOLDER in line, f"the warning must name the folder; line={line}"
    assert INBOX_ID in line, f"the warning must name the source stream; line={line}"
    assert "no destination container" in line, f"the warning must name the cause; line={line}"


def test_the_unresolved_destination_warning_is_said_once_per_folder(
    captured_warnings: list[str],
) -> None:
    """The BOUND, and it is the half that keeps the warning readable.

    A per-message line would put one WARNING on every message of every round for
    the life of the process, which is the shape #189 spent a round removing from
    the add-on loader: an operator who cannot read the log cannot act on it. The
    fact reported is a property of the FOLDER and the adapter, not of any one
    message, so once per folder carries the same diagnosis at a survivable
    volume.

    Three messages, three fallbacks, one line.
    """
    messages = tuple(_message(f"m{index}", containers=(INBOX_ID,)) for index in range(1, 4))
    provider = _FiledIdProvider(
        ChangeSet(messages=messages, cursor=FINAL_CURSOR),
        containers=_prefixed_containers(),
        filed_as_prefix=PREFIX,
        destination_id=None,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    summary = watcher.poll_once(context)

    assert summary.applied == 3, "all three must still file; the warning changes no behaviour"
    assert len(captured_warnings) == 1, (
        f"one line per folder, not one per message; captured={captured_warnings}"
    )


def test_a_mailbox_wide_stream_does_not_warn_about_its_own_window(
    captured_warnings: list[str],
) -> None:
    """THE NEGATIVE CONTROL, and the reason the warning is keyed on the stream shape.

    Gmail is ONE mailbox-wide stream (``container_id is None``), so the source
    window IS the window the post-move id comes back through. The fallback is
    not a risk there, it is the correct answer -- and warning about it would put
    a WARNING in front of every Gmail operator, every round, for behaviour that
    is working. Without this row the fix would be a false alarm on the kit's own
    default provider.
    """
    provider = _FiledIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=_prefixed_containers(),
        filed_as_prefix=PREFIX,
        destination_id=None,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier())

    watcher.poll_once(context)

    assert context.container_id is None, "this is the Gmail shape the guard keys on"
    assert not captured_warnings, (
        f"a mailbox-wide stream has one window and nothing to warn about; "
        f"captured={captured_warnings}"
    )


def test_a_resolved_destination_never_warns(captured_warnings: list[str]) -> None:
    """THE OTHER NEGATIVE CONTROL: the shipped Graph adapter reports its id.

    This is the path every real install takes today, which is why #194 calls the
    defect dormant. It must stay quiet, or the warning fires on the healthy case
    and stops meaning anything on the broken one.
    """
    provider = _FiledIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=_prefixed_containers(),
        filed_as_prefix=PREFIX,
        destination_id=PREFIXED_DEST_ID,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    watcher.poll_once(context)

    assert state.seen_message(PREFIXED_DEST_ID, "m1-moved") is True
    assert not captured_warnings, (
        f"a destination the adapter reported is not a fallback; captured={captured_warnings}"
    )


# ---------------------------------------------------------------------------
# #194 round 5 -- the guard belongs on the ANSWER, not on ``None``. Three more
# shapes reach the same unguarded destination window, and round 4's check saw
# none of them: a name match onto the WRONG container answers non-None, a move
# receipt with no new id returns before the check ran, and the once-bound was
# keyed on a folder name that several source streams share.
# ---------------------------------------------------------------------------


DECOY_ID = "Folder_Decoy_Business_Action"
"""A second container whose displayName is the RAW taxonomy folder.

Graph permits one displayName under two parents, which
``_destination_container_id``'s own module docstring records. This is the
container a first-match-wins name scan hits when the adapter reports nothing and
the real destination carries the prefixed name.
"""


class _NoMovedIdProvider(_FiledIdProvider):
    """An adapter whose move landed and could not report the new id.

    The SHIPPED Graph shape, not an invented one:
    ``providers.outlook.provider`` returns exactly this when ``/move`` answers
    without an id, logging "reported no id, receipt id UNKNOWN". The real
    ``FiledMessage`` class is used rather than the harness's stand-in, because
    the point of these rows is that the shipped adapter produces the shape.
    """

    def file_message(self, message_id: str, folder: str) -> str:
        self.filed.append((message_id, folder))
        if self.file_error is not None:
            raise self.file_error
        return FiledMessage(
            f"{self.filed_as_prefix}{folder}",
            message_id=None,
            destination_id=self.destination_id,
        )


def test_a_name_match_onto_a_decoy_container_is_reported(
    captured_warnings: list[str],
) -> None:
    """RED (#194 round 5): the guard was STRUCTURALLY UNREACHABLE on a real refile.

    Round 4 warned only when the name match ALSO came back ``None``. Here it
    comes back with an id -- the DECOY's -- because the scan is first-match-wins
    over display names and the decoy carries the raw taxonomy folder while the
    adapter actually wrote into the prefixed one. The mark therefore lands in a
    window nothing polls for this message, the real destination stream refiles
    it, and the old ``if destination_id is None`` could never fire.

    A name guess producing SOMETHING is not evidence it produced the right
    thing. The fact that determines the risk is that the write did not report
    where it went, which is what the guard now asks.

    RED-FIRST, observed before the fix::

        AssertionError: a mark placed by a name guess must be reported;
        captured=[]
    """
    containers = (
        Container(id=INBOX_ID, name="Inbox", well_known="inbox", parent_id=None, exclusive=True),
        Container(id=DECOY_ID, name=FOLDER, well_known=None, parent_id=None, exclusive=True),
        Container(
            id=PREFIXED_DEST_ID,
            name=f"{PREFIX}{FOLDER}",
            well_known=None,
            parent_id="p2",
            exclusive=True,
        ),
    )
    provider = _FiledIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=containers,
        destination_id=None,
        filed_as_prefix=PREFIX,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    watcher.poll_once(context)

    assert state.seen_message(DECOY_ID, "m1-moved") is True, "the decoy captured the mark"
    assert state.seen_message(PREFIXED_DEST_ID, "m1-moved") is False
    assert captured_warnings, (
        f"a mark placed by a name guess must be reported; captured={captured_warnings}"
    )


def test_a_move_receipt_with_no_new_id_is_reported_on_a_per_folder_stream(
    captured_warnings: list[str],
) -> None:
    """RED (#194 round 5): the early return skipped round 4's check entirely.

    ``post_move_id is None`` is the shipped Graph adapter's own documented
    receipt for a move that landed without an id in the response, and
    ``_mark_applied_seen`` returned on it BEFORE the warning ran. No destination
    mark is made at all, so the destination folder's own delta poll meets the
    moved message as new -- the same unguarded window the fallback case has, with
    even less placed in it -- and the watcher said nothing. The adapter's own
    WARNING is about the RECEIPT id, not about the replay window, so nothing
    told the operator the destination stream was unguarded.

    The check now sits ABOVE the return, because the fact it reports is a
    property of the write, not of what the resolution afterwards managed to find.

    RED-FIRST, observed before the fix::

        AssertionError: a move that reported no destination must be reported;
        captured=[]
    """
    provider = _NoMovedIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=_prefixed_containers(),
        destination_id=None,
        filed_as_prefix=PREFIX,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    summary = watcher.poll_once(context)

    assert summary.applied == 1, "the warning changes no behaviour; the message still files"
    assert state.seen.get(PREFIXED_DEST_ID) is None, "no destination mark was made at all"
    assert captured_warnings, (
        f"a move that reported no destination must be reported; captured={captured_warnings}"
    )


def test_the_shipped_graph_no_id_receipt_is_reported_even_with_a_destination_id(
    captured_warnings: list[str],
) -> None:
    """RED (#194 round 5): the SHIPPED adapter's no-id path, reported id and all.

    ``providers.outlook.provider`` returns ``FiledMessage(message_id=None,
    destination_id=<the id it POSTed>)`` -- so on the real adapter the
    destination IS reported while the post-move id is not. Round 4's guard keyed
    on the destination, which is present, and the early return fired first
    anyway, so this path was silent from both directions.

    Nothing is placed in the destination window here either, and this is the one
    no-id shape a real install actually produces rather than a fake's.

    THE WARNING IS DELIBERATELY SPENT ON A REPORTED DESTINATION. The risk is not
    that the container is unknown -- it is that the moved message has no replay
    mark anywhere, and a reported id does not put one there when there is no id
    to mark.

    RED-FIRST, observed before the fix::

        AssertionError: the shipped no-id receipt must be reported;
        captured=[]
    """
    provider = _NoMovedIdProvider(
        ChangeSet(messages=(_message("m1", containers=(INBOX_ID,)),), cursor=FINAL_CURSOR),
        containers=_prefixed_containers(),
        destination_id=PREFIXED_DEST_ID,
        filed_as_prefix=PREFIX,
    )
    state = WatchState()
    context = _context(provider, state=state, classifier=CountingClassifier()).model_copy(
        update={"container_id": INBOX_ID}
    )

    summary = watcher.poll_once(context)

    assert summary.applied == 1
    assert state.seen.get(PREFIXED_DEST_ID) is None, "no post-move id existed to mark"
    assert captured_warnings, (
        f"the shipped no-id receipt must be reported; captured={captured_warnings}"
    )


def test_a_second_source_stream_with_the_same_defect_is_still_reported(
    captured_warnings: list[str],
) -> None:
    """RED (#194 round 5): the once-bound was keyed on the folder NAME alone.

    On Graph every folder is its own stream, so one taxonomy folder is the
    destination of writes from many source streams. Keyed on the folder alone,
    the first stream to hit the condition spent the process's only line and every
    other affected stream was silent -- while the docstring claimed the diagnosis
    was "the folder plus the adapter", which is exactly the half a bare folder
    name drops.

    Two different source streams, the same unresolved destination folder, two
    lines. The bound still holds WITHIN a stream, which the row above this one
    pins.

    RED-FIRST, observed before the fix::

        AssertionError: each affected source stream must say it once; captured
        one line for two streams
    """
    state = WatchState()
    other_source = "Folder_Other_Source_Stream"
    containers = (
        *_prefixed_containers(),
        Container(id=other_source, name="Other", well_known=None, parent_id=None, exclusive=True),
    )
    for index, stream in enumerate((INBOX_ID, other_source), start=1):
        provider = _FiledIdProvider(
            ChangeSet(
                messages=(_message(f"m{index}", containers=(stream,)),),
                cursor=f"history-{2000 + index}",
            ),
            containers=containers,
            destination_id=None,
            filed_as_prefix=PREFIX,
        )
        watcher.poll_once(
            _context(provider, state=state, classifier=CountingClassifier()).model_copy(
                update={"container_id": stream}
            )
        )

    assert len(captured_warnings) == 2, (
        f"each affected source stream must say it once; captured={captured_warnings}"
    )
    assert INBOX_ID in captured_warnings[0]
    assert other_source in captured_warnings[1]
