"""Tests for the watch cursor state layer.

Every test here runs with no credential, no socket, and no Graph client. That
is a property of the module under test, not an accident of the fixtures: the
watch state was split away from the loop precisely so its correctness -- crash
durability, replay rejection, resync bookkeeping -- could be proven without a
live mailbox.

The ``isolated_dirs`` autouse fixture in ``conftest.py`` redirects the data
directory to a ``tmp_path``, so ``state_path(GOOGLE)`` resolves inside the test's own
sandbox and no test can touch a real deployment's cursor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inbox_zero_connector.config import PROVIDER_NAMES
from inbox_zero_connector.services.watch.state import (
    MAILBOX_WIDE_KEY,
    PASS_VOLUME_HISTORY,
    SEEN_LIMIT,
    WatchCursor,
    WatchState,
    state_path,
)

INBOX = "AAMkAGI2-inbox"
OTHER = "AAMkAGI2-archive"

GOOGLE, MICROSOFT = PROVIDER_NAMES
"""The two installed providers, taken from the constant rather than spelled out.

A literal ``"google"`` here would keep passing if the connector renamed or
added a provider, so the tests would stop describing the deployment they
guard.
"""

# A realistic opaque deltaLink. The test asserts it survives BYTE FOR BYTE:
# the token encodes the original query parameters, so a module that helpfully
# normalized, re-encoded, or stripped anything from it would break the next
# round in a way no type checker would catch.
DELTA_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"
    "?$deltatoken=abc123-XYZ_%2Fplus%2Band%3Dequals"
)


def test_cursor_round_trips_through_the_state_file_verbatim() -> None:
    """A saved cursor reloads with its opaque link unchanged."""
    state = WatchState()
    state.record_cursor(INBOX, DELTA_LINK)
    state.save(GOOGLE)

    reloaded = WatchState.load(GOOGLE)
    cursor = reloaded.cursor_for(INBOX)

    assert cursor is not None
    assert cursor.token == DELTA_LINK
    assert cursor.container_id == INBOX
    assert cursor.stored_at != ""


def test_unknown_folder_returns_none_rather_than_raising() -> None:
    """No cursor is an ordinary state -- first run, and just after a resync."""
    state = WatchState()
    state.record_cursor(INBOX, DELTA_LINK)

    assert state.cursor_for(OTHER) is None
    assert WatchState().cursor_for(INBOX) is None


def test_a_crash_mid_write_leaves_the_previous_cursor_intact() -> None:
    """The atomic write is what stops a torn file stranding the watcher.

    The crash is simulated by making the temp-file write blow up AFTER the
    first good save. If the module wrote in place, the target would already be
    truncated or half-written by the time the failure landed; because it writes
    to a sibling temp file and only then ``os.replace``s, the old complete
    state is still on disk and still readable.
    """
    first = WatchState()
    first.record_cursor(INBOX, DELTA_LINK)
    first.save(GOOGLE)

    second = WatchState()
    second.record_cursor(INBOX, "https://graph.microsoft.com/DOOMED-WRITE")

    real_open = Path.open

    def exploding_open(self: Path, *args: object, **kwargs: object) -> object:
        if not self.name.endswith(".tmp"):
            return real_open(self, *args, **kwargs)  # type: ignore[arg-type]
        # Leave a partial temp file on disk, exactly as a real crash would,
        # then fail. The handle is closed here rather than left to the
        # collector so the test does not leak a descriptor of its own.
        with real_open(self, *args, **kwargs) as handle:  # type: ignore[arg-type]
            handle.write("{ truncated garba")
        raise OSError("simulated crash mid-write")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", exploding_open)
        with pytest.raises(OSError, match="simulated crash"):
            second.save(GOOGLE)

    survivor = WatchState.load(GOOGLE)
    cursor = survivor.cursor_for(INBOX)
    assert cursor is not None
    assert cursor.token == DELTA_LINK, "the completed earlier cursor must survive"

    on_disk = json.loads(state_path(GOOGLE).read_text(encoding="utf-8"))
    assert on_disk["cursors"][INBOX]["token"] == DELTA_LINK


def test_resync_drops_the_cursor_and_bumps_the_generation() -> None:
    """The expired-token path. Expected operation, not failure.

    Whichever provider-specific signal triggered it -- and the adapter owns
    which one that is -- the state layer's job is identical: forget the token,
    count the event.
    """
    state = WatchState()
    state.record_cursor(INBOX, DELTA_LINK)
    assert state.generation_for(INBOX) == 0

    generation = state.resync_required(INBOX)

    assert generation == 1
    assert state.generation_for(INBOX) == 1
    assert state.cursor_for(INBOX) is None, "a stale link must not be replayable"

    # The counter is monotonic and survives the cursor it describes being
    # rebuilt, which is the whole reason it does not live on the cursor.
    state.record_cursor(INBOX, "https://graph.microsoft.com/fresh-after-resync")
    assert state.generation_for(INBOX) == 1
    assert state.resync_required(INBOX) == 2


def test_the_generation_survives_a_save_and_reload() -> None:
    """A restart is exactly when an operator reads the resync count."""
    state = WatchState()
    state.record_cursor(INBOX, DELTA_LINK)
    state.resync_required(INBOX)
    state.resync_required(INBOX)
    state.save(GOOGLE)

    assert WatchState.load(GOOGLE).generation_for(INBOX) == 2


def test_resync_keeps_the_replay_window() -> None:
    """A full resync re-reads everything, so the guard is needed most there."""
    state = WatchState()
    state.mark_seen(INBOX, "msg-1")
    state.resync_required(INBOX)

    assert state.seen_message(INBOX, "msg-1")


def test_the_guard_rejects_a_replayed_message() -> None:
    """Providers document replays as expected, so processing must be idempotent."""
    state = WatchState()
    assert not state.seen_message(INBOX, "msg-1")

    state.mark_seen(INBOX, "msg-1")

    assert state.seen_message(INBOX, "msg-1")
    # Folders do not share a window: the same id in another folder is a
    # different event and must still be processed.
    assert not state.seen_message(OTHER, "msg-1")


def test_marking_a_seen_message_again_does_not_reorder_the_window() -> None:
    """One id that keeps replaying must not hold the window open."""
    state = WatchState()
    state.mark_seen(INBOX, "msg-1")
    state.mark_seen(INBOX, "msg-2")
    state.mark_seen(INBOX, "msg-1")

    assert state.seen[INBOX] == ["msg-1", "msg-2"]


def test_the_replay_window_is_bounded_and_evicts_oldest_first() -> None:
    """An unbounded guard is a slow leak on an unattended watcher.

    REWRITTEN (#161b). It used to mark all ``SEEN_LIMIT + 10`` ids inside ONE
    pass and assert the window had already evicted ten of them -- which is the
    self-defeating eviction itself, asserted as the contract. The bound is real
    and is asserted here still; what changed is WHEN it applies. Eviction may
    only drop carryover from earlier passes, so the ten oldest age out when the
    marks that push them out belong to a later pass.

    AMENDED (#161 round 3): each pass now stores its cursor, because eviction is
    tied to that commit. An id whose round never committed is one the provider
    re-offers, so the window refuses to shrink below it -- see
    ``test_watchstate_window_commitment.py``. The bound asserted here is the
    committed-stream behaviour, which is every ordinary round.
    """
    state = WatchState()
    state.begin_pass()
    for index in range(10):
        state.mark_seen(INBOX, f"msg-{index}")
    state.record_cursor(INBOX, "token-1")
    state.begin_pass()
    for index in range(10, SEEN_LIMIT + 10):
        state.mark_seen(INBOX, f"msg-{index}")
    state.record_cursor(INBOX, "token-2")
    # AT THE BOUNDARY, not mid-pass (#161 A1). Eviction leaves the running
    # pass's own marks alone, so the window is allowed to sit over the bound
    # until the pass that filled it is over; ``begin_pass`` is where the size
    # is reasserted, and the size it reasserts is the largest recent pass.
    state.begin_pass()

    assert len(state.seen[INBOX]) == SEEN_LIMIT
    # The ten oldest aged out; the newest are all still guarded.
    assert not state.seen_message(INBOX, "msg-0")
    assert not state.seen_message(INBOX, "msg-9")
    assert state.seen_message(INBOX, "msg-10")
    assert state.seen_message(INBOX, f"msg-{SEEN_LIMIT + 9}")


def test_a_replay_outside_the_window_is_reported_as_new() -> None:
    """The honest failure mode, asserted rather than left to the docstring.

    This is the documented limit of the guard: an id evicted by the bound
    reads as unseen and WILL be reprocessed by a caller that trusts this
    answer. It is asserted here so the behaviour is a stated contract instead
    of a surprise found in production.

    REWRITTEN (#161b) for the same reason as the bound test above: the ancient
    id is CARRYOVER, and carryover is what eviction is allowed to drop. Marking
    it in the pass that overflows would now protect it, which is the fix, not a
    loosening of this contract.

    AMENDED (#161 round 3): the rounds commit their cursors, because an id
    belonging to an UNCOMMITTED round is one the provider will re-offer and the
    window now refuses to evict it. Ageing out is a fact about carryover the
    stream has moved past, which is what committing states.
    """
    state = WatchState()
    state.begin_pass()
    state.mark_seen(INBOX, "msg-ancient")
    state.record_cursor(INBOX, "token-1")
    state.begin_pass()
    for index in range(SEEN_LIMIT):
        state.mark_seen(INBOX, f"msg-{index}")
    state.record_cursor(INBOX, "token-2")
    state.begin_pass()

    assert not state.seen_message(INBOX, "msg-ancient")


def test_the_replay_window_survives_a_save_and_reload() -> None:
    """A watcher restart must not reprocess everything it already handled."""
    state = WatchState()
    state.record_cursor(INBOX, DELTA_LINK)
    state.mark_seen(INBOX, "msg-1")
    state.save(GOOGLE)

    assert WatchState.load(GOOGLE).seen_message(INBOX, "msg-1")


def test_load_returns_empty_state_when_no_file_exists() -> None:
    """First run is not a defect."""
    assert not state_path(GOOGLE).exists()

    state = WatchState.load(GOOGLE)

    assert state.cursors == {}
    assert state.cursor_for(INBOX) is None


def test_an_unreadable_state_file_starts_fresh_rather_than_stopping() -> None:
    """Recovery from a lost cursor is already supported -- it is a full resync."""
    state_path(GOOGLE).parent.mkdir(parents=True, exist_ok=True)
    state_path(GOOGLE).write_text("{ this is not json", encoding="utf-8")

    state = WatchState.load(GOOGLE)

    assert state.cursor_for(INBOX) is None


def test_the_state_file_lands_in_the_data_dir_and_leaves_no_temp_behind() -> None:
    """No new location is invented, and the atomic write cleans up after itself."""
    state = WatchState()
    state.record_cursor(INBOX, DELTA_LINK)
    written = state.save(GOOGLE)

    assert written == state_path(GOOGLE)
    assert written.parent == state_path(GOOGLE).parent
    assert not written.with_name(f"{written.name}.tmp").exists()


def test_recording_a_cursor_carries_the_generation_forward() -> None:
    """Advancing within a sync run is not a resync and must not bump the count.

    Asserted AFTER a resync, where the expected generation is non-zero. The
    same assertion against a fresh folder would read ``== 0`` and would be
    satisfied by a cursor that simply hardcoded zero -- proven by mutation:
    replacing the carried lookup with a literal 0 left that version of this
    test green.
    """
    state = WatchState()
    state.record_cursor(INBOX, DELTA_LINK)
    state.resync_required(INBOX)

    state.record_cursor(INBOX, "https://graph.microsoft.com/fresh-after-resync")
    state.record_cursor(INBOX, "https://graph.microsoft.com/second-round")

    cursor = state.cursor_for(INBOX)
    assert cursor is not None
    assert cursor.generation == 1, "the cursor must carry the folder's resync count"
    assert state.generation_for(INBOX) == 1


def test_watch_cursor_rejects_unknown_fields() -> None:
    """extra=forbid: a typo'd key is a loud error, not a silently dropped cursor."""
    with pytest.raises(ValueError, match=r"[Ee]xtra"):
        WatchCursor(container_id=INBOX, token=DELTA_LINK, deltalink="typo")  # type: ignore[call-arg]


# -- container_id: mailbox-wide vs per-container -------------------------
#
# The two shapes are not a configuration choice. A provider with a
# mailbox-wide change stream (one token for the whole mailbox) stores exactly
# one cursor with ``container_id=None``; a provider with only per-container
# streams stores one cursor per container. The store must serve both without
# either shape being able to reach the other's slot.

GMAIL_HISTORY_ID = "8675309"
"""A Gmail ``historyId``, stored as a string and never parsed as a number.

Deliberately NOT a URL. A cursor field that only ever held a deltaLink would
pass every test in this file written before this one, so the mailbox-wide
tests use a token of the other provider's shape on purpose.
"""


def test_a_mailbox_wide_cursor_round_trips_through_the_state_file() -> None:
    """``container_id=None`` is a real, storable, reloadable cursor."""
    state = WatchState()
    state.record_cursor(None, GMAIL_HISTORY_ID)
    state.save(GOOGLE)

    cursor = WatchState.load(GOOGLE).cursor_for(None)

    assert cursor is not None
    assert cursor.token == GMAIL_HISTORY_ID
    assert cursor.container_id is None, "the mailbox-wide cursor names no container"


def test_a_per_container_cursor_round_trips_through_the_state_file() -> None:
    """The other shape, asserted alongside so neither is proven by the other."""
    state = WatchState()
    state.record_cursor(INBOX, DELTA_LINK)
    state.save(GOOGLE)

    cursor = WatchState.load(GOOGLE).cursor_for(INBOX)

    assert cursor is not None
    assert cursor.token == DELTA_LINK
    assert cursor.container_id == INBOX


def test_the_mailbox_wide_cursor_does_not_collide_with_a_container_cursor() -> None:
    """Both shapes coexist in one store, and neither reads the other's slot.

    This is the test that a sentinel-string key would fail. Keying ``None`` as
    ``""`` or ``"*"`` puts it in the same namespace as provider container ids;
    the assertions below hold only because the key cannot be spelled by any
    container id.
    """
    state = WatchState()
    state.record_cursor(None, GMAIL_HISTORY_ID)
    state.record_cursor(INBOX, DELTA_LINK)
    state.record_cursor(OTHER, "https://graph.microsoft.com/other-folder")

    assert len(state.cursors) == 3

    mailbox_wide = state.cursor_for(None)
    per_container = state.cursor_for(INBOX)
    assert mailbox_wide is not None
    assert per_container is not None
    assert mailbox_wide.token == GMAIL_HISTORY_ID
    assert per_container.token == DELTA_LINK

    # And it survives the JSON round trip, which is where a key that the
    # encoder mangled would quietly turn into a second, unreachable slot.
    state.save(GOOGLE)
    reloaded = WatchState.load(GOOGLE)
    assert len(reloaded.cursors) == 3
    reloaded_wide = reloaded.cursor_for(None)
    assert reloaded_wide is not None
    assert reloaded_wide.token == GMAIL_HISTORY_ID
    assert reloaded.cursor_for(INBOX) is not None


def test_the_mailbox_wide_slot_cannot_be_reached_by_any_container_id() -> None:
    """No string a provider could hand us as a container id reaches that slot.

    Asserted through the STORE, not against the constant. A test that only
    checked ``MAILBOX_WIDE_KEY`` would pass while the keying function ignored
    it entirely -- proven by mutation: replacing the key lookup with a literal
    ``""`` left a constant-only assertion green.

    The plausible sentinels below are the spellings a reviewer would reach for
    first, and each is a string a provider is permitted to emit. Graph folder
    ids are base64url text and Gmail label ids are ``Label_<n>`` plus the
    uppercase system names; none can carry a NUL byte, which is why the real
    key is unreachable by construction rather than by convention.
    """
    assert "\x00" in MAILBOX_WIDE_KEY

    for plausible_sentinel in ("", "*", "__mailbox__", "mailbox-wide", "None", INBOX):
        state = WatchState()
        state.record_cursor(None, GMAIL_HISTORY_ID)
        state.record_cursor(plausible_sentinel, DELTA_LINK)

        mailbox_wide = state.cursor_for(None)
        assert mailbox_wide is not None
        assert mailbox_wide.token == GMAIL_HISTORY_ID, (
            f"container id {plausible_sentinel!r} overwrote the mailbox-wide cursor"
        )
        assert len(state.cursors) == 2


def test_resync_works_for_a_mailbox_wide_cursor() -> None:
    """The resync path is shape-agnostic: same drop, same monotonic counter."""
    state = WatchState()
    state.record_cursor(None, GMAIL_HISTORY_ID)
    assert state.generation_for(None) == 0

    assert state.resync_required(None) == 1

    assert state.cursor_for(None) is None, "a stale token must not be replayable"
    assert state.generation_for(None) == 1
    assert state.resync_required(None) == 2


def test_resync_of_one_shape_leaves_the_other_untouched() -> None:
    """Resyncing the mailbox-wide stream must not drop a container's cursor."""
    state = WatchState()
    state.record_cursor(None, GMAIL_HISTORY_ID)
    state.record_cursor(INBOX, DELTA_LINK)

    state.resync_required(None)

    assert state.cursor_for(None) is None
    survivor = state.cursor_for(INBOX)
    assert survivor is not None
    assert survivor.token == DELTA_LINK
    assert state.generation_for(INBOX) == 0, "a container's counter is its own"


def test_a_mailbox_wide_resync_keeps_the_replay_window() -> None:
    """A full mailbox resync re-reads everything -- the guard is needed most here."""
    state = WatchState()
    state.mark_seen(None, "msg-1")
    state.resync_required(None)

    assert state.seen_message(None, "msg-1")


def test_the_replay_windows_of_the_two_shapes_do_not_share_ids() -> None:
    """Same message id, different stream, still a separate event."""
    state = WatchState()
    state.mark_seen(None, "msg-1")

    assert state.seen_message(None, "msg-1")
    assert not state.seen_message(INBOX, "msg-1")


def test_a_mailbox_wide_cursor_carries_its_generation_forward() -> None:
    """The carried-generation rule holds for the shape with no container id.

    Asserted after a resync so the expected value is non-zero: against a fresh
    stream this would read ``== 0`` and a cursor hardcoding zero would pass.
    """
    state = WatchState()
    state.record_cursor(None, GMAIL_HISTORY_ID)
    state.resync_required(None)

    state.record_cursor(None, "8675310")

    cursor = state.cursor_for(None)
    assert cursor is not None
    assert cursor.generation == 1
    assert state.generation_for(None) == 1


def test_container_id_defaults_to_the_mailbox_wide_shape() -> None:
    """A cursor constructed with only a token is mailbox-wide, not container-less-by-accident."""
    cursor = WatchCursor(token=GMAIL_HISTORY_ID)

    assert cursor.container_id is None


# -- provider scoping ----------------------------------------------------
#
# One host can watch a Google mailbox and a Microsoft mailbox at the same
# time. Both watchers write into the same data directory, and each save is a
# whole-file replace, so a shared filename means the last writer erases the
# other provider's cursors -- silently, because the surviving file is
# well-formed and parses cleanly.


def test_the_two_providers_write_to_different_files() -> None:
    """The defect this scoping exists to prevent, asserted at the path level.

    Asserted on the PATHS rather than only on the reloaded contents: a module
    that wrote one shared file but partitioned it by a provider key inside
    would still be two processes clobbering one file, and the clobber is the
    write, not the layout.
    """
    google = state_path(GOOGLE)
    microsoft = state_path(MICROSOFT)

    assert google != microsoft
    assert google.parent == microsoft.parent, "both still live in the data dir"
    assert GOOGLE in google.name
    assert MICROSOFT in microsoft.name


def test_each_provider_reads_back_only_its_own_cursors() -> None:
    """A Microsoft save must not be able to erase or be read as a Google cursor.

    Both providers store a cursor for the SAME container id on purpose. If the
    file were shared, the second save would overwrite the first and both
    reloads would return the same token -- which is exactly the corruption
    that reads as an ordinary cursor and sends one watcher to the wrong
    stream.
    """
    google_state = WatchState()
    google_state.record_cursor(None, GMAIL_HISTORY_ID)
    google_state.save(GOOGLE)

    microsoft_state = WatchState()
    microsoft_state.record_cursor(INBOX, DELTA_LINK)
    microsoft_state.save(MICROSOFT)

    reloaded_google = WatchState.load(GOOGLE)
    reloaded_microsoft = WatchState.load(MICROSOFT)

    google_cursor = reloaded_google.cursor_for(None)
    assert google_cursor is not None
    assert google_cursor.token == GMAIL_HISTORY_ID
    assert reloaded_google.cursor_for(INBOX) is None, "Graph's cursor leaked into Gmail's file"

    microsoft_cursor = reloaded_microsoft.cursor_for(INBOX)
    assert microsoft_cursor is not None
    assert microsoft_cursor.token == DELTA_LINK
    assert reloaded_microsoft.cursor_for(None) is None, "Gmail's cursor leaked into Graph's file"


def test_an_unknown_provider_name_is_rejected() -> None:
    """A typo must raise, never quietly open a state file of its own.

    An unrecognised name that is allowed through creates an empty file, and an
    empty file is indistinguishable from a first run: the watcher finds no
    cursor and resyncs the whole mailbox with no error anywhere, while the
    real cursor sits beside it under the correct name being read by nobody.
    """
    with pytest.raises(ValueError, match="unknown provider"):
        state_path("gogle")

    with pytest.raises(ValueError):
        WatchState().save("gogle")

    with pytest.raises(ValueError):
        WatchState.load("gogle")


@pytest.mark.parametrize(
    ("hostile", "reason"),
    [
        ("../../etc/cron.d/x", "path separator"),
        ("google/../../escape", "path separator"),
        ("/etc/passwd", "path separator"),
        ("..", "not a usable filename component"),
        ("", "not a usable filename component"),
    ],
)
def test_a_provider_name_that_would_escape_the_data_dir_is_rejected(
    hostile: str, reason: str
) -> None:
    """The guard belongs where the path is built, not where the name comes from.

    Every value reaching this function today originates in ``PROVIDER_NAMES``,
    so nothing hostile can arrive -- but a filename composed from a variable is
    a traversal shape whatever its current source, and only the code building
    the path can enforce containment for every future caller.

    THE MATCH ON THE TRAVERSAL MESSAGE IS LOAD-BEARING, not decoration. Every
    string below is also absent from ``PROVIDER_NAMES``, so a bare
    ``pytest.raises(ValueError)`` is satisfied by the membership check alone
    and says nothing about the traversal guards -- proven by mutation: deleting
    both traversal guards left the unmatched version of this test GREEN. Naming
    the reason is what makes the guard the thing under test.
    """
    with pytest.raises(ValueError, match=reason):
        state_path(hostile)

    with pytest.raises(ValueError, match=reason):
        WatchState().save(hostile)

    assert not (state_path(GOOGLE).parent.parent / "escape").exists()


def test_a_traversal_name_is_refused_even_if_it_is_a_known_provider_prefix() -> None:
    """Containment is checked independently of the known-provider list.

    A composed name beginning with a real provider is the case that would slip
    through an implementation which validated only the leading segment, and it
    is refused for the traversal reason -- not because the whole string missed
    the membership list.
    """
    with pytest.raises(ValueError, match="path separator"):
        state_path(f"{GOOGLE}/../../escape")


def test_the_state_path_stays_inside_the_data_dir_for_every_known_provider() -> None:
    """The positive half: every legal name resolves to a direct child, no escapes."""
    for provider in PROVIDER_NAMES:
        target = state_path(provider)
        assert target.parent == target.parent.resolve()
        assert target.resolve().parent == target.parent.resolve()


# ---------------------------------------------------------------------------
# The self-defeating eviction (#161b)
# ---------------------------------------------------------------------------


def test_one_pass_marking_more_than_the_limit_evicts_none_of_its_own_marks() -> None:
    """THE #161b repro: a 550-id page used to evict 100% of its own marks.

    Oldest-first eviction with no regard for the current pass means a page
    larger than ``SEEN_LIMIT`` pushes out the very ids it just recorded, so a
    replay of that page reprocesses every message on it. The window may exceed
    the bound WITHIN a pass; the bound applies to carryover.
    """
    state = WatchState()
    state.begin_pass()
    page = [f"page-{index}" for index in range(SEEN_LIMIT + 50)]
    for message_id in page:
        state.mark_seen(INBOX, message_id)

    assert all(state.seen_message(INBOX, message_id) for message_id in page)


def test_the_pass_that_overflowed_keeps_its_marks_into_the_next_pass() -> None:
    """The window is not allowed to grow without limit across passes.

    REWRITTEN (#161 A1). It used to assert the window was back at a flat
    ``SEEN_LIMIT`` one pass later and that ``page-0`` had aged out -- which IS
    the round-2 defect, written down as the contract: the 550-id round lost 50
    of its own marks the instant the next round started, and being offered the
    same page again reprocessed them, every round, forever.

    The bound still holds; it is now a FLOOR sized from observed volume. A pass
    of 550 keeps room for 550 while that pass is in the history, so nothing it
    marked is evicted before the next poll sees it, and the next pass's own
    marks come on top of it rather than in place of it.
    """
    state = WatchState()
    state.begin_pass()
    for index in range(SEEN_LIMIT + 50):
        state.mark_seen(INBOX, f"page-{index}")
    state.begin_pass()
    state.mark_seen(INBOX, "next-pass")

    assert len(state.seen[INBOX]) <= SEEN_LIMIT + 51
    assert state.seen_message(INBOX, "next-pass")
    assert state.seen_message(INBOX, f"page-{SEEN_LIMIT + 49}")
    assert state.seen_message(INBOX, "page-0")


def test_the_window_returns_to_the_bound_once_the_big_pass_ages_out() -> None:
    """The volume-sized headroom is temporary -- quiet rounds give it back.

    The pairing test for the one above: the window is ALLOWED to exceed
    ``SEEN_LIMIT`` while a large pass is in living memory, and is required to
    come back down once it is not. Without this half the sizing rule would be a
    high-water mark that never decays, which is the unbounded growth
    ``SEEN_LIMIT`` exists to prevent arriving by another door.

    AMENDED (#161 round 3): the decay is what a COMMITTED stream gets, so every
    round here stores its token. Handing the headroom back while the cursor is
    still withheld was the refuted behaviour -- it evicted exactly the ids the
    next poll re-offered.
    """
    state = WatchState()
    state.begin_pass()
    for index in range(SEEN_LIMIT + 50):
        state.mark_seen(INBOX, f"page-{index}")
    state.record_cursor(INBOX, "token-page")
    for round_index in range(PASS_VOLUME_HISTORY + 1):
        state.begin_pass()
        state.mark_seen(INBOX, f"quiet-{round_index}")
        state.record_cursor(INBOX, f"token-quiet-{round_index}")
    state.begin_pass()

    assert len(state.seen[INBOX]) <= SEEN_LIMIT


def test_carryover_from_earlier_passes_is_evicted_before_current_pass_marks() -> None:
    """Eviction takes the oldest CARRYOVER, never an id this pass recorded.

    AMENDED (#161 round 3): carryover is only evictable once its round's cursor
    committed, so both rounds store a token here. Ids from an uncommitted round
    are the set the provider re-offers, and the window holds them regardless of
    age.
    """
    state = WatchState()
    state.begin_pass()
    for index in range(SEEN_LIMIT):
        state.mark_seen(INBOX, f"old-{index}")
    state.record_cursor(INBOX, "token-old")
    state.begin_pass()
    for index in range(10):
        state.mark_seen(INBOX, f"new-{index}")
    state.record_cursor(INBOX, "token-new")
    state.begin_pass()

    assert len(state.seen[INBOX]) == SEEN_LIMIT
    assert all(state.seen_message(INBOX, f"new-{index}") for index in range(10))
    assert not state.seen_message(INBOX, "old-0")
    assert not state.seen_message(INBOX, "old-9")
    assert state.seen_message(INBOX, "old-10")


# ---------------------------------------------------------------------------
# Filing-attempt counts (#158 round 2) -- the bound on a withheld cursor
# ---------------------------------------------------------------------------


def test_an_unattempted_message_reports_zero_attempts() -> None:
    """ "No entry" is the honest answer for a message never refused, not an error."""
    assert WatchState().attempts_for(INBOX, "msg-1") == 0


def test_recording_an_attempt_returns_the_running_count() -> None:
    """The caller compares the returned count against its own bound."""
    state = WatchState()

    assert state.record_attempt(INBOX, "msg-1") == 1
    assert state.record_attempt(INBOX, "msg-1") == 2
    assert state.attempts_for(INBOX, "msg-1") == 2


def test_attempt_counts_survive_a_save_and_reload() -> None:
    """A restart must not reset the bound -- that is how a pin becomes permanent.

    The count is persisted for the same reason the cursor is: a watcher
    restarted between rounds would otherwise begin every message's retry
    budget again, and a message that can never be filed would pin the cursor
    forever across restarts exactly as it did with no bound at all.
    """
    state = WatchState()
    state.record_attempt(INBOX, "msg-1")
    state.record_attempt(INBOX, "msg-1")
    state.record_attempt(None, "msg-2")
    state.save(GOOGLE)

    reloaded = WatchState.load(GOOGLE)

    assert reloaded.attempts_for(INBOX, "msg-1") == 2
    assert reloaded.attempts_for(None, "msg-2") == 1


def test_attempt_counts_of_the_two_cursor_shapes_do_not_share_ids() -> None:
    """The map is keyed per stream, exactly as ``seen`` and ``cursors`` are."""
    state = WatchState()
    state.record_attempt(INBOX, "msg-1")

    assert state.attempts_for(None, "msg-1") == 0
    assert state.attempts_for(OTHER, "msg-1") == 0


def test_clearing_an_attempt_entry_prunes_it_rather_than_zeroing_it() -> None:
    """The map holds only messages currently mid-retry; a cleared id leaves no row."""
    state = WatchState()
    state.record_attempt(INBOX, "msg-1")
    state.record_attempt(INBOX, "msg-2")

    state.clear_attempts(INBOX, "msg-1")

    assert state.attempts_for(INBOX, "msg-1") == 0
    assert "msg-1" not in state.attempts[INBOX]
    assert state.attempts[INBOX]["msg-2"] == 1


def test_clearing_an_attempt_entry_that_was_never_recorded_is_not_an_error() -> None:
    """Every APPLIED message clears its entry, and most never had one."""
    WatchState().clear_attempts(INBOX, "msg-never")


def test_marking_a_message_seen_prunes_its_attempt_entry() -> None:
    """A seen message is out of the retry table: it can never be offered again."""
    state = WatchState()
    state.record_attempt(INBOX, "msg-1")

    state.mark_seen(INBOX, "msg-1")

    assert state.attempts_for(INBOX, "msg-1") == 0
