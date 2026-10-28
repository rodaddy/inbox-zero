"""Graph adapter core CRUD: list, read, file, flag, and the no-send audit.

The original ``test_outlook_provider.py``. Everything below the first section
banner was moved into a sibling file on 2026-08-19 to get under the repo's
500-code-line ceiling; what remains is the Stage 1 surface -- ``list_inbox``,
``get_thread``, ``file_message``, ``set_important``, folder caching, error
translation -- plus the invariant that no request ever reaches a send or delete
endpoint. No network, no credential: every test runs against a mocked transport.
"""

from __future__ import annotations

import httpx
import pytest

from _outlook_harness import (
    MOCK_TRANSPORT_SENTINEL,
    Recorder,
)
from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.outlook.provider import (
    GRAPH_BASE,
    OutlookProvider,
)


def test_list_inbox_parses_messages(provider: OutlookProvider) -> None:
    messages = provider.list_inbox(limit=10)
    assert [m.id for m in messages] == ["m1", "m2"]
    assert messages[0].sender == "sender@example.com"
    assert messages[0].subject == "First"
    assert messages[0].is_unread is True


@pytest.mark.parametrize(
    ("extra_fields", "expected"),
    [
        ({"internetMessageId": "<m1@example.com>"}, "<m1@example.com>"),
        ({}, ""),
    ],
)
def test_list_inbox_projects_internet_message_id(
    extra_fields: dict[str, str], expected: str
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"value": [{"id": "m1", **extra_fields}]})

    client = httpx.Client(base_url=GRAPH_BASE, transport=httpx.MockTransport(respond))
    provider = OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)

    [message] = provider.list_inbox(limit=1)

    assert message.internet_message_id == expected
    assert "internetMessageId" in requests[0].url.params["$select"].split(",")


def test_list_inbox_unread_filter_is_sent(provider: OutlookProvider, recorder: Recorder) -> None:
    provider.list_inbox(unread_only=True)
    assert "isRead+eq+false" in str(recorder.requests[0].url) or "isRead eq false" in str(
        recorder.requests[0].url
    )


def test_list_inbox_clamps_an_absurd_limit(provider: OutlookProvider, recorder: Recorder) -> None:
    provider.list_inbox(limit=100_000)
    url = str(recorder.requests[0].url)
    assert "%24top=200" in url or "$top=200" in url


def test_get_thread_returns_bodies(provider: OutlookProvider) -> None:
    thread = provider.get_thread("c_m1")
    assert thread.subject == "First"
    assert thread.bodies == ["body of m1"]


def test_file_message_moves_into_an_existing_folder(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    import json as _json

    applied = provider.file_message("m1", "Action")
    assert applied == "Action"
    move = next(r for r in recorder.requests if r.url.path.endswith("/move"))
    assert _json.loads(move.content)["destinationId"] == "F_EXIST"


def test_file_message_creates_a_folder_only_when_missing(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    provider.file_message("m1", "Receipts")
    creates = [
        r for r in recorder.requests if r.method == "POST" and r.url.path.endswith("/mailFolders")
    ]
    assert len(creates) == 1


def test_folder_lookup_is_cached_across_calls(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    provider.file_message("m1", "Action")
    provider.file_message("m2", "Action")
    lookups = [
        r for r in recorder.requests if r.method == "GET" and r.url.path.endswith("/mailFolders")
    ]
    assert len(lookups) == 1


def test_set_important_patches_importance_and_flag(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    import json as _json

    assert provider.set_important("m1", True) == "high"
    patch = next(r for r in recorder.requests if r.method == "PATCH")
    payload = _json.loads(patch.content)
    assert payload["importance"] == "high"
    assert payload["flag"]["flagStatus"] == "flagged"


def test_set_important_false_lowers_the_flag(provider: OutlookProvider, recorder: Recorder) -> None:
    import json as _json

    assert provider.set_important("m1", False) == "normal"
    patch = next(r for r in recorder.requests if r.method == "PATCH")
    payload = _json.loads(patch.content)
    assert payload["importance"] == "normal"
    assert payload["flag"]["flagStatus"] == "notFlagged"


def test_http_errors_surface_as_provider_errors(recorder: Recorder) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "Access denied"}})

    client = httpx.Client(base_url=GRAPH_BASE, transport=httpx.MockTransport(fail))
    provider = OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)
    with pytest.raises(ProviderError, match="403"):
        provider.list_inbox()


def test_no_request_ever_hits_a_send_or_delete_endpoint(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """Invariants 1 and 3. Exercise every path, then audit the traffic.

    ``/createReply`` is no longer forbidden here: it is the REPLY-DRAFT action,
    it runs under Mail.ReadWrite, and it does not send. Graph's actual send
    paths are ``/me/sendMail`` and the per-message ``/send`` action, and both
    are still asserted absent. ``/forward`` stays banned -- forwarding is not a
    capability this connector has.
    """
    from inbox_zero_connector.providers.base import SentSelection

    provider.list_inbox()
    provider.get_thread("c_m1")
    provider.read_sent_sample(SentSelection(after="2026-01-01"))
    provider.file_message("m1", "Action")
    provider.set_important("m1", True)
    provider.create_draft("c_m1", "Thanks.")

    assert recorder.requests, "expected recorded traffic"
    for request in recorder.requests:
        assert request.method != "DELETE", f"DELETE issued to {request.url.path}"
        path = request.url.path.lower()
        assert "sendmail" not in path
        assert not path.endswith("/send")
        assert "/forward" not in path
        assert "/createforward" not in path
