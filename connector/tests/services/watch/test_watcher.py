"""One polling round -- polling, scope, filing, and round state -- over fakes.

No credential, no socket, no model call. The DRAFTING half of the same round
lives in ``test_watcher_drafting.py``: this module was split on 2026-08-19
when the Sprint 4 drafting tests pushed it to 523 code lines, past the repo's
500-line ceiling (``scripts/check_code_size.py``). The seam is the concern,
not an arbitrary cut -- nothing here reaches ``create_draft``, and
``FakeProvider.create_draft`` raising is what proves it.

This module remains the HOME of the shared watcher fakes. ``FakeProvider``,
``CountingClassifier``, ``_message``, and ``_context`` are imported from here
by ``test_watcher_drafting.py``, ``test_watchloop.py``,
``test_watchloop_pass_failure.py``, and ``test_reauth_cycles.py``; changing
one of them changes all five modules.

Every test here builds a ``FakeProvider`` and a ``CountingClassifier``, so the
round is exercised end to end with nothing live behind it. That is a property
of the module under test rather than of the fixtures: ``poll_once`` takes its
provider and its classifier as injected values precisely so the whole state
table can be driven with no mailbox and no clock.

The classifier is a SPY as well as a stub. Several of these tests assert what
it was NOT asked about, which is the only way to prove the scope gate runs
before classification rather than after it -- the observable labelling
behaviour is identical either way, and the difference is whether confidential
mail was read.
"""

from __future__ import annotations

import pytest

from _watch_harness import (
    CONFIDENTIAL_ID,
    FINAL_CURSOR,
    FOLDER,
    INBOX_ID,
    PAGE_TOKEN,
    CountingClassifier,
    FakeProvider,
    _context,
    _FiledIdProvider,
    _message,
    _ReassignedId,
)
from inbox_zero_connector.providers.base import (
    ChangeSet,
    Container,
    MailProvider,
    Message,
    ProviderError,
)
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.audit import trace as trace
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.watch import round as watcher
from inbox_zero_connector.services.watch.scope import MailboxScope
from inbox_zero_connector.services.watch.state import MAILBOX_WIDE_KEY, WatchState

# ---------------------------------------------------------------------------
# The normal round
# ---------------------------------------------------------------------------


def test_a_normal_round_files_ledgers_and_stores_the_cursor() -> None:
    """The happy path end to end: message filed, receipt written, cursor stored."""
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"), _message("m2")), cursor=FINAL_CURSOR)
    )
    classifier = CountingClassifier()
    context = _context(provider, classifier=classifier)

    summary = watcher.poll_once(context)

    assert provider.filed == [("m1", FOLDER), ("m2", FOLDER)]
    assert classifier.seen_ids == ["m1", "m2"]
    assert summary.state is watcher.RoundState.COMMITTED
    assert (summary.seen, summary.classified, summary.applied, summary.failed) == (2, 2, 2, 0)
    assert summary.cursor_stored is True

    rows = ledger.read_all()
    assert [row.message_id for row in rows] == ["m1", "m2"]
    assert {row.status for row in rows} == {ledger.Status.APPLIED}
    assert {row.detail for row in rows} == {FOLDER}

    cursor = context.state.cursor_for(None)
    assert cursor is not None
    assert cursor.token == FINAL_CURSOR


def test_an_uncertain_classification_is_filed_into_review() -> None:
    """Low confidence still clears the inbox through the explicit Review folder."""
    provider = FakeProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    classifier = CountingClassifier(confidence=0.10)

    summary = watcher.poll_once(_context(provider, classifier=classifier))

    assert provider.filed == [("m1", "Review")]
    assert summary.applied == 1
    rows = ledger.read_all()
    assert [(row.message_id, row.detail, row.status) for row in rows] == [
        ("m1", "Review", ledger.Status.APPLIED)
    ]


def test_the_stored_cursor_is_replayed_verbatim_into_the_next_poll() -> None:
    """The token is opaque: it goes back to the provider byte for byte."""
    state = WatchState()
    state.record_cursor(None, "history-1000")
    provider = FakeProvider(ChangeSet(cursor=FINAL_CURSOR))

    watcher.poll_once(_context(provider, state=state))

    assert provider.polled == ["history-1000"]


def test_a_cold_start_polls_with_no_token_and_is_not_an_error() -> None:
    """No cursor is the ordinary state of a stream before its first round."""
    provider = FakeProvider(ChangeSet(cursor=FINAL_CURSOR))

    summary = watcher.poll_once(_context(provider))

    assert provider.polled == [None]
    assert summary.state is watcher.RoundState.COMMITTED


# ---------------------------------------------------------------------------
# Resync
# ---------------------------------------------------------------------------


def test_a_resync_round_resyncs_and_processes_nothing() -> None:
    """A refused token means the position is rejected -- act on none of it.

    The provider is scripted to return a message ALONGSIDE the resync flag,
    which is the case that separates "returned early" from "happened to have
    nothing to do". Nothing is classified, nothing is filed, and nothing is
    receipted.
    """
    state = WatchState()
    state.record_cursor(None, "history-stale")
    provider = FakeProvider(ChangeSet(messages=(_message("m1"),), resync_required=True))
    classifier = CountingClassifier()

    summary = watcher.poll_once(_context(provider, state=state, classifier=classifier))

    assert classifier.seen_ids == []
    assert provider.filed == []
    assert ledger.read_all() == []
    assert summary.state is watcher.RoundState.RESYNCED
    assert summary.resynced is True
    assert (summary.seen, summary.classified, summary.applied) == (0, 0, 0)


def test_a_resync_drops_the_cursor_bumps_the_generation_and_keeps_seen_ids() -> None:
    """The cursor goes, the generation moves, the replay window survives.

    Clearing the replay window on a resync would guarantee the reprocessing
    the window exists to prevent, because a resync re-reads everything.
    """
    state = WatchState()
    state.record_cursor(None, "history-stale")
    state.mark_seen(None, "m-old")
    provider = FakeProvider(ChangeSet(resync_required=True))

    summary = watcher.poll_once(_context(provider, state=state))

    assert state.cursor_for(None) is None
    assert state.generation_for(None) == 1
    assert state.seen_message(None, "m-old") is True
    assert summary.cursor_stored is False


# ---------------------------------------------------------------------------
# Scope -- the gate that must precede classification
# ---------------------------------------------------------------------------


def test_an_out_of_scope_message_is_never_classified() -> None:
    """THE ordering assertion: the classifier was not called for it AT ALL.

    Classification reads subject, sender, and snippet. A pipeline that
    classified first and discarded afterwards would produce identical
    labelling and would still have performed the read the client asked us not
    to perform, so the only evidence that distinguishes the two is what the
    classifier was shown.
    """
    provider = FakeProvider(
        ChangeSet(
            messages=(
                _message("in-scope"),
                _message("confidential", containers=(CONFIDENTIAL_ID,)),
            ),
            cursor=FINAL_CURSOR,
        )
    )
    classifier = CountingClassifier()
    scope = MailboxScope(mode="all_except", excluded_folders=("Client Matters",))

    summary = watcher.poll_once(_context(provider, scope=scope, classifier=classifier))

    assert classifier.seen_ids == ["in-scope"]
    assert "confidential" not in classifier.seen_ids
    assert provider.filed == [("in-scope", FOLDER)]
    assert [row.message_id for row in ledger.read_all()] == ["in-scope"]
    assert summary.skipped_out_of_scope == 1
    assert summary.classified == 1


def test_an_out_of_scope_message_is_not_marked_seen() -> None:
    """No action occurred, so it must not spend a slot in the replay window."""
    provider = FakeProvider(
        ChangeSet(messages=(_message("confidential", containers=(CONFIDENTIAL_ID,)),))
    )
    state = WatchState()
    scope = MailboxScope(mode="all_except", excluded_folders=("Client Matters",))

    watcher.poll_once(_context(provider, scope=scope, state=state, classifier=CountingClassifier()))

    assert state.seen_message(None, "confidential") is False


def test_any_excluded_container_blocks_a_message_carrying_several() -> None:
    """Any-exclusion-wins: an in-scope label cannot outvote an excluded one."""
    provider = FakeProvider(
        ChangeSet(messages=(_message("both", containers=(INBOX_ID, CONFIDENTIAL_ID)),))
    )
    classifier = CountingClassifier()
    scope = MailboxScope(mode="all_except", excluded_folders=("Client Matters",))

    summary = watcher.poll_once(_context(provider, scope=scope, classifier=classifier))

    assert classifier.seen_ids == []
    assert summary.skipped_out_of_scope == 1


def test_a_message_with_no_containers_is_not_classified() -> None:
    """Fails closed: "we know nothing about it" must not resolve to permission."""
    provider = FakeProvider(ChangeSet(messages=(_message("orphan", containers=()),)))
    classifier = CountingClassifier()

    summary = watcher.poll_once(_context(provider, classifier=classifier))

    assert classifier.seen_ids == []
    assert summary.skipped_out_of_scope == 1


# ---------------------------------------------------------------------------
# Replay guard
# ---------------------------------------------------------------------------


def test_an_already_seen_message_is_skipped() -> None:
    """The replay guard suppresses a repeat before any decision is made."""
    state = WatchState()
    state.mark_seen(None, "m1")
    provider = FakeProvider(ChangeSet(messages=(_message("m1"), _message("m2"))))
    classifier = CountingClassifier()

    summary = watcher.poll_once(_context(provider, state=state, classifier=classifier))

    assert classifier.seen_ids == ["m2"]
    assert provider.filed == [("m2", FOLDER)]
    assert summary.skipped_already_seen == 1
    assert summary.classified == 1


def test_a_processed_message_is_marked_seen_so_the_next_round_skips_it() -> None:
    provider = FakeProvider(ChangeSet(messages=(_message("m1"),)))
    state = WatchState()

    watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))

    assert state.seen_message(None, "m1") is True


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_a_failing_file_is_ledgered_as_failed_and_the_round_continues() -> None:
    """One contended message must not abandon every message behind it."""
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"), _message("m2")), cursor=FINAL_CURSOR),
        file_error=ProviderError("412 refused"),
    )
    classifier = CountingClassifier()

    summary = watcher.poll_once(_context(provider, classifier=classifier))

    assert [attempt[0] for attempt in provider.filed] == ["m1", "m2"]
    assert summary.failed == 2
    assert summary.applied == 0
    # UPDATED (#158): a round carrying a failure withholds its cursor, so it
    # ends PAGED rather than COMMITTED even though the final page carried one.
    assert summary.state is watcher.RoundState.PAGED
    assert summary.cursor_stored is False

    rows = ledger.read_all()
    assert [row.message_id for row in rows] == ["m1", "m2"]
    assert {row.status for row in rows} == {ledger.Status.FAILED}


def test_a_failure_receipt_records_a_code_never_the_provider_message() -> None:
    """Provider prose can quote the subject it refused. Only the code is kept."""
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"),)),
        file_error=ProviderError("refused message 'Quarterly review notes'"),
    )

    watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    row = ledger.read_all()[0]
    assert row.error_code == "ProviderError"
    assert "Quarterly" not in row.error_code


def test_a_failed_message_is_not_marked_seen_so_the_next_round_retries_it() -> None:
    """REWRITTEN (#158). This test used to assert the DEFECT, deliberately.

    It read ``test_a_failed_message_is_still_marked_seen`` and asserted
    ``seen_message(...) is True`` on the reasoning that the attempt happened
    and was receipted. That reasoning is wrong for a TRANSIENT refusal: the
    message never left the inbox, so marking it seen orphans it forever --
    the next round reports ``skipped_already_seen``, the mail sits in the
    inbox, and the watcher reports healthy. Only an APPLIED result spends a
    replay slot.
    """
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"),)),
        file_error=ProviderError("412"),
    )
    state = WatchState()

    watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))

    assert state.seen_message(None, "m1") is False


def test_a_round_with_a_failure_stores_no_cursor_so_the_failure_is_re_polled() -> None:
    """REWRITTEN CONTRACT (#158): a failure withholds the round's cursor.

    Not marking the message seen is only half the retry: the cursor is what
    decides whether the next round is shown the message at all. A committed
    cursor moves the stream past a message that never left the inbox, so the
    retry the replay guard now permits would never be offered one.
    """
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"), _message("m2")), cursor=FINAL_CURSOR),
        file_error=ProviderError("412"),
    )
    state = WatchState()

    summary = watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))

    assert summary.failed == 2
    assert summary.cursor_stored is False
    assert summary.state is watcher.RoundState.PAGED
    assert state.cursor_for(None) is None


def test_a_fully_successful_round_still_stores_its_cursor() -> None:
    """The withholding is scoped to failures; the happy path is unchanged."""
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR),
    )
    state = WatchState()

    summary = watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))

    assert summary.failed == 0
    assert summary.cursor_stored is True
    assert summary.state is watcher.RoundState.COMMITTED
    cursor = state.cursor_for(None)
    assert cursor is not None
    assert cursor.token == FINAL_CURSOR


class _ExplodingClassifier(CountingClassifier):
    """A classifier that raises a NON-provider exception for one named message.

    A local model can return a malformed payload, and the openai-compatible
    backend raises ``ClassifierBackendError`` -- neither is a ``ProviderError``.
    ``ValueError`` stands in for the whole family here precisely because it is
    unrelated to the provider hierarchy: catching it proves the boundary is
    ``Exception``, not a widened provider catch.
    """

    def __init__(self, explode_on: str) -> None:
        super().__init__()
        self.explode_on = explode_on

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        recorded = super().classify(message, folders)
        if message.id == self.explode_on:
            raise ValueError("malformed local-model response for 'Quarterly review notes'")
        return recorded


def test_a_classifier_that_raises_contains_the_failure_to_its_own_message() -> None:
    """#157: a non-ProviderError from ``classify`` must not kill the round.

    ``classify`` used to sit ABOVE the per-message try, so any classifier
    exception -- ``ClassifierBackendError``, or a ``TypeError``/``ValueError``
    off a malformed local-model response -- propagated out of ``poll_once``
    and abandoned every message behind it.
    """
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"), _message("m2")), cursor=FINAL_CURSOR),
    )
    classifier = _ExplodingClassifier("m1")

    summary = watcher.poll_once(_context(provider, classifier=classifier))

    assert classifier.seen_ids == ["m1", "m2"]
    assert provider.filed == [("m2", FOLDER)]
    assert (summary.failed, summary.applied) == (1, 1)
    rows = ledger.read_all()
    assert [(row.message_id, row.status) for row in rows] == [
        ("m1", ledger.Status.FAILED),
        ("m2", ledger.Status.APPLIED),
    ]
    assert rows[0].error_code == "ValueError"
    assert "Quarterly" not in rows[0].error_code


def test_a_ledger_write_failure_still_propagates_out_of_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE CARVE-OUT. Broadening the catch must not swallow the audit invariant.

    A mutation without its receipt breaks the record the kit is sold on, so an
    ``OSError`` from the ledger is the one failure the per-message boundary
    does not contain.
    """
    provider = FakeProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("ledger volume is read-only")

    monkeypatch.setattr(watcher.ledger, "append", explode)

    with pytest.raises(OSError):
        watcher.poll_once(_context(provider, classifier=CountingClassifier()))


def test_a_failing_poll_raises_rather_than_returning_a_quiet_summary() -> None:
    """A fetch that failed reported nothing -- there is nothing to summarise."""

    class BrokenProvider(FakeProvider):
        def poll_changes(
            self,
            cursor: str | None,
            *,
            page_token: str | None = None,
            container_id: str | None = None,
        ) -> ChangeSet:
            raise ProviderError("503")

    provider = BrokenProvider(ChangeSet())

    with pytest.raises(ProviderError):
        watcher.poll_once(_context(provider))


# ---------------------------------------------------------------------------
# THE cursor rule, and the paging loop that decides when it is settled
# ---------------------------------------------------------------------------


class _PagingProvider(FakeProvider):
    """A mailbox whose change feed is SCRIPTED AS A SEQUENCE, one entry per page.

    ``FakeProvider`` holds one ``ChangeSet`` and returns it for every call. That
    is the right fake for a single-page round and exactly the wrong one for a
    paged round: a feed that reports the same continuation handle forever has
    no last page, so a loop driven by it cannot terminate and a round that
    polls once looks correct against it. This subclass hands out the pages in
    order and records the ``page_token`` it was asked with, which is the
    evidence that the loop replays the handle the previous page reported rather
    than inventing one.
    """

    def __init__(self, pages: tuple[ChangeSet, ...], **kwargs: object) -> None:
        super().__init__(pages[0], **kwargs)  # type: ignore[arg-type]
        self.pages = pages
        self.page_tokens: list[str | None] = []

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        self.polled.append(cursor)
        self.page_tokens.append(page_token)
        return self.pages[len(self.page_tokens) - 1]


def test_a_round_whose_last_page_carries_no_cursor_stores_nothing() -> None:
    """The single silent-data-loss bug in the design, asserted directly.

    UPDATED WITH THE PAGING FIX, and the change is the ticket. This test used
    to hand a ``FakeProvider`` one mid-page ``ChangeSet`` and assert the whole
    round ended at ``PAGED`` -- which encoded the defect, because that fake
    returns the SAME page for every call and the round was polling exactly
    once. ``poll_once`` now drains to exhaustion, so the way to reach ``PAGED``
    is a feed that runs out of pages without ever offering a cursor. The rule
    under test is unchanged and is the one that matters: a position that was
    never final is never written. The messages ARE still processed -- re-reading
    a page is free, skipping one is not.
    """
    provider = _PagingProvider((
        ChangeSet(messages=(_message("m1"),), next_page_token=PAGE_TOKEN),
        ChangeSet(messages=(_message("m2"),)),
    ))
    state = WatchState()

    summary = watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))

    assert summary.cursor_stored is False
    assert summary.state is watcher.RoundState.PAGED
    assert state.cursor_for(None) is None
    assert provider.filed == [("m1", FOLDER), ("m2", FOLDER)]


def test_a_round_that_never_reaches_a_cursor_leaves_an_existing_one_as_it_was() -> None:
    """A round that ends unfinished must not move a position that was correct."""
    state = WatchState()
    state.record_cursor(None, "history-1000")
    provider = _PagingProvider((ChangeSet(next_page_token=PAGE_TOKEN), ChangeSet()))

    watcher.poll_once(_context(provider, state=state))

    cursor = state.cursor_for(None)
    assert cursor is not None
    assert cursor.token == "history-1000"  # ruff: ignore[hardcoded-password-string]


def test_a_two_page_round_processes_both_pages_and_stores_the_final_cursor() -> None:
    """A round runs to EXHAUSTION: pages remaining means fetch again, not stop.

    The defect this pins is a round that polled once and returned ``PAGED``.
    Nothing above it re-entered on that state, so page two was never fetched
    and the cursor never advanced -- the stream stalled at page one forever
    while every summary looked like an ordinary partial read.
    """
    provider = _PagingProvider((
        ChangeSet(messages=(_message("m1"),), next_page_token=PAGE_TOKEN),
        ChangeSet(messages=(_message("m2"),), cursor=FINAL_CURSOR),
    ))
    state = WatchState()
    classifier = CountingClassifier()

    summary = watcher.poll_once(_context(provider, state=state, classifier=classifier))

    assert provider.page_tokens == [None, PAGE_TOKEN]
    assert provider.polled == [None, None]
    assert classifier.seen_ids == ["m1", "m2"]
    assert provider.filed == [("m1", FOLDER), ("m2", FOLDER)]
    assert summary.state is watcher.RoundState.COMMITTED
    assert (summary.seen, summary.classified, summary.applied) == (2, 2, 2)
    assert summary.cursor_stored is True
    cursor = state.cursor_for(None)
    assert cursor is not None
    assert cursor.token == FINAL_CURSOR


def test_a_resync_on_a_later_page_keeps_the_counts_the_earlier_pages_earned() -> None:
    """The writes on page one HAPPENED. The summary must not claim otherwise.

    A resync demanded mid-round drops the cursor and ends the round, exactly as
    it does on the first poll -- but page one's messages were already classified,
    filed, and receipted, and reporting zeros for them would make the summary
    disagree with the ledger.
    """
    state = WatchState()
    state.record_cursor(None, "history-stale")
    provider = _PagingProvider((
        ChangeSet(messages=(_message("m1"),), next_page_token=PAGE_TOKEN),
        ChangeSet(messages=(_message("m2"),), resync_required=True),
    ))
    classifier = CountingClassifier()

    summary = watcher.poll_once(_context(provider, state=state, classifier=classifier))

    assert classifier.seen_ids == ["m1"]
    assert provider.filed == [("m1", FOLDER)]
    assert summary.state is watcher.RoundState.RESYNCED
    assert summary.resynced is True
    assert summary.cursor_stored is False
    assert (summary.seen, summary.classified, summary.applied) == (1, 1, 1)
    assert state.cursor_for(None) is None
    assert state.generation_for(None) == 1


def test_a_message_in_an_unenumerated_container_is_not_classified() -> None:
    """FAILS CLOSED on an id the provider did not enumerate.

    An unknown id used to resolve to itself as the folder NAME with no
    well-known tag and was then answered by the scope rules -- so a message
    sitting in an explicitly excluded folder was admitted whenever the
    container listing did not name that folder's id. A listing that is stale,
    truncated, or filtered is an ordinary provider condition, and the correct
    answer to "I do not know where this message is" is not permission.
    """
    provider = FakeProvider(
        ChangeSet(messages=(_message("ghost", containers=("Label_Unknown",)),), cursor=FINAL_CURSOR)
    )
    classifier = CountingClassifier()

    summary = watcher.poll_once(_context(provider, classifier=classifier))

    assert classifier.seen_ids == []
    assert provider.filed == []
    assert summary.skipped_unenumerated_container == 1
    assert summary.skipped_out_of_scope == 0, (
        "the SCOPE refused nothing here -- the LISTING did (#198), and the two "
        "counts drive different WARNs and different operator fixes"
    )
    assert summary.classified == 0


def test_the_round_sums_skipped_missing_across_every_page() -> None:
    """A 404 shed on page one counts as much as one shed on the last page.

    Reading the field off the final ``ChangeSet`` would report zero for the
    ordinary case, because the page that carries the cursor is usually the
    quiet one.
    """
    provider = _PagingProvider((
        ChangeSet(skipped_missing=2, next_page_token=PAGE_TOKEN),
        ChangeSet(skipped_missing=1, cursor=FINAL_CURSOR),
    ))

    summary = watcher.poll_once(_context(provider))

    assert summary.skipped_missing == 3
    assert summary.seen == 0, "a shed id never became a message, so no gate saw it"


def test_the_changeset_model_refuses_a_cursor_while_pages_remain() -> None:
    """The other half of the two-mechanism rule: an adapter cannot produce one."""
    with pytest.raises(ValueError, match="pages remaining"):
        ChangeSet(cursor=FINAL_CURSOR, next_page_token=PAGE_TOKEN)


def test_no_cursor_is_stored_when_every_page_is_empty_and_none_is_final() -> None:
    """A quiet feed that never offers a cursor still writes nothing."""
    provider = _PagingProvider((ChangeSet(next_page_token=PAGE_TOKEN), ChangeSet()))
    state = WatchState()

    summary = watcher.poll_once(_context(provider, state=state))

    assert summary.cursor_stored is False
    assert state.cursor_for(None) is None


# ---------------------------------------------------------------------------
# The prohibitions, asserted rather than promised
# ---------------------------------------------------------------------------


def test_the_module_calls_no_flagging_method() -> None:
    """Importance remains outside the watch path.

    CHANGED IN SPRINT 4, DELIBERATELY AND VISIBLY. This test used to assert
    ``FORBIDDEN_METHODS == {"create_draft", "set_important"}``. ``create_draft``
    left that tuple in the same change that landed its idempotency gate
    (``watcher._already_drafted`` over ``ledger.thread_has_draft``), and this
    assertion moved with it rather than being relaxed afterwards to make a red
    run green. ``set_important`` is untouched: it is a different product
    decision and nothing in Sprint 4 argues about it.

    Asserted against the SOURCE, not against one round's behaviour: a test that
    only drove the happy path would miss a call on a branch it did not take.
    """
    assert set(watcher.FORBIDDEN_METHODS) == {"set_important"}
    source = _watcher_source()
    for method in watcher.FORBIDDEN_METHODS:
        assert f".{method}(" not in source, f"watcher must never call {method}"


def test_the_writes_the_module_performs_are_filing_and_drafting() -> None:
    """The write surface is exactly two methods, and BOTH are exercised in source.

    A presence assertion per method, not just the absence sweep above: a
    constant naming a permitted method the module never calls would pass an
    absence-only check vacuously.
    """
    assert set(watcher.WRITE_METHODS) == {"file_message", "create_draft"}
    assert not set(watcher.WRITE_METHODS) & set(watcher.FORBIDDEN_METHODS)
    source = _watcher_source()
    for method in watcher.WRITE_METHODS:
        assert f".{method}(" in source
    assert ".tag_message(" not in source


def test_no_write_outside_the_declared_surface_appears_in_source() -> None:
    """Nothing mutating is called that ``WRITE_METHODS`` does not name.

    Enumerated against the PROVIDER INTERFACE rather than a hand-written list,
    so a mutating method added to ``MailProvider`` later is covered here on the
    day it appears instead of whenever somebody remembers to extend a literal.
    """
    mutators = {
        name
        for name in vars(MailProvider)
        if not name.startswith("_")
        and any(
            verb in name for verb in ("tag", "delete", "remove", "move", "file", "set_", "draft")
        )
    }
    source = _watcher_source()
    for method in mutators - set(watcher.WRITE_METHODS):
        assert f".{method}(" not in source, f"watcher must never call {method}"


def test_no_send_or_delete_method_exists_to_call() -> None:
    """Absence, not a guard: the interface has no such capability at all."""
    for banned in ("send", "send_message", "delete", "trash", "forward", "remove_label"):
        assert not hasattr(MailProvider, banned)


# The RoundSummary/PlannedFiling shape guards (counts-and-identifiers only,
# frozen, no extra fields, nothing of the message read) live in
# test_watcher_summary_shape.py (2026-09-01 split -- 500-line ceiling).


# test_the_module_neither_sleeps_nor_schedules and its AST scan live in
# test_watcher_import_scan.py (#81 split -- this module's 500-line ceiling).


def _watcher_source() -> str:
    from pathlib import Path

    return Path(watcher.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The receipt must name the message that EXISTS after the write (lane RD)
# ---------------------------------------------------------------------------
#
# Graph's /move is copy-then-delete: the id handed to ``file_message`` is dead
# the instant the move lands, and the destination holds a NEW entity with a NEW
# id. ``MailProvider.file_message`` is therefore not a fire-and-forget call --
# its RETURN VALUE is the adapter telling the caller what the write produced,
# and the Outlook adapter already carries the post-move id there
# (``providers/outlook.py``: ``FiledMessage(..., message_id=new_id)``).
#
# The MCP tool boundary already reads it (``server.py``:
# ``receipt_id = getattr(applied, "message_id", key)``). The watcher does not:
# it discards the return value and receipts the pre-move id. That asymmetry is
# not cosmetic -- a ledger row naming a dead id cannot be resolved back to a
# message, so the audit trail the kit is sold on points at nothing and
# ``reset restore`` silently undoes zero of it (proven on a live tenant
# 2026-08-19, 54/54 unrestorable).
#
# ``getattr`` with the pre-move id as the fallback is the seam that keeps this
# provider-agnostic: Gmail's ``file_message`` returns a plain ``str`` label
# name with no ``message_id`` attribute, so Gmail keeps receipting exactly the
# id it receipts today. Gmail ids are stable and there is nothing to correct.
#
# ``_FiledIdProvider`` and ``_ReassignedId`` moved to ``_watch_harness`` on
# 2026-08-24 when ``test_watcher_destination_window.py`` (the #173 split)
# became their second consumer.


def test_the_receipt_records_the_id_the_move_returned_not_the_dead_one() -> None:
    """A provider that reassigns the id on write is receipted at its NEW id.

    The pre-move id no longer resolves to anything once the write lands, so a
    row carrying it is a receipt for a message that cannot be found again.
    """
    provider = _FiledIdProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    summary = watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    assert summary.applied == 1
    assert provider.filed == [("m1", FOLDER)]
    row = ledger.read_all()[0]
    assert row.status is ledger.Status.APPLIED
    assert row.message_id == "m1-moved"


def test_a_provider_that_returns_a_plain_name_is_receipted_at_the_id_it_was_given() -> None:
    """The Gmail shape is unchanged: no reassignment reported, no correction made.

    ``GmailProvider.file_message`` returns the label name as a bare ``str``.
    Gmail's ids are stable, so the id passed in IS the id that still exists,
    and reading a post-write id must degrade to exactly today's behaviour
    rather than inventing one.
    """
    provider = FakeProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    summary = watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    assert summary.applied == 1
    assert [row.message_id for row in ledger.read_all()] == ["m1"]


def test_a_refused_filing_is_still_receipted_at_the_id_it_was_asked_about() -> None:
    """No write landed, so there is no new id; the FAILED row names the request.

    Guards the fix's blast radius: reading the return value must not change the
    failure arm, which has no return value to read.
    """
    provider = _FiledIdProvider(
        ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR),
        file_error=ProviderError("refused"),
    )

    summary = watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    assert summary.failed == 1
    row = ledger.read_all()[0]
    assert row.status is ledger.Status.FAILED
    assert row.message_id == "m1"


# ---------------------------------------------------------------------------
# The Gmail flag-label defect, proven at the ROUND rather than at the adapter
# ---------------------------------------------------------------------------


def test_an_unread_important_gmail_inbox_message_is_classified_under_the_default_scope() -> None:
    """THE live defect (head-verified against a real mailbox, 2026-08-22).

    The adapter's own test pins the decoded tuple; this one pins the
    CONSEQUENCE, because the tuple is only interesting for what the scope gate
    does with it. ``MailboxScope`` defaults to ``inbox_only`` and
    ``covers_all`` is any-exclusion-wins, so one uncovered entry refuses the
    whole message -- and ``UNREAD`` rides on essentially every message the
    watcher exists to read. Reporting it as a container therefore emptied the
    watcher: every inbox message came back ``skipped_out_of_scope``, and no
    adapter-level assertion about a tuple says that out loud.

    The message is decoded by ``_to_message`` from a REAL Gmail payload shape
    rather than constructed, so the tuple under test is the one the adapter
    actually produces; a hand-built ``Message`` here would pass whatever the
    adapter did.

    The evidence is the SUMMARY, not the classifier spy. ``IMPORTANT`` makes
    ``is_important`` true, and the deterministic ``_is_flagged_important`` rule
    settles the message before any model is consulted -- so an empty
    ``seen_ids`` here means "a rule decided it", not "scope refused it". What
    distinguishes the two is ``skipped_out_of_scope``, which is the counter the
    defect moved.
    """
    from inbox_zero_connector.providers.gmail.messages import _to_message

    decoded = _to_message({
        "id": "gm1",
        "threadId": "t-gm1",
        "snippet": "attached",
        "labelIds": ["INBOX", "UNREAD", "IMPORTANT"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Sender Name <sender@example.com>"},
                {"name": "Subject", "value": "Quarterly review notes"},
                {"name": "Date", "value": "Sat, 22 Aug 2026 10:00:00 -0400"},
            ]
        },
    })
    provider = FakeProvider(
        ChangeSet(messages=(decoded,), cursor=FINAL_CURSOR),
        containers=(
            Container(
                id="INBOX",
                name="Inbox",
                well_known="inbox",
                parent_id=None,
                exclusive=False,
            ),
        ),
    )
    summary = watcher.poll_once(
        _context(provider, scope=MailboxScope(), classifier=CountingClassifier())
    )

    assert summary.skipped_out_of_scope == 0
    assert summary.classified == 1
    assert summary.applied == 1
    assert provider.filed == [("gm1", "Business-Action")]


# ---------------------------------------------------------------------------
# The refile loop (#161a) -- a move that mints a new id must spend TWO slots
# ---------------------------------------------------------------------------


def test_a_move_that_mints_a_new_id_marks_both_ids_seen() -> None:
    """THE #161a repro: the destination folder is its own delta stream.

    Graph's ``/move`` is copy-then-delete, so the filed message re-enters the
    watcher from its DESTINATION folder carrying the post-move id. Marking only
    the pre-move id leaves that arrival unguarded, and the round files it again
    -- ``AAA`` becomes ``AAA-MOVED``, then ``AAA-MOVED-MOVED``, forever.
    """
    provider = _FiledIdProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    state = WatchState()

    watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))

    assert state.seen_message(None, "m1") is True
    assert state.seen_message(None, "m1-moved") is True


def test_the_post_move_id_re_entering_the_stream_is_skipped_as_already_seen() -> None:
    """The consequence, driven end to end: the second round files nothing."""
    state = WatchState()
    first = _FiledIdProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    watcher.poll_once(_context(first, state=state, classifier=CountingClassifier()))

    second = _FiledIdProvider(ChangeSet(messages=(_message("m1-moved"),), cursor="history-2002"))
    summary = watcher.poll_once(_context(second, state=state, classifier=CountingClassifier()))

    assert summary.skipped_already_seen == 1
    assert summary.applied == 0
    assert second.filed == []


class _NoIdMoveProvider(FakeProvider):
    """An honest Graph move receipt: the write landed, the new id is unknown."""

    def file_message(self, message_id: str, folder: str) -> str:
        self.filed.append((message_id, folder))
        return _ReassignedId(folder, message_id=None)


def test_a_move_receipt_with_no_post_move_id_marks_only_the_original() -> None:
    """``FiledMessage.message_id`` is ``None`` when Graph reported no new id."""
    provider = _NoIdMoveProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))
    state = WatchState()

    summary = watcher.poll_once(_context(provider, state=state, classifier=CountingClassifier()))

    assert summary.applied == 1
    assert state.seen_message(None, "m1") is True
    assert state.seen[MAILBOX_WIDE_KEY] == ["m1"]


def test_a_move_receipt_with_no_post_move_id_records_the_unknown_id_not_the_dead_one() -> None:
    """The ledger row says ``MESSAGE_ID_UNKNOWN``, never the id the move deleted.

    Graph's ``/move`` is copy-then-delete, so a ``204 No Content`` means the
    write landed and the pre-move id is DEAD. Falling back to it writes a row
    that looks exactly like a correct one and resolves to nothing -- the
    dishonesty ``ledger.MESSAGE_ID_UNKNOWN`` exists to make visible (#169).
    """
    provider = _NoIdMoveProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    summary = watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    assert summary.applied == 1
    row = ledger.read_all()[0]
    assert row.status is ledger.Status.APPLIED
    assert row.message_id == ledger.MESSAGE_ID_UNKNOWN


def test_a_move_receipt_with_no_post_move_id_traces_the_unknown_id() -> None:
    """The decision trace carries the same honest id as the ledger row.

    The two rows are read together when auditing a filing, so a trace naming
    the dead id would reintroduce the wrong answer beside a right one.
    """
    provider = _NoIdMoveProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    watcher.poll_once(_context(provider, classifier=CountingClassifier()))
