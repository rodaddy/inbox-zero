"""Per-container delta cold start, the delta ``$select``, and draft filtering.

Issues #143/#150. Graph runs ONE delta stream per FOLDER, so a watcher covering
more than the inbox has to be able to say WHICH folder a cold start opens; on a
resume the folder already rides inside the stored ``deltaLink`` and no parameter
is needed. These tests drive that through the public
``OutlookProvider.poll_changes`` boundary against the shared mocked transport.
"""

from __future__ import annotations

from typing import Any

import pytest

from _outlook_harness import MOCK_TRANSPORT_SENTINEL, Recorder
from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE, OutlookProvider

ARCHIVE_FOLDER_ID = "F_ARCHIVE"
COLD_DELTA_LINK = (
    f"{GRAPH_BASE}/me/mailFolders/{ARCHIVE_FOLDER_ID}/messages/delta?$deltatoken=cold-1"
)
RESUME_LINK = f"{GRAPH_BASE}/me/mailFolders/{ARCHIVE_FOLDER_ID}/messages/delta?$deltatoken=r-1"


def _queue_final_page(recorder: Recorder, values: list[dict[str, Any]]) -> None:
    """Queue one successful final delta page in the shared transport."""
    recorder.get_status.append(200)
    recorder.delta_payloads.append({"value": values, "@odata.deltaLink": COLD_DELTA_LINK})


def test_cold_start_walks_the_named_container(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A named container opens THAT folder's stream, not the inbox's."""
    _queue_final_page(recorder, [])

    provider.poll_changes(None, container_id=ARCHIVE_FOLDER_ID)

    assert recorder.requests[0].url.path.endswith(
        f"/me/mailFolders/{ARCHIVE_FOLDER_ID}/messages/delta"
    )


def test_cold_start_without_a_container_still_walks_the_inbox(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """``None`` keeps the provider default stream, so existing callers are unmoved."""
    _queue_final_page(recorder, [])

    provider.poll_changes(None, container_id=None)

    assert recorder.requests[0].url.path.endswith("/me/mailFolders/inbox/messages/delta")


def test_resume_ignores_the_container_because_the_link_already_encodes_it(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A stored ``deltaLink`` is replayed VERBATIM; the container cannot override it."""
    _queue_final_page(recorder, [])

    provider.poll_changes(RESUME_LINK, container_id="F_SOMETHING_ELSE")

    assert str(recorder.requests[0].url) == RESUME_LINK


def test_cold_start_projects_only_the_fields_the_adapter_reads(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """The delta GET carries a ``$select`` naming exactly the projected fields."""
    _queue_final_page(recorder, [])

    provider.poll_changes(None)

    selected = recorder.requests[0].url.params["$select"].split(",")
    assert sorted(selected) == sorted([
        "id",
        "conversationId",
        "internetMessageId",
        "from",
        "subject",
        "receivedDateTime",
        "bodyPreview",
        "isRead",
        "importance",
        "flag",
        "parentFolderId",
        "isDraft",
    ])


def test_drafts_are_skipped_before_projection(
    provider: OutlookProvider, recorder: Recorder
) -> None:
    """A delta arrival Graph marks ``isDraft`` never reaches the change set."""
    _queue_final_page(
        recorder,
        [
            {"id": "d1", "subject": "half typed", "isDraft": True},
            {"id": "m1", "subject": "real mail", "isDraft": False},
        ],
    )

    changes = provider.poll_changes(None)

    assert [message.id for message in changes.messages] == ["m1"]


def test_dropped_drafts_are_logged_as_a_count(
    provider: OutlookProvider, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """The DEBUG line reports how many were dropped, never what they said."""
    import logging

    from loguru import logger

    handler_id = logger.add(
        lambda record: logging.getLogger("delta-test").debug(record), level="DEBUG"
    )
    try:
        _queue_final_page(
            recorder,
            [
                {"id": "d1", "subject": "half typed", "isDraft": True},
                {"id": "d2", "subject": "also half typed", "isDraft": True},
            ],
        )
        with caplog.at_level(logging.DEBUG, logger="delta-test"):
            provider.poll_changes(None)
    finally:
        logger.remove(handler_id)

    drafts = [line for line in caplog.text.splitlines() if "draft" in line.lower()]
    assert drafts, caplog.text
    assert "skipped=2" in "\n".join(drafts)
    assert "half typed" not in caplog.text


def test_file_message_reports_an_unknown_id_when_move_returns_no_id(
    recorder: Recorder,
) -> None:
    """A ``/move`` response with no id must NOT be reported as the pre-move id."""
    import httpx

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/move"):
            return httpx.Response(200, json={})
        return recorder.handle(request)

    client = httpx.Client(base_url=GRAPH_BASE, transport=httpx.MockTransport(respond))
    provider = OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)

    applied = provider.file_message("m1", "Action")

    assert str(applied) == "Action"
    assert applied.message_id is None
