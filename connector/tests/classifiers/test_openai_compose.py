"""``compose`` drives the SAME backend as ``classify``, with no tool and no invention.

Separate from ``test_openai_classifier.py`` because the two methods make
opposite demands of the model. ``classify`` REQUIRES a tool call and refuses
prose; ``compose`` requires prose and offers no tool at all. Keeping the
drafting assertions in their own file means the "no tools reached the wire"
claim is not buried among a suite whose every other call sends one.

THE INVARIANT UNDER TEST IS THAT A FAILURE PRODUCES NO BODY. Every unhappy path
here asserts ``ClassifierBackendError`` rather than a placeholder string,
because a fabricated body does not fail loudly -- it lands in the owner's
drafts folder looking exactly like one the model wrote, and the owner's only
signal that drafting is broken would be a reply they do not remember
approving.

The client is a spy as well as a stub, following ``test_openai_classifier``'s
``SpyClient``: several assertions here are about what was NOT sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from openai import APIConnectionError, OpenAIError

from inbox_zero_connector.classifiers.openai_compatible import (
    COMPOSE_SYSTEM_PROMPT,
    TEMPERATURE,
    ClassifierBackendError,
    OpenAICompatibleClassifier,
)

PROMPT = "Reply to the thread below.\n<thread>Can you send the invoice?</thread>"
"""Stands in for ``drafting.build_body_prompt``'s output.

Opaque to this module on purpose: ``compose`` passes the prompt through
untouched, so the test asserts it arrived verbatim rather than asserting
anything about how it was assembled -- that is ``build_body_prompt``'s own
test, and duplicating it here would couple this file to a module ``compose``
never imports.
"""

BODY = "Sure -- invoice 4021 is attached. Let me know if the PO number is wrong."


@dataclass(frozen=True)
class _FakeMessage:
    """A completion message carrying prose. ``tool_calls`` is absent by design.

    A drafting response has no tool call, and modelling that absence is what
    makes the "no tools kwarg was sent" assertion honest: the stub could not
    return one even if the module asked.
    """

    content: str | None


@dataclass(frozen=True)
class _FakeChoice:
    message: _FakeMessage


@dataclass(frozen=True)
class _FakeResponse:
    choices: list[_FakeChoice]


class SpyClient:
    """Records every outbound call. Only ``chat.completions.create`` exists.

    Same shape as ``test_openai_classifier.SpyClient`` and deliberately a
    separate copy rather than an import: a shared fixture between the two files
    would make a change made for the classification suite silently alter what
    the drafting suite proves. The class is eight lines of stub.
    """

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.chat = self

    @property
    def completions(self) -> SpyClient:
        return self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response

    @property
    def last_call(self) -> dict[str, Any]:
        assert self.calls, "compose never called the backend"
        return self.calls[-1]


def _response(content: str | None) -> _FakeResponse:
    return _FakeResponse([_FakeChoice(_FakeMessage(content))])


def _classifier(client: SpyClient) -> OpenAICompatibleClassifier:
    return OpenAICompatibleClassifier(client=client, model="test-model")


def test_compose_returns_the_model_body_stripped() -> None:
    """The happy path: what the model said, with surrounding whitespace removed.

    Stripping is asserted with padding the model plausibly emits -- a leading
    newline after a system turn is common -- because the body is inserted into
    a draft where a leading blank line is visible to the owner.
    """
    client = SpyClient(_response(f"\n\n{BODY}\n  "))
    assert _classifier(client).compose(PROMPT, voice=object()) == BODY


def test_compose_sends_no_tools_and_the_prompt_as_the_user_turn() -> None:
    """No tool machinery reaches the wire, and the prompt arrives verbatim.

    THE CENTRAL STRUCTURAL ASSERTION. ``classify`` sends ``tools`` and
    ``tool_choice='required'`` on the same client, so a ``compose`` that
    copy-pasted that call would still return a body for a tool-calling model
    and fail only against models without one. Asserting the kwargs are ABSENT
    -- not merely empty -- catches that at the seam.
    """
    client = SpyClient(_response(BODY))
    _classifier(client).compose(PROMPT, voice=object())

    call = client.last_call
    assert "tools" not in call, "compose must offer no tool: a body has no schema"
    assert "tool_choice" not in call, "compose must not constrain the answer shape"
    assert call["temperature"] == TEMPERATURE, "drafting uses the same temperature as classify"
    assert call["messages"] == [
        {"role": "system", "content": COMPOSE_SYSTEM_PROMPT},
        {"role": "user", "content": PROMPT},
    ], "a system turn and the prompt unchanged, in that order"


def test_a_backend_error_raises_rather_than_inventing_a_body() -> None:
    """A failed call is an error the watcher can report, never a draft."""
    client = SpyClient(error=APIConnectionError(request=None))  # type: ignore[arg-type]
    with pytest.raises(ClassifierBackendError) as caught:
        _classifier(client).compose(PROMPT, voice=object())
    assert "ACTION REQUIRED" in str(caught.value), "the operator needs the next step"
    assert BODY not in str(caught.value)


def test_any_sdk_error_is_caught_not_only_connection_errors() -> None:
    """The handler catches ``OpenAIError``, the base -- not one leaf class.

    Pinned separately because catching ``APIConnectionError`` alone would pass
    the test above and still let an authentication or rate-limit error escape
    as an exception type the watcher does not handle, ending the round instead
    of failing one message.
    """
    client = SpyClient(error=OpenAIError("rate limited"))
    with pytest.raises(ClassifierBackendError):
        _classifier(client).compose(PROMPT, voice=object())


@pytest.mark.parametrize(
    ("content", "case"),
    [
        ("", "an empty string"),
        ("   \n\t ", "whitespace only"),
        (None, "a null content field"),
    ],
)
def test_an_empty_body_is_an_error_not_an_empty_draft(content: str | None, case: str) -> None:
    """Nothing usable from the model produces NO draft, rather than a blank one.

    All three spellings of "nothing" are pinned: a model that returns ``""``,
    one that returns whitespace, and a non-conforming server that returns
    ``null``. Returning any of them would create an empty draft in the owner's
    mailbox, which reads as a reply the model chose to leave blank.
    """
    client = SpyClient(_response(content))
    with pytest.raises(ClassifierBackendError, match="ACTION REQUIRED"):
        _classifier(client).compose(PROMPT, voice=object())


def test_a_response_with_no_choices_is_an_error() -> None:
    """An endpoint that answered with an empty envelope is named as such."""
    client = SpyClient(_FakeResponse([]))
    with pytest.raises(ClassifierBackendError, match="no choices"):
        _classifier(client).compose(PROMPT, voice=object())


def test_the_voice_argument_is_accepted_and_never_inspected() -> None:
    """``voice`` satisfies the Protocol and is not read -- proven by a hostile one.

    An object whose every attribute access raises: if ``compose`` ever starts
    reading the profile, this fails immediately. The docstring's claim that the
    prompt already carries the voice is only checkable this way, and it matters
    because the watch side types this ``object`` and hands over a value from an
    add-on this module cannot import.
    """

    class _Exploding:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"compose must not inspect the voice profile ({name!r})")

    client = SpyClient(_response(BODY))
    assert _classifier(client).compose(PROMPT, voice=_Exploding()) == BODY
