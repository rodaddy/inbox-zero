"""Gmail draft composition: who the reply is addressed to, and what it carries.

Split out of ``test_gmail_provider.py`` on 2026-08-19 (500-code-line ceiling).
Owns the ``create_draft`` path end to end, which is the only place this adapter
WRITES anything a human will see, so it is grouped away from the read paths.

Three concerns, all about the same artifact:

* ``create_draft`` itself -- refused unless drafts are enabled, bound to the
  existing thread, threading headers set, no double ``Re:`` prefix.
* The owner is stripped from the draft's Cc, and the recipients the tool
  REPORTS must match the ones the draft actually carries -- a mismatch means the
  operator approves one thing and sends another.
* Hostile inbound headers are copied forward from the message being replied to,
  so a CRLF in a subject or a References header must be folded into one line
  rather than injecting a header of the attacker's choosing.
"""

from __future__ import annotations

from typing import Any

import pytest

from _gmail_harness import (
    FakeDrafts,
    FakeService,
    GmailProvider,
    _draft_mime,
    _message,
    _owner_cc_store,
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
from inbox_zero_connector.providers.base import ProviderError

# ---------------------------------------------------------------------------
# Stage 2: create_draft
# ---------------------------------------------------------------------------


def test_create_draft_is_refused_when_drafts_are_disabled(provider: GmailProvider) -> None:
    """The config flag gates the adapter too, not just the auth request."""

    with pytest.raises(ProviderError, match="drafts are disabled"):
        provider.create_draft("t_m1", "hi")
    service: FakeService = provider._service  # type: ignore[assignment]
    assert service.log == []


def test_create_self_draft_carries_the_subject_and_body_and_no_recipients(
    drafting_provider: GmailProvider,
) -> None:
    """The digest draft is addressed to NOBODY (#65, D10/D20).

    The MIME is read back rather than the arguments echoed, because the header
    the owner opens is what matters: a To or Cc line here would mean the digest
    path can address mail, which is the one thing this operation must not do.
    """
    draft_id = drafting_provider.create_self_draft("Inbox Zero digest 2026-09-01", "3 filed.")

    assert draft_id == "DRAFT_1"
    mime = _draft_mime(drafting_provider)
    assert "Subject: Inbox Zero digest 2026-09-01" in mime
    assert "3 filed." in mime
    assert "To:" not in mime
    assert "Cc:" not in mime
    assert "Bcc:" not in mime


def test_create_self_draft_is_not_bound_to_any_thread(
    drafting_provider: GmailProvider,
) -> None:
    """No ``threadId``: the digest is a new message, not a reply to anyone."""
    drafting_provider.create_self_draft("Inbox Zero digest 2026-09-01", "3 filed.")

    service: FakeService = drafting_provider._service  # type: ignore[assignment]
    call = next(kwargs for name, kwargs in service.log if name == "drafts.create")
    assert "threadId" not in call["body"]["message"]


def test_create_self_draft_is_refused_when_drafts_are_disabled(provider: GmailProvider) -> None:
    """Same posture gate as ``create_draft``, and it refuses before any call."""

    with pytest.raises(ProviderError, match="drafts are disabled"):
        provider.create_self_draft("Inbox Zero digest 2026-09-01", "3 filed.")
    service: FakeService = provider._service  # type: ignore[assignment]
    assert service.log == []


def test_create_draft_addresses_the_reply_from_the_thread(
    drafting_provider: GmailProvider,
) -> None:
    result = drafting_provider.create_draft("t_m1", "Thanks.")
    assert result.draft_id == "DRAFT_1"
    mime = _draft_mime(drafting_provider)
    assert "To: sender@example.com" in mime
    assert "Cc: cc@other.example" in mime


def test_create_draft_binds_the_draft_to_the_existing_thread(
    drafting_provider: GmailProvider,
) -> None:
    """Without threadId the draft is a NEW message, which is a wider capability."""
    drafting_provider.create_draft("t_m1", "Thanks.")
    service: FakeService = drafting_provider._service  # type: ignore[assignment]
    call = next(kwargs for name, kwargs in service.log if name == "drafts.create")
    assert call["body"]["message"]["threadId"] == "t_m1"


def test_create_draft_sets_the_threading_headers(drafting_provider: GmailProvider) -> None:
    mime = ""
    drafting_provider.create_draft("t_m1", "Thanks.")
    mime = _draft_mime(drafting_provider)
    assert "In-Reply-To: <m1@mail.example>" in mime
    assert "<earlier@mail.example>" in mime
    assert "Subject: Re: First" in mime


def test_create_draft_does_not_double_prefix_a_re_subject(
    store: dict[str, Any],
) -> None:
    store["thread"] = {
        "id": "t_m1",
        "messages": [_message("m1", "Re: First")],
    }
    provider = GmailProvider(service=FakeService(store), drafts_enabled=True)
    provider.create_draft("t_m1", "Thanks.")
    assert "Subject: Re: First" in _draft_mime(provider)
    assert "Re: Re:" not in _draft_mime(provider)


def test_reply_to_wins_over_from(store: dict[str, Any]) -> None:
    """What a mail client does, and the sender explicitly asked for."""
    store["thread"] = {
        "id": "t_m1",
        "messages": [
            _message(
                "m1",
                "First",
                extra_headers=[{"name": "Reply-To", "value": "desk@example.org"}],
            )
        ],
    }
    provider = GmailProvider(service=FakeService(store), drafts_enabled=True)
    provider.create_draft("t_m1", "Thanks.")
    mime = _draft_mime(provider)
    assert "To: desk@example.org" in mime
    assert "To: sender@example.com" not in mime


def test_create_draft_refuses_a_thread_that_does_not_exist(store: dict[str, Any]) -> None:

    store["thread"] = {"id": "t_missing", "messages": []}
    provider = GmailProvider(service=FakeService(store), drafts_enabled=True)
    with pytest.raises(ProviderError, match="nothing to reply to"):
        provider.create_draft("t_missing", "hi")


def test_create_draft_signature_offers_no_recipient_argument() -> None:
    import inspect

    assert set(inspect.signature(GmailProvider.create_draft).parameters) == {
        "self",
        "thread_id",
        "body",
    }


def test_the_fake_drafts_resource_has_no_send_method() -> None:
    """The fake must not be able to do what the product cannot.

    A mock that offered ``send`` would let a regression -- a real send call
    appearing in the adapter -- pass this suite green.
    """
    assert hasattr(FakeDrafts, "create")
    assert not hasattr(FakeDrafts, "send")


def test_get_thread_exposes_participants_and_labels(provider: GmailProvider) -> None:
    thread = provider.get_thread("t_m1")
    assert "sender@example.com" in thread.participants
    assert "owner@example.com" in thread.participants
    assert "cc@other.example" in thread.participants
    assert "INBOX" in thread.labels


# ---------------------------------------------------------------------------
# The draft the owner opens must match the recipients the tool reported
# ---------------------------------------------------------------------------


def test_the_owner_is_stripped_from_the_draft_cc(store: dict[str, Any]) -> None:
    """F11. The tool layer reports recipients minus the owner, but the MIME
    headers were built from the thread unfiltered -- so the draft the owner
    actually opened had them on its own Cc, disagreeing with what the AI was
    told the draft said. The header is what gets sent; it is the one that has
    to be right.
    """
    provider = GmailProvider(service=FakeService(_owner_cc_store(store)), drafts_enabled=True)
    provider.create_draft("t_m1", "Thanks.")
    mime = _draft_mime(provider)
    assert "owner@example.com" not in mime
    assert "Cc: cc@other.example" in mime


def test_the_reported_recipients_exclude_the_owner(store: dict[str, Any]) -> None:
    """F11. DraftResult.recipients is the receipt for what the draft addresses."""
    provider = GmailProvider(service=FakeService(_owner_cc_store(store)), drafts_enabled=True)
    result = provider.create_draft("t_m1", "Thanks.")
    assert "owner@example.com" not in result.recipients
    assert result.recipients == ["sender@example.com", "cc@other.example"]


def test_a_reply_addressed_only_to_the_owner_is_refused(store: dict[str, Any]) -> None:
    """F11 + F12. Stripping the owner can empty the To line. A draft with a
    bare ``To:`` is not a narrower draft, it is a malformed one -- refuse it
    rather than hand Gmail a message with nowhere to go.
    """

    store["thread"] = {
        "id": "t_m1",
        "messages": [
            {
                "id": "m1",
                "threadId": "t_m1",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "owner@example.com"},
                        {"name": "Subject", "value": "Note to self"},
                    ]
                },
            }
        ],
    }
    provider = GmailProvider(service=FakeService(store), drafts_enabled=True)
    with pytest.raises(ProviderError, match="nobody to reply to"):
        provider.create_draft("t_m1", "Thanks.")
    service: FakeService = provider._service  # type: ignore[assignment]
    assert not any(name == "drafts.create" for name, _ in service.log)


# ---------------------------------------------------------------------------
# Hostile inbound headers are copied forward; they must not be trusted verbatim
# ---------------------------------------------------------------------------


def _header_names(mime: str) -> list[str]:
    """Header field names from a rendered message, unfolding continuations.

    A continuation line starts with whitespace and belongs to the header above
    it -- exactly the distinction that matters here. Injected text surviving as
    a folded continuation is inert content; the same text at column zero would
    be a header of its own, which is the actual injection.
    """
    names = []
    for line in mime.split("\n"):
        if not line.strip():
            break
        if line[:1] in (" ", "\t"):
            continue
        if ":" in line:
            names.append(line.split(":", 1)[0].strip())
    return names


@pytest.mark.parametrize(
    ("hostile", "expected"),
    [
        ("Hello\r\nBcc: attacker@evil.example", "Hello Bcc: attacker@evil.example"),
        ("Hello\nBcc: attacker@evil.example", "Hello Bcc: attacker@evil.example"),
        ("Hello\rBcc: attacker@evil.example", "Hello Bcc: attacker@evil.example"),
        ("  padded  ", "padded"),
    ],
)
def test_sanitize_header_folds_every_line_break_into_one_line(hostile: str, expected: str) -> None:
    """G8. Pins the sanitizer's BEHAVIOUR, not merely that nothing exploded.

    The predecessor tests wrapped create_draft in ``except ProviderError:
    return``, so they passed identically with ``_sanitize_header`` deleted --
    the ValueError path they were meant to prove was gone became just another
    early return. Asserting on the function itself cannot be satisfied that
    way: with the sanitizer stubbed to the identity these assertions fail.
    """
    from inbox_zero_connector.providers.gmail.messages import _sanitize_header

    result = _sanitize_header(hostile)
    assert result == expected
    assert "\n" not in result
    assert "\r" not in result


def test_a_crlf_bearing_subject_produces_a_draft_with_the_header_folded(
    store: dict[str, Any],
) -> None:
    """G8/F12. Subject is copied from the inbound message, so its content is
    attacker-controlled. The draft IS created -- no refusal, no uncaught
    ValueError -- and the injected text survives only as inert Subject content
    on ONE line, never as a Bcc header of its own.
    """
    store["thread"] = {
        "id": "t_m1",
        "messages": [
            _message("m1", "Hello\r\nBcc: attacker@evil.example"),
        ],
    }
    provider = GmailProvider(service=FakeService(store), drafts_enabled=True)
    result = provider.create_draft("t_m1", "Thanks.")
    assert result.draft_id == "DRAFT_1"
    mime = _draft_mime(provider)
    assert "Subject: Re: Hello Bcc: attacker@evil.example" in mime
    assert "Bcc" not in _header_names(mime)
    assert "\nBcc:" not in mime


def test_a_crlf_bearing_references_header_produces_a_draft_with_it_folded(
    store: dict[str, Any],
) -> None:
    """G8/F12, threading arm. References is copied forward the same way."""
    store["thread"] = {
        "id": "t_m1",
        "messages": [
            _message(
                "m1",
                "First",
                extra_headers=[
                    {
                        "name": "References",
                        "value": "<a@mail.example>\r\nBcc: attacker@evil.example",
                    }
                ],
            )
        ],
    }
    provider = GmailProvider(service=FakeService(store), drafts_enabled=True)
    result = provider.create_draft("t_m1", "Thanks.")
    assert result.draft_id == "DRAFT_1"
    mime = _draft_mime(provider)
    assert "<a@mail.example> Bcc: attacker@evil.example" in mime
    assert "Bcc" not in _header_names(mime)
    assert "\nBcc:" not in mime
