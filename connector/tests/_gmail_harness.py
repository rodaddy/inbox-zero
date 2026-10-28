"""Shared Gmail test harness: the mocked google-api-python-client and its fixtures.

NOT a conftest. ``store``, ``provider``, and ``drafting_provider`` are
Gmail-specific names, and a root conftest would publish them to every outlook,
ledger, and watcher module in this suite -- ``provider`` in particular is a name
another adapter's tests already use for their OWN fixture, and the collision
would resolve silently in conftest's favour. A leading-underscore module is not
collected by pytest and is imported by name, so each Gmail test file states its
dependency out loud.

The fakes replay canned responses off a ``store`` dict and append every call to
a log, so a test asserts on WHAT WAS REQUESTED without a network or a
credential. ``FakeDrafts`` deliberately has no ``send`` method; that absence is
itself asserted, because a fake offering ``send`` would let a regression pass
the suite while failing the product.

Split out of ``test_gmail_provider.py`` on 2026-08-19 when that file exceeded
the repo's 500-code-line ceiling and had to be broken along its own section
banners; every file born from that split imports this one.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from inbox_zero_connector.providers.gmail import provider as gmail  # ruff: ignore[unused-import]
from inbox_zero_connector.providers.gmail.provider import (
    LEGACY_LABEL_PREFIX as LEGACY_LABEL_PREFIX,
)
from inbox_zero_connector.providers.gmail.provider import (
    GmailProvider as GmailProvider,
)


class _Call:
    """Records a request and replays a canned response, like the google client."""

    def __init__(self, log: list[tuple[str, dict[str, Any]]], name: str, result: Any) -> None:
        self._log = log
        self._name = name
        self._result = result

    def execute(self) -> Any:
        return self._result


class FakeMessages:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], store: dict[str, Any]) -> None:
        self._log = log
        self._store = store

    def list(self, **kwargs: Any) -> _Call:
        self._log.append(("messages.list", kwargs))
        key = "sent_list" if "in:sent" in str(kwargs.get("q", "")) else "list"
        return _Call(self._log, "messages.list", self._store[key])

    def get(self, **kwargs: Any) -> _Call:
        self._log.append(("messages.get", kwargs))
        return _Call(self._log, "messages.get", self._store["messages"][kwargs["id"]])

    def modify(self, **kwargs: Any) -> _Call:
        self._log.append(("messages.modify", kwargs))
        return _Call(self._log, "messages.modify", {"id": kwargs["id"]})


class FakeDrafts:
    """The drafts resource. It has a create method and NO send method.

    That absence is deliberate and is asserted by
    ``test_the_fake_drafts_resource_has_no_send_method`` -- a fake that offered
    ``send`` would let a regression pass the suite while failing the product.
    """

    def __init__(self, log: list[tuple[str, dict[str, Any]]], store: dict[str, Any]) -> None:
        self._log = log
        self._store = store

    def create(self, **kwargs: Any) -> _Call:
        self._log.append(("drafts.create", kwargs))
        return _Call(self._log, "drafts.create", {"id": "DRAFT_1", "message": {"id": "m9"}})


class FakeThreads:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], store: dict[str, Any]) -> None:
        self._log = log
        self._store = store

    def get(self, **kwargs: Any) -> _Call:
        self._log.append(("threads.get", kwargs))
        return _Call(self._log, "threads.get", self._store["thread"])


class FakeLabels:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], store: dict[str, Any]) -> None:
        self._log = log
        self._store = store

    def list(self, **kwargs: Any) -> _Call:
        self._log.append(("labels.list", kwargs))
        return _Call(self._log, "labels.list", {"labels": self._store["labels"]})

    def create(self, **kwargs: Any) -> _Call:
        self._log.append(("labels.create", kwargs))
        # ``store["label_create_hook"]``, when present, OWNS the outcome. It is
        # how the 409 race is driven -- a competing client minting the label
        # between our list and our create -- without a network or a real
        # Google error object leaking into every other test's fixture.
        hook = self._store.get("label_create_hook")
        if hook is not None:
            return _Call(self._log, "labels.create", hook(**kwargs))
        created = {"id": f"LBL_{kwargs['body']['name']}", "name": kwargs["body"]["name"]}
        self._store["labels"].append(created)
        return _Call(self._log, "labels.create", created)


class FakeHistory:
    """The ``users.history`` resource, replaying a canned page or an error.

    ``store["history"]`` is the page to return. ``store["history_error"]``, if
    present, is raised instead -- that is how the refused-position path is
    driven without a network or a real Google error.
    """

    def __init__(self, log: list[tuple[str, dict[str, Any]]], store: dict[str, Any]) -> None:
        self._log = log
        self._store = store

    def list(self, **kwargs: Any) -> _Call:
        self._log.append(("history.list", kwargs))
        error = self._store.get("history_error")
        if error is not None:
            raise error
        return _Call(self._log, "history.list", self._store["history"])


class FakeUsers:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], store: dict[str, Any]) -> None:
        self._log = log
        self._store = store

    def messages(self) -> FakeMessages:
        return FakeMessages(self._log, self._store)

    def threads(self) -> FakeThreads:
        return FakeThreads(self._log, self._store)

    def labels(self) -> FakeLabels:
        return FakeLabels(self._log, self._store)

    def drafts(self) -> FakeDrafts:
        return FakeDrafts(self._log, self._store)

    def history(self) -> FakeHistory:
        return FakeHistory(self._log, self._store)

    def getProfile(self, **kwargs: Any) -> _Call:  # ruff: ignore[invalid-function-name]
        self._log.append(("getProfile", kwargs))
        return _Call(
            self._log,
            "getProfile",
            {
                "emailAddress": "owner@example.com",
                "historyId": self._store.get("history_id", "9001"),
            },
        )


class FakeService:
    def __init__(self, store: dict[str, Any]) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self._store = store

    def users(self) -> FakeUsers:
        return FakeUsers(self.log, self._store)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(
    msg_id: str,
    subject: str,
    labels: list[str] | None = None,
    extra_headers: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "threadId": f"t_{msg_id}",
        "snippet": f"snippet for {msg_id}",
        "labelIds": labels or ["INBOX", "UNREAD"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Sender Name <sender@example.com>"},
                {"name": "To", "value": "owner@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Sun, 17 Aug 2026 10:00:00 -0400"},
                {"name": "Message-Id", "value": f"<{msg_id}@mail.example>"},
                *(extra_headers or []),
            ],
            "body": {"data": _b64(f"body of {msg_id}")},
        },
    }


def _sent(msg_id: str, labels: list[str] | None = None) -> dict[str, Any]:
    return _message(
        msg_id,
        "Re: Hello",
        labels=labels or ["SENT"],
        extra_headers=[{"name": "Cc", "value": "cc@other.example"}],
    )


@pytest.fixture
def store() -> dict[str, Any]:
    thread_message = _message(
        "m1",
        "First",
        extra_headers=[
            {"name": "Cc", "value": "cc@other.example"},
            {"name": "References", "value": "<earlier@mail.example>"},
        ],
    )
    return {
        "list": {"messages": [{"id": "m1"}, {"id": "m2"}]},
        "sent_list": {"messages": [{"id": "s1"}]},
        "messages": {
            "m1": thread_message,
            "m2": _message("m2", "Second"),
            "s1": _sent("s1"),
            "s2": _sent("s2", labels=["SENT", "Label_Legal"]),
        },
        "thread": {"id": "t_m1", "messages": [thread_message]},
        "labels": [{"id": "LBL_EXIST", "name": "Action"}],
        "history_id": "9001",
        "history": {
            "historyId": "9100",
            "history": [{"messagesAdded": [{"message": {"id": "m2"}}]}],
        },
    }


@pytest.fixture
def provider(store: dict[str, Any]) -> GmailProvider:
    return GmailProvider(service=FakeService(store))


@pytest.fixture
def drafting_provider(store: dict[str, Any]) -> GmailProvider:
    return GmailProvider(service=FakeService(store), drafts_enabled=True)


# ---------------------------------------------------------------------------
# Call-log accessors. Shared because the 2026-08-19 split left the tests that
# use them in a DIFFERENT file from the section they were written for: a query
# assertion belongs with query-injection, a MIME assertion with drafts, and the
# owner-on-Cc store is read by both. Duplicating them per file would let the two
# copies drift into reading the log differently, which is exactly the kind of
# silent disagreement the harness exists to prevent.
# ---------------------------------------------------------------------------


def _query(provider: GmailProvider) -> str:
    service: FakeService = provider._service  # type: ignore[assignment]
    call = next(kwargs for name, kwargs in service.log if name == "messages.list")
    return str(call["q"])


def _draft_mime(provider: GmailProvider) -> str:
    import base64 as _b

    service: FakeService = provider._service  # type: ignore[assignment]
    call = next(kwargs for name, kwargs in service.log if name == "drafts.create")
    raw = call["body"]["message"]["raw"]
    return _b.urlsafe_b64decode(raw.encode()).decode()


def _owner_cc_store(store: dict[str, Any]) -> dict[str, Any]:
    """A thread where the OWNER is on Cc alongside a third party."""
    store["thread"] = {
        "id": "t_m1",
        "messages": [
            _message(
                "m1",
                "First",
                extra_headers=[
                    {"name": "Cc", "value": "cc@other.example, Owner <owner@example.com>"}
                ],
            )
        ],
    }
    return store
