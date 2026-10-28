"""The Location header Graph offers on a refused delta token, and its absence.

Graph answers an evicted delta token with ``410 Gone`` /
``syncStateNotFound``, and it MAY carry a ``Location`` header naming the url to
resume from. Discarding that header costs a full cold-start folder walk on
every 410 -- expected operation on a healthy mailbox, not an error -- so the
header is replayed when present and the cold start stands when it is not.

Its own transport rather than the shared ``Recorder``, for one reason: the
shared double's ``_delta`` route serves a 410 with no headers at all, and the
header IS the thing under test here. The pattern is the same one
``_outlook_harness`` uses -- an ``httpx.MockTransport`` over a handler that
records every request and serves canned responses in order -- kept local so no
existing test's double changes shape.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from _outlook_harness import MOCK_TRANSPORT_SENTINEL
from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE, OutlookProvider

REFUSED_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_INBOX/messages/delta"
    "?$deltatoken=evicted-token"
)
"""The stored position Graph is about to refuse."""

RESUME_LOCATION = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_INBOX/messages/delta"
    "?$deltatoken=served-by-location&state=A%2BB%2F9%3D"
)
"""The url Graph names in ``Location``. Percent-encoding on purpose.

A url that survives a round trip only when it is replayed BYTE FOR BYTE. An
implementation that parsed and reassembled it would normalise ``%2B`` and
``%2F`` and issue a position Graph never handed out.
"""

FINAL_DELTA_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/F_INBOX/messages/delta"
    "?$deltatoken=round-complete"
)

COLD_START_PATH = "/v1.0/me/mailFolders/inbox/messages/delta"
"""What a cold start walks. Asserted ABSENT on the Location path."""


class _DeltaRecorder:
    """Serves one refusal then one page, and records every request url."""

    def __init__(self, *, location: str | None) -> None:
        self.location = location
        self.requests: list[httpx.Request] = []
        self.served = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.served += 1
        if self.served == 1:
            headers = {} if self.location is None else {"Location": self.location}
            return httpx.Response(
                410, json={"error": {"code": "syncStateNotFound"}}, headers=headers
            )
        page: dict[str, Any] = {"value": [], "@odata.deltaLink": FINAL_DELTA_LINK}
        return httpx.Response(200, json=page)

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


def _provider_for(recorder: _DeltaRecorder) -> OutlookProvider:
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(recorder.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    return OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)


def test_410_with_a_location_header_resumes_from_that_url_verbatim() -> None:
    """The second GET is the ``Location`` url, and no cold start is issued.

    Both halves are asserted because either alone would pass an implementation
    that is wrong in the other direction: replaying the header AND still
    walking the folder from the beginning would keep the cost the header exists
    to avoid.
    """
    recorder = _DeltaRecorder(location=RESUME_LOCATION)

    changes = _provider_for(recorder).poll_changes(REFUSED_LINK)

    assert recorder.urls() == [REFUSED_LINK, RESUME_LOCATION]
    assert all(COLD_START_PATH not in url for url in recorder.urls())
    assert changes.resync_required is False
    assert changes.cursor == FINAL_DELTA_LINK


def test_410_without_a_location_header_still_asks_for_a_cold_start() -> None:
    """No header, no resume: the round ends in ``resync_required`` as before.

    The unchanged path, asserted so the header support cannot quietly become a
    silent retry on a refusal Graph offered no position for.
    """
    recorder = _DeltaRecorder(location=None)

    changes = _provider_for(recorder).poll_changes(REFUSED_LINK)

    assert recorder.urls() == [REFUSED_LINK]
    assert changes.resync_required is True
    assert changes.cursor is None


@pytest.mark.parametrize("location", ["", "   "])
def test_a_blank_location_header_is_not_a_resume_position(location: str) -> None:
    """An empty header value is treated as absent, not replayed as a url.

    httpx resolves ``""`` against the client base url, so replaying a blank
    header would silently issue a GET against the Graph ROOT -- a request the
    service never named, dressed as one it did.
    """
    recorder = _DeltaRecorder(location=location)

    changes = _provider_for(recorder).poll_changes(REFUSED_LINK)

    assert recorder.urls() == [REFUSED_LINK]
    assert changes.resync_required is True
