"""Gmail read and label operations -- the everyday calls, and what they never do.

Split out of ``test_gmail_provider.py`` on 2026-08-19 (500-code-line ceiling).
Owns the adapter's routine surface: authenticate, ``list_inbox``,
``get_thread``, ``file_message``, ``tag_message``, ``set_important``, and the
requested OAuth scope set.

The load-bearing test here is the last one: no call the adapter can make ever
reaches trash, delete, or ``drafts.send``. Filing is ADDITIVE -- a label is
added and INBOX removed -- and tagging must never move a message. Those are the
promises that let a client point this at a live mailbox, so they are asserted
against the recorded call log rather than inferred from the code.
"""

from __future__ import annotations

from typing import Any

import pytest

from _gmail_harness import (
    FakeService,
    GmailProvider,
    _query,
)
from _gmail_harness import (
    drafting_provider as drafting_provider,
)
from _gmail_harness import (
    provider as provider,
)
from _gmail_harness import (
    store as store,
)
from inbox_zero_connector.providers.base import Message, ProviderError


def test_authenticate_returns_the_account_address(provider: GmailProvider) -> None:
    assert provider.authenticate(interactive=False) == "owner@example.com"


def test_list_inbox_returns_parsed_headers(provider: GmailProvider) -> None:
    messages = provider.list_inbox(limit=10)
    assert [m.id for m in messages] == ["m1", "m2"]
    assert messages[0].subject == "First"
    assert messages[0].sender == "sender@example.com"
    assert messages[0].is_unread is True


def test_list_inbox_projects_the_internet_message_id(provider: GmailProvider) -> None:
    messages = provider.list_inbox(limit=10)
    assert messages[0].internet_message_id == "<m1@mail.example>"
    service: FakeService = provider._service  # type: ignore[assignment]
    call = next(kwargs for name, kwargs in service.log if name == "messages.get")
    assert "Message-Id" in call["metadataHeaders"]


def test_message_without_an_internet_message_id_defaults_empty() -> None:
    message = Message(
        id="m1",
        thread_id="t_m1",
        sender="sender@example.com",
        subject="First",
        received="Sun, 17 Aug 2026 10:00:00 -0400",
    )
    assert message.internet_message_id == ""


def test_list_inbox_unread_filter_uses_the_query(provider: GmailProvider) -> None:
    provider.list_inbox(unread_only=True)
    service: FakeService = provider._service  # type: ignore[assignment]
    call = next(kwargs for name, kwargs in service.log if name == "messages.list")
    assert call["q"] == "in:inbox is:unread"


def test_list_inbox_clamps_an_absurd_limit(provider: GmailProvider) -> None:
    provider.list_inbox(limit=100_000)
    service: FakeService = provider._service  # type: ignore[assignment]
    call = next(kwargs for name, kwargs in service.log if name == "messages.list")
    assert call["maxResults"] == 200


def test_get_thread_decodes_bodies(provider: GmailProvider) -> None:
    thread = provider.get_thread("t_m1")
    assert thread.subject == "First"
    assert thread.bodies == ["body of m1"]


def test_file_message_adds_the_label_and_removes_inbox(provider: GmailProvider) -> None:
    applied = provider.file_message("m1", "Action")
    assert applied == "Action"
    service: FakeService = provider._service  # type: ignore[assignment]
    modify = next(kwargs for name, kwargs in service.log if name == "messages.modify")
    assert modify["body"]["addLabelIds"] == ["LBL_EXIST"]
    assert modify["body"]["removeLabelIds"] == ["INBOX"]


def test_file_message_never_uses_threads_modify(provider: GmailProvider) -> None:
    """threads.modify does not accept gmail.modify.restricted -- see scopes.py.

    Filing must go message-by-message through messages.modify or it would fail
    on the granted token.
    """
    provider.file_message("m1", "Action")
    service: FakeService = provider._service  # type: ignore[assignment]
    assert not any(name == "threads.modify" for name, _ in service.log)


def test_file_message_creates_a_label_only_when_missing(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    provider.file_message("m1", "Receipts")
    service: FakeService = provider._service  # type: ignore[assignment]
    created = [kwargs for name, kwargs in service.log if name == "labels.create"]
    assert len(created) == 1
    assert created[0]["body"]["name"] == "Receipts"


def test_label_lookup_is_cached_across_calls(provider: GmailProvider) -> None:
    provider.file_message("m1", "Action")
    provider.file_message("m2", "Action")
    service: FakeService = provider._service  # type: ignore[assignment]
    assert sum(1 for name, _ in service.log if name == "labels.list") == 1


def test_tag_message_adds_a_label_and_never_removes_one(provider: GmailProvider) -> None:
    """The classify-without-moving primitive: addLabelIds only.

    ``removeLabelIds`` must be ABSENT from the body, not merely empty. Gmail's
    modify performs the union server-side, so there is nothing to read and
    nothing to put back -- the whole reason the interface expresses intent to
    ADD rather than a full label set.
    """
    applied = provider.tag_message("m1", "Action")
    assert applied == "Action"
    service: FakeService = provider._service  # type: ignore[assignment]
    modify = next(kwargs for name, kwargs in service.log if name == "messages.modify")
    assert modify["body"]["addLabelIds"] == ["LBL_EXIST"]
    assert "removeLabelIds" not in modify["body"]


def test_tag_message_leaves_an_existing_label_in_place(provider: GmailProvider) -> None:
    """A message already carrying labels keeps every one of them.

    Nothing in the request names a label to drop, so the labels the message
    already has -- INBOX included, which is what distinguishes tagging from
    filing -- cannot be affected by it.
    """
    provider.tag_message("m1", "Action")
    service: FakeService = provider._service  # type: ignore[assignment]
    modify = next(kwargs for name, kwargs in service.log if name == "messages.modify")
    assert list(modify["body"]) == ["addLabelIds"]
    assert "INBOX" not in modify["body"]["addLabelIds"]


def test_tag_message_never_moves_the_message(provider: GmailProvider) -> None:
    """Placement is untouched: no INBOX removal, no threads.modify, no move."""
    provider.tag_message("m1", "Action")
    service: FakeService = provider._service  # type: ignore[assignment]
    assert not any(name == "threads.modify" for name, _ in service.log)
    modify = next(kwargs for name, kwargs in service.log if name == "messages.modify")
    assert "INBOX" not in str(modify["body"])


def test_tag_message_refuses_a_comma_before_any_api_call(provider: GmailProvider) -> None:
    """A delimiter-bearing name is rejected BEFORE a request, and before a label
    is created as a side effect of resolving a name headed for refusal."""
    from inbox_zero_connector.services.triage.taxonomy import LabelNameError

    with pytest.raises(LabelNameError, match="forbidden character"):
        provider.tag_message("m1", "Business, Action")
    service: FakeService = provider._service  # type: ignore[assignment]
    assert service.log == []


def test_tag_message_sends_the_stripped_name_not_the_argument(
    provider: GmailProvider, store: dict[str, Any]
) -> None:
    """The strip is a CORRECTION and the returned value is what must be used.

    A trailing space is invisible in every UI, so discarding validate_label_name's
    return would create a second label that looks identical to the real one. The
    created label and the returned name must both be the stripped form.
    """
    applied = provider.tag_message("m1", "Receipts ")
    assert applied == "Receipts"
    service: FakeService = provider._service  # type: ignore[assignment]
    created = [kwargs for name, kwargs in service.log if name == "labels.create"]
    assert created[0]["body"]["name"] == "Receipts"


def test_set_important_adds_and_removes_the_flag(provider: GmailProvider) -> None:
    assert provider.set_important("m1", True) == "IMPORTANT"
    provider.set_important("m1", False)
    service: FakeService = provider._service  # type: ignore[assignment]
    modifies = [kwargs for name, kwargs in service.log if name == "messages.modify"]
    assert modifies[0]["body"] == {"addLabelIds": ["IMPORTANT"]}
    assert modifies[1]["body"] == {"removeLabelIds": ["IMPORTANT"]}


def test_no_call_ever_touches_trash_or_delete(drafting_provider: GmailProvider) -> None:
    """Invariant 3: nothing destructive. Exercise every mutating path."""
    from inbox_zero_connector.providers.base import SentSelection

    drafting_provider.list_inbox()
    drafting_provider.get_thread("t_m1")
    drafting_provider.read_sent_sample(SentSelection(after="2026-01-01"))
    drafting_provider.file_message("m1", "Action")
    drafting_provider.set_important("m1", True)
    drafting_provider.create_draft("t_m1", "Thanks.")
    service: FakeService = drafting_provider._service  # type: ignore[assignment]
    called = {name for name, _ in service.log}
    assert called.isdisjoint({
        "messages.trash",
        "messages.delete",
        "messages.batchDelete",
        "drafts.send",
    })
    for _, kwargs in service.log:
        assert "TRASH" not in str(kwargs.get("body", {}))


# ---------------------------------------------------------------------------
# Stage 2: scope selection
# ---------------------------------------------------------------------------


def test_the_adapter_requests_one_interactive_scope_when_drafts_are_off(
    provider: GmailProvider,
) -> None:
    assert not any("compose" in scope for scope in provider.scopes)
    assert len(provider.scopes) == 1


def test_the_adapter_scope_request_is_unchanged_by_drafts_interactively(
    drafting_provider: GmailProvider,
) -> None:
    """gmail.modify already covers drafts.create; interactive drafts add no
    scope (and never add compose -- that is the DWD conditional)."""
    assert list(drafting_provider.scopes) == ["https://www.googleapis.com/auth/gmail.modify"]


# ---------------------------------------------------------------------------
# Stage 2: read_sent_sample
# ---------------------------------------------------------------------------


def test_read_sent_sample_refuses_an_empty_selection(provider: GmailProvider) -> None:
    """Defense in depth: the tool layer refuses it and the adapter refuses it again."""
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError, match="explicit selection"):
        provider.read_sent_sample(SentSelection())
    service: FakeService = provider._service  # type: ignore[assignment]
    assert service.log == []


def test_read_sent_sample_query_is_always_scoped_to_sent(provider: GmailProvider) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(SentSelection(after="2026-01-01"))
    assert _query(provider).startswith("in:sent")


def test_selection_labels_and_dates_become_query_clauses(provider: GmailProvider) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(
        SentSelection(labels=("Clients",), after="2026-01-01", before="2026-06-01")
    )
    query = _query(provider)
    assert 'label:"Clients"' in query
    assert "after:2026/01/01" in query
    assert "before:2026/06/01" in query


def test_exclusions_become_negative_clauses(provider: GmailProvider) -> None:
    """The exclusion is enforced on GOOGLE's side: excluded mail is never fetched."""
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(SentSelection(after="2026-01-01", exclude_labels=("Legal",)))
    assert '-label:"Legal"' in _query(provider)


def test_read_sent_sample_returns_bodies_and_recipients(provider: GmailProvider) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    messages = provider.read_sent_sample(SentSelection(after="2026-01-01"))
    assert len(messages) == 1
    assert messages[0].body == "body of s1"
    assert "owner@example.com" in messages[0].recipients
    assert "cc@other.example" in messages[0].recipients


def test_explicit_message_ids_skip_the_search(provider: GmailProvider) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    messages = provider.read_sent_sample(SentSelection(message_ids=("s1",)))
    assert [m.id for m in messages] == ["s1"]
    service: FakeService = provider._service  # type: ignore[assignment]
    assert not any(name == "messages.list" for name, _ in service.log)


def test_exclusions_still_apply_on_the_message_id_path(provider: GmailProvider) -> None:
    """The path that bypasses the query must not bypass the owner's boundary."""
    from inbox_zero_connector.providers.base import SentSelection

    messages = provider.read_sent_sample(
        SentSelection(message_ids=("s1", "s2"), exclude_labels=("Label_Legal",))
    )
    assert [m.id for m in messages] == ["s1"]


def test_an_inbox_id_is_refused_by_read_sent_sample(provider: GmailProvider) -> None:
    """The sent-only contract holds on the hand-picked-id path too.

    ``m1`` is an INBOX message in the fixture. Handing its id to a sent-only
    tool must be REFUSED, not filtered: the tool's whole promise is that what
    comes back is mail the owner WROTE, and quietly returning nothing would
    hide that the caller reached outside that set.
    """
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError, match="not sent mail"):
        provider.read_sent_sample(SentSelection(message_ids=("m1",)))


def test_the_sent_refusal_names_the_offending_id(provider: GmailProvider) -> None:
    """A refusal a caller cannot act on is a worse bug than the one it caught."""
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError, match="'m1'"):
        provider.read_sent_sample(SentSelection(message_ids=("s1", "m1")))


def test_a_genuine_sent_id_still_reads(provider: GmailProvider) -> None:
    """The guard refuses non-sent mail without narrowing the legitimate path."""
    from inbox_zero_connector.providers.base import SentSelection

    messages = provider.read_sent_sample(SentSelection(message_ids=("s1",)))
    assert [m.id for m in messages] == ["s1"]
    assert messages[0].body == "body of s1"


def test_the_sent_check_costs_no_extra_api_calls(provider: GmailProvider) -> None:
    """The label that proves SENT rides along on the fetch that was happening anyway.

    A guard implemented as a second lookup per message would double the API
    cost of every voice-profile read, so the receipt is asserted here rather
    than left to review.
    """
    from inbox_zero_connector.providers.base import SentSelection

    with pytest.raises(ProviderError, match="not sent mail"):
        provider.read_sent_sample(SentSelection(message_ids=("m1",)))
    service: FakeService = provider._service  # type: ignore[assignment]
    assert [name for name, _ in service.log] == ["messages.get"]


def test_the_sent_guard_also_covers_the_search_path(provider: GmailProvider) -> None:
    """Every path into read_sent_sample, not just the one that had the hole.

    ``in:sent`` makes the search path correct today, so this asserts the guard
    is placed where BOTH paths pass through it -- a future search path that
    stopped being unconditional would be caught rather than trusted.
    """
    from inbox_zero_connector.providers.base import SentSelection

    store: dict[str, Any] = provider._service._store  # type: ignore[attr-defined]
    store["sent_list"] = {"messages": [{"id": "m1"}]}
    with pytest.raises(ProviderError, match="not sent mail"):
        provider.read_sent_sample(SentSelection(after="2026-01-01"))


def test_read_sent_sample_clamps_the_limit(provider: GmailProvider) -> None:
    from inbox_zero_connector.providers.base import SentSelection

    provider.read_sent_sample(SentSelection(after="2026-01-01", limit=100_000))
    service: FakeService = provider._service  # type: ignore[assignment]
    call = next(kwargs for name, kwargs in service.log if name == "messages.list")
    assert call["maxResults"] == 200
