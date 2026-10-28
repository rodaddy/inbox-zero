"""``create_draft``: the reply-draft action and the scope posture it preserves.

Split out of ``test_outlook_provider.py`` on 2026-08-19 (500-code-line ceiling).
Owns the Stage 2 ``create_draft`` banner. The concern is that drafting a reply
never widens what the connector can do: the adapter posts to ``/createReply``
with a comment and NO recipients, so Graph's own reply semantics choose the
addressees and the connector's signature offers no way to name them.
"""

from __future__ import annotations

import pytest

from _outlook_harness import (
    Recorder,
    _message,
)
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.outlook.provider import (
    OutlookProvider,
)

# ---------------------------------------------------------------------------
# Stage 2: create_draft
# ---------------------------------------------------------------------------


def test_create_draft_uses_the_reply_draft_action(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The whole reason Graph's scope posture is unchanged by Stage 2."""
    result = provider.create_draft("c_m1", "Thanks.")
    assert result.draft_id == "DRAFT_1"
    assert any(r.url.path.endswith("/createReply") for r in recorder.requests)


def test_create_draft_sends_only_a_comment_never_recipients(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Microsoft addresses the reply server-side; we supply the body and nothing else."""
    import json as _json

    provider.create_draft("c_m1", "Thanks.")
    call = next(r for r in recorder.requests if r.url.path.endswith("/createReply"))
    payload = _json.loads(call.content)
    assert payload == {"comment": "Thanks."}
    for banned in ("toRecipients", "ccRecipients", "bccRecipients", "message"):
        assert banned not in payload


def test_create_self_draft_posts_a_subject_and_body_and_no_recipients(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The digest draft is addressed to NOBODY (#65, D10/D20).

    The PAYLOAD is asserted rather than the return value, because the absence
    is the security property: an address can only reach Graph through one of
    the three recipient collections, and none of them is here.
    """
    import json as _json

    draft_id = provider.create_self_draft("Inbox Zero digest 2026-09-01", "3 filed.")

    assert draft_id == "SELF_DRAFT_1"
    call = next(
        r for r in recorder.requests if r.method == "POST" and r.url.path.endswith("/me/messages")
    )
    payload = _json.loads(call.content)
    assert payload == {
        "subject": "Inbox Zero digest 2026-09-01",
        "body": {"contentType": "Text", "content": "3 filed."},
    }
    for banned in ("toRecipients", "ccRecipients", "bccRecipients"):
        assert banned not in payload


def test_create_self_draft_never_reaches_a_send_or_reply_endpoint(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The whole path, asserted at the transport: create only, no send verb."""
    provider.create_self_draft("Inbox Zero digest 2026-09-01", "3 filed.")

    writes = [r.url.path for r in recorder.requests if r.method != "GET"]
    assert writes == ["/v1.0/me/messages"]
    paths = [r.url.path for r in recorder.requests]
    for banned in ("/send", "/sendMail", "/createReply", "/forward"):
        assert not any(path.endswith(banned) for path in paths)


def test_create_draft_recipients_come_back_from_graph(provider: OutlookProvider) -> None:
    result = provider.create_draft("c_m1", "Thanks.")
    assert result.recipients == ["sender@example.com", "cc@other.example"]


def test_create_draft_replies_to_the_most_recent_message(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    recorder.thread = [_message("newest", "First"), _message("older", "First")]
    provider.create_draft("c_m1", "Thanks.")
    call = next(r for r in recorder.requests if r.url.path.endswith("/createReply"))
    assert "/newest/" in call.url.path


def test_create_draft_refuses_a_thread_that_does_not_exist(
    provider: OutlookProvider, recorder: Recorder
) -> None:

    recorder.thread = []
    with pytest.raises(ProviderError, match="nothing to reply to"):
        provider.create_draft("c_missing", "hi")
    assert not any(r.url.path.endswith("/createReply") for r in recorder.requests)


def test_create_draft_signature_offers_no_recipient_argument() -> None:
    import inspect

    assert set(inspect.signature(OutlookProvider.create_draft).parameters) == {
        "self",
        "thread_id",
        "body",
    }


def test_get_thread_exposes_participants_and_categories(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    recorder.thread = [_message("m1", "First", categories=["Prospects"])]
    thread = provider.get_thread("c_m1")
    assert thread.participants == [
        "sender@example.com",
        "owner@example.com",
        "cc@other.example",
    ]
    assert thread.labels == ["Prospects"]
