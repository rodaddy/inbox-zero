"""Shared Graph-adapter test harness: the mocked httpx transport and its fixtures.

NOT a conftest. ``recorder`` and ``provider`` are Graph-specific names, and a
root conftest would publish them to every gmail/ledger/watcher test module in
this suite -- ``provider`` in particular is a name another adapter's tests could
plausibly want for their own fixture, and the collision would resolve silently
in conftest's favour. A leading-underscore module is not collected by pytest and
is imported by name, so each Outlook test file states its dependency out loud.

Split out of ``test_outlook_provider.py`` on 2026-08-19 when that file exceeded
the repo's 500-code-line ceiling and had to be broken along its own section
banners; every file born from that split imports this one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from inbox_zero_connector.providers.outlook.provider import (
    GRAPH_BASE,
    OutlookProvider,
)

_Handler = Callable[[httpx.Request], "httpx.Response | None"]
"""A route handler. ``None`` means "not mine", so the table falls through."""

# Not a credential. Every test here runs against httpx.MockTransport, so this
# string is never sent anywhere and authenticates nothing -- it exists only to
# satisfy the adapter's "do I already have a token" branch without touching the
# OS keychain. Named rather than inlined so flake8-bandit's hardcoded-password
# rule stays ON for the rest of the suite instead of being suppressed, and
# named WITHOUT a credential-shaped word so the rule does not fire on the
# constant that exists to avoid it.
MOCK_TRANSPORT_SENTINEL = "mock-transport-only-authenticates-nothing"


def _ends(suffix: str) -> Callable[[str], bool]:
    """Path predicate for the route table."""
    return lambda path: path.endswith(suffix)


def _contains(fragment: str) -> Callable[[str], bool]:
    """Path predicate for the route table."""
    return lambda path: fragment in path


SENT_FOLDER_ID = "F_SENTITEMS"
"""The mailbox's sent-items folder id, as Graph would report it.

Graph's ``sentitems`` is a WELL-KNOWN NAME, not an id: a message's
``parentFolderId`` carries the real folder id, so proving a message is sent
mail means resolving the name to that id once and comparing.
"""

INBOX_FOLDER_ID = "F_INBOX"
"""Any other folder. A message parented here is NOT sent mail."""


def _message(
    msg_id: str,
    subject: str,
    important: bool = False,
    categories: list[str] | None = None,
    parent_folder_id: str = SENT_FOLDER_ID,
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "conversationId": f"c_{msg_id}",
        "parentFolderId": parent_folder_id,
        "from": {"emailAddress": {"address": "sender@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "owner@example.com"}}],
        "ccRecipients": [{"emailAddress": {"address": "cc@other.example"}}],
        "categories": categories or [],
        "subject": subject,
        "receivedDateTime": "2026-08-17T14:00:00Z",
        "sentDateTime": "2026-08-17T14:00:00Z",
        "bodyPreview": f"preview {msg_id}",
        "isRead": False,
        "importance": "high" if important else "normal",
        "flag": {"flagStatus": "flagged" if important else "notFlagged"},
        "body": {"contentType": "text", "content": f"body of {msg_id}"},
    }


class Recorder:
    """Mock transport that logs every request and serves canned responses."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.folders: list[dict[str, Any]] = [{"id": "F_EXIST", "displayName": "Action"}]
        self.thread: list[dict[str, Any]] = [_message("m1", "First")]
        self.sent: list[dict[str, Any]] = [_message("s1", "Re: Hello")]
        self.inbox_only: list[dict[str, Any]] = [
            _message("i1", "Confidential", parent_folder_id=INBOX_FOLDER_ID)
        ]
        """Messages that exist in the mailbox but are NOT sent mail.

        ``GET /me/messages/{id}`` resolves any message in the mailbox
        regardless of folder, which is exactly why the sent-only contract
        cannot be assumed from the endpoint.
        """

        self.sent_folder_payload: dict[str, Any] | None = None
        """Override for the sentitems folder body. ``None`` serves the real one.

        Exists so a test can hand the adapter a SUCCESSFUL response carrying
        no usable id -- the shape that made the sent-only guard compare two
        empty strings and accept a message it could not prove anything about.
        """

        self.create_folder_payload: dict[str, Any] | None = None
        """Override for the created-folder body. Same purpose, create arm."""

        self.tagged: dict[str, Any] = {
            "id": "t1",
            "@odata.etag": 'W/"ETAG-1"',
            "categories": [],
        }
        """The one message the category path reads and writes, WITH its etag.

        Stateful on purpose. A ``categories`` PATCH REPLACES the collection, so
        a stub answering a fixed body could not show the union preserving
        anything -- and what happens to a value that was already there is the
        entire defect under test.
        """

        self.delta_payloads: list[dict[str, Any]] = []
        """Delta pages served in order, then an empty final page."""

        self.get_status: list[int] = []
        """Statuses the delta ``GET`` serves, one per call, then 200."""

        self.patch_status: list[int] = []
        """Statuses ``PATCH`` serves, one per call, then the normal behaviour.

        A LIST rather than a flag because the 412 path is about a SEQUENCE:
        fail, re-read, succeed. A single toggle cannot express "the second
        attempt is the one that lands", which is the behaviour proving the
        retry re-GETs instead of replaying a stale body.
        """

        self.if_match_seen: list[str | None] = []
        """The ``If-Match`` header of every PATCH, in order."""

        self.patch_bodies: list[dict[str, Any]] = []
        """The parsed body of every PATCH, in order."""

        self.before: Callable[[httpx.Request], None] | None = None
        """Runs before each request is routed. The only way to model a RACE.

        A concurrent edit is by definition something that happens between our
        GET and our PATCH, so no canned response can express it -- the state has
        to change while the adapter is mid-sequence. This hook is where a test
        plays the other client.
        """

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Route one mocked Graph request. Table dispatch, not an if/elif ladder.

        The routes are DATA: each row is (method, predicate-on-path, handler),
        checked in order, so adding an endpoint is one row and the branch count
        stays at one. Same rule the connector's own control flow follows
        (``_DOCS/STANDARDS-core.md`` ## Control Flow) -- a test double that
        grows a nine-deep ladder stops being readable as a spec of what the
        adapter is allowed to call.
        """
        if self.before is not None:
            self.before(request)
        self.requests.append(request)
        path = request.url.path
        for method, matches, handler in self._routes():
            if request.method == method and matches(path):
                response = handler(request)
                if response is not None:
                    return response
        return httpx.Response(404, json={"error": {"message": f"unmapped {request.method} {path}"}})

    def _routes(self) -> list[tuple[str, Callable[[str], bool], _Handler]]:
        """The route table. Order matters: the first match wins."""
        return [
            (
                "GET",
                lambda path: "/me/mailFolders/" in path and path.endswith("/messages/delta"),
                self._delta,
            ),
            ("GET", _ends("/mailFolders/inbox/messages"), self._inbox),
            ("GET", _ends("/mailFolders/sentitems/messages"), self._sent_folder),
            ("GET", _ends("/mailFolders/sentitems"), self._sent_folder_metadata),
            ("GET", _ends("/me/messages"), self._thread),
            ("POST", _ends("/createReply"), self._create_reply),
            ("GET", _ends("/me/mailFolders"), self._list_folders),
            ("GET", _contains("/me/messages/"), self._one_message),
            ("POST", _ends("/me/messages"), self._create_self_draft),
            ("POST", _ends("/me/mailFolders"), self._create_folder),
            ("POST", _ends("/move"), self._move),
            ("PATCH", lambda _: True, self._patch),
        ]

    def _delta(self, request: httpx.Request) -> httpx.Response:
        """Serve queued delta pages and per-call GET failures."""
        status = self.get_status.pop(0) if self.get_status else 200
        if status != 200:
            return httpx.Response(status, json={"error": {"code": "SyncStateNotFound"}})
        payload = (
            self.delta_payloads.pop(0)
            if self.delta_payloads
            else {"value": [], "@odata.deltaLink": str(request.url)}
        )
        return httpx.Response(200, json=payload)

    def _sent_folder_metadata(self, _: httpx.Request) -> httpx.Response:
        """Resolve the ``sentitems`` well-known name to the real folder id."""
        if self.sent_folder_payload is not None:
            return httpx.Response(200, json=self.sent_folder_payload)
        return httpx.Response(200, json={"id": SENT_FOLDER_ID, "displayName": "Sent Items"})

    def _inbox(self, _: httpx.Request) -> httpx.Response:
        inbox = [_message("m1", "First"), _message("m2", "Second")]
        return httpx.Response(200, json={"value": inbox})

    def _sent_folder(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": self.sent})

    def _thread(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": self.thread})

    def _create_reply(self, _: httpx.Request) -> httpx.Response:
        reply = {
            "id": "DRAFT_1",
            "toRecipients": [{"emailAddress": {"address": "sender@example.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "cc@other.example"}}],
        }
        return httpx.Response(201, json=reply)

    def _create_self_draft(self, _: httpx.Request) -> httpx.Response:
        """``POST /me/messages`` -- the recipientless digest draft (#65).

        Echoes back only an id. It deliberately does NOT echo recipient
        collections: a fake that invented them would let a test assert
        "no recipients" against a response the adapter never sent.
        """
        return httpx.Response(201, json={"id": "SELF_DRAFT_1"})

    def _list_folders(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": self.folders})

    def _one_message(self, request: httpx.Request) -> httpx.Response | None:
        """None means "no such message", so the table falls through to 404.

        Searches sent AND inbox, because Graph does: ``GET /me/messages/{id}``
        is mailbox-wide and will happily hand back an inbox message to a
        caller that asked the sent-sample tool for it.
        """
        msg_id = request.url.path.rsplit("/", 1)[-1]
        mailbox = [*self.sent, *self.inbox_only, self.tagged]
        found = next((m for m in mailbox if m["id"] == msg_id), None)
        return httpx.Response(200, json=found) if found is not None else None

    def _create_folder(self, request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content)["displayName"]
        if self.create_folder_payload is not None:
            return httpx.Response(201, json=self.create_folder_payload)
        created = {"id": f"F_{name}", "displayName": name}
        self.folders.append(created)
        return httpx.Response(201, json=created)

    def _move(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "moved"})

    def _patch(self, request: httpx.Request) -> httpx.Response:
        """Record every PATCH, then behave the way Graph does for ``categories``.

        REPLACE, not merge, because that is what the live API does: writing
        ``["B"]`` over ``["A"]`` returns 200 and ``A`` is gone. A stub that
        merged would make a broken union look correct while the real service
        ate the owner's categories.
        """
        self.if_match_seen.append(request.headers.get("If-Match"))
        body = json.loads(request.content) if request.content else {}
        self.patch_bodies.append(body)
        if self.patch_status:
            return httpx.Response(
                self.patch_status.pop(0), json={"error": {"code": "ErrorSomething"}}
            )
        if "categories" in body and request.url.path.endswith(f"/{self.tagged['id']}"):
            self.tagged["categories"] = list(body["categories"])
            self.tagged["@odata.etag"] = f'W/"ETAG-{len(self.patch_bodies) + 1}"'
            return httpx.Response(200, json=self.tagged)
        return httpx.Response(200, json={"id": "patched"})

    def method_paths(self) -> set[tuple[str, str]]:
        return {(r.method, r.url.path) for r in self.requests}


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def provider(recorder: Recorder) -> OutlookProvider:
    client = httpx.Client(
        base_url=GRAPH_BASE,
        transport=httpx.MockTransport(recorder.handle),
        headers={"Authorization": f"Bearer {MOCK_TRANSPORT_SENTINEL}"},
    )
    return OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)


# Ids a hostile (or merely confused) caller could hand the tool layer. Each one
# is a DIFFERENT way to leave the message segment: an absolute-looking path, a
# traversal, a fragment or query that truncates the suffix, a CRLF pair that
# splits the request, and a bare slash. The point of the table is that the
# refusal is a property of the id GRAMMAR, not a blocklist of known-bad strings.
_HOSTILE_MESSAGE_IDS = [
    "m1/../../me/sendMail",
    "../../me/sendMail",
    "m1#/move",
    "m1?x=/move",
    "m1\r\nX-Injected: 1",
    "m1/send",
    "m1/",
    "/me/sendMail",
]
