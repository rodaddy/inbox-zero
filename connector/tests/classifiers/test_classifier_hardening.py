"""The two edges a hostile or broken backend reaches first (#166).

WHY THESE ARE NOT IN ``test_openai_classifier.py``: that module pins the happy
path and the documented failure grammar. These two pin a CONTRACT the class
states in its own docstring -- "raised rather than returning a guess" -- against
inputs the SDK's types say cannot happen and a non-conforming server produces
anyway.

1. ``tool_call.function.arguments`` is typed ``str`` by the SDK. A server that
   answers ``null`` or a decoded object puts ``None`` or a ``dict`` there, and
   ``json.loads`` then raises ``TypeError`` -- a type error escaping a module
   whose whole error contract is ``ClassifierBackendError``. The watcher catches
   the documented error and turns it into a FAILED receipt; an escaping
   ``TypeError`` is an unhandled crash of the round instead.

2. A classifier call with NO TIMEOUT hangs the watcher forever on a server that
   accepts the connection and never answers -- the single most common failure of
   a local inference box under load. The bound is a construction-time property
   of the client, so it is asserted there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from inbox_zero_connector.classifiers.openai_compatible import (
    CLASSIFY_TOOL_NAME,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    ClassifierBackendError,
    OpenAICompatibleClassifier,
)
from inbox_zero_connector.providers.base import Message
from inbox_zero_connector.services.triage.taxonomy import default_taxonomy


def _message() -> Message:
    return Message(
        id="msg-hardening",
        thread_id="thread-hardening",
        sender="billing@vendor.example",
        subject="Invoice 4021 for July",
        received="2026-08-18T10:00:00Z",
        snippet="Your invoice is attached.",
    )


@dataclass(frozen=True)
class _Function:
    """The SDK types ``arguments`` as ``str``; this stub deliberately does not."""

    arguments: Any
    name: str = CLASSIFY_TOOL_NAME


class _ToolCall:
    def __init__(self, arguments: Any) -> None:
        self.function = _Function(arguments)


class _CompletionMessage:
    def __init__(self, arguments: Any) -> None:
        self.tool_calls = [_ToolCall(arguments)]
        self.content = ""


class _Choice:
    def __init__(self, arguments: Any) -> None:
        self.message = _CompletionMessage(arguments)


class _Response:
    def __init__(self, arguments: Any) -> None:
        self.choices = [_Choice(arguments)]


class _Client:
    """Answers with whatever was put in the arguments slot. No socket."""

    def __init__(self, arguments: Any) -> None:
        self._response = _Response(arguments)
        self.chat = self

    @property
    def completions(self) -> _Client:
        return self

    def create(self, **_kwargs: Any) -> Any:
        return self._response


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(None, id="null-arguments"),
        pytest.param({"folder": "Receipts", "confidence": 0.9, "reason": "x"}, id="decoded-object"),
        pytest.param(b'{"folder": "Receipts"}', id="bytes"),
        pytest.param(17, id="number"),
    ],
)
def test_non_string_tool_arguments_raise_the_class_contract(
    installed_taxonomy: None, arguments: Any
) -> None:
    """The class contract is ``ClassifierBackendError``. Never a raw ``TypeError``."""
    backend = OpenAICompatibleClassifier(client=_Client(arguments), model="tiny-nonconforming")
    with pytest.raises(ClassifierBackendError) as excinfo:
        backend.classify(_message(), default_taxonomy().folders)

    message = str(excinfo.value)
    assert "tiny-nonconforming" in message
    assert "ACTION REQUIRED" in message


def test_a_string_payload_still_parses(installed_taxonomy: None) -> None:
    """The guard rejects the wrong TYPE, not the well-formed string case."""
    arguments = json.dumps({"folder": "Receipts", "confidence": 0.9, "reason": "Invoice."})
    backend = OpenAICompatibleClassifier(client=_Client(arguments))
    assert backend.classify(_message(), default_taxonomy().folders).folder == "Receipts"


def test_the_constructed_client_is_bounded_by_a_timeout_and_retries() -> None:
    """A built-in client never waits forever on a server that stops answering."""
    backend = OpenAICompatibleClassifier(base_url="https://gw.example.test/v1")
    client = backend._client  # the bound is on the client the class built

    assert client.timeout == pytest.approx(DEFAULT_TIMEOUT_SECONDS)
    assert client.max_retries == DEFAULT_MAX_RETRIES


def test_the_timeout_is_configurable_by_the_caller() -> None:
    """The bound is a default, not a hardcode: config can raise it for a slow box."""
    backend = OpenAICompatibleClassifier(
        base_url="https://gw.example.test/v1", timeout_seconds=90.0, max_retries=1
    )
    assert backend._client.timeout == pytest.approx(90.0)
    assert backend._client.max_retries == 1


def test_the_default_bounds_are_sane() -> None:
    assert 0 < DEFAULT_TIMEOUT_SECONDS <= 120
    assert 0 <= DEFAULT_MAX_RETRIES <= 3
