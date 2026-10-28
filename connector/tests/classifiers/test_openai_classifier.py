"""The model-backed classifier, driven over a stubbed SDK. No socket, no key.

Every test here builds a ``SpyClient`` and injects it, so the whole module is
exercised with nothing live behind it. That is a property of the module under
test rather than of these fixtures: the constructor takes a ``client``
precisely so the call path can be driven with no endpoint and no credential.

The client is a SPY as well as a stub. Several of these tests assert what was
NOT sent -- specifically that no message body reaches the wire -- and that is
the only way to prove the data boundary the module's docstring claims. The
observable classification is identical either way; the difference is whether
the client's mail left their control, which is the buyer's central objection.

Following ``test_watcher.py``'s ``FakeProvider`` pattern: the unused half of
the surface raises, so a test that accidentally reaches an unstubbed path fails
loudly instead of reading as a pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from openai import APIConnectionError, OpenAIError

from inbox_zero_connector.classifiers.openai_compatible import (
    API_KEY_ENV_VAR,
    CLASSIFY_TOOL_NAME,
    DEFAULT_BASE_URL,
    DEFAULT_BASE_URL_HOST,
    DEFAULT_MODEL,
    LOCAL_PLACEHOLDER_KEY,
    MAX_SNIPPET_CHARS,
    ClassifierBackendError,
    OpenAICompatibleClassifier,
    build_prompt,
)
from inbox_zero_connector.providers.base import Message
from inbox_zero_connector.services.triage.decide import MIN_CONFIDENCE, classify
from inbox_zero_connector.services.triage.taxonomy import Taxonomy, default_taxonomy

FOLDER = "Receipts"
"""A folder that IS in the default taxonomy.

A name outside it would be coerced to ``Review`` by ``classify``, and these
tests would then be asserting the coercion invariant instead of this module's
behaviour -- which is real, and proven separately in ``test_classify.py``.
"""

BODY_MARKER = "SENSITIVE-BODY-TEXT-THAT-MUST-NEVER-BE-SENT"
"""Planted in a field that is not sender/subject/snippet, then searched for.

The data-boundary tests assert this string appears NOWHERE in the outbound
payload. A marker rather than a field-name check because the claim is about
what crosses the wire, not about which attribute the code happened to read.
"""


def _message(**overrides: Any) -> Message:
    """A header projection with every field populated, so omissions are visible."""
    fields: dict[str, Any] = {
        "id": "msg-1",
        "thread_id": "thread-1",
        "sender": "billing@vendor.example",
        "subject": "Invoice 4021 for July",
        "received": "2026-08-18T10:00:00Z",
        "snippet": "Your invoice is attached. Amount due: $420.00.",
        "is_unread": True,
        # NOT flagged: is_important would fire a deterministic rule and the
        # model would never be consulted at all.
        "is_important": False,
        "containers": ("Label_INBOX",),
    }
    fields.update(overrides)
    return Message(**fields)


@dataclass(frozen=True)
class _FakeFunction:
    """The SDK's ``tool_call.function``: a name and a JSON argument string."""

    name: str
    arguments: str


@dataclass(frozen=True)
class _FakeToolCall:
    function: _FakeFunction


@dataclass(frozen=True)
class _FakeMessage:
    """A completion message. ``content`` is populated even when a tool was called.

    Deliberate: a real response carries both, and a module that read prose when
    a tool call was present would pass a stub that left content empty.
    """

    tool_calls: list[_FakeToolCall] | None
    content: str = "prose the module must refuse to parse"


@dataclass(frozen=True)
class _FakeChoice:
    message: _FakeMessage


@dataclass(frozen=True)
class _FakeResponse:
    choices: list[_FakeChoice]


def _tool_response(
    folder: str = FOLDER,
    confidence: float = 0.95,
    reason: str = "Invoice recording a payment due.",
    *,
    name: str = CLASSIFY_TOOL_NAME,
    raw_arguments: str | None = None,
) -> _FakeResponse:
    """Build a well-formed tool-call completion, or a deliberately broken one."""
    arguments = raw_arguments or json.dumps({
        "folder": folder,
        "confidence": confidence,
        "reason": reason,
    })
    call = _FakeToolCall(_FakeFunction(name, arguments))
    return _FakeResponse([_FakeChoice(_FakeMessage([call]))])


class SpyClient:
    """Stands in for ``openai.OpenAI``, recording every outbound call.

    Only ``chat.completions.create`` is implemented, because that is the only
    method this module reaches. Anything else raising is the point: a module
    that started calling a second endpoint would fail here rather than pass
    silently.
    """

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response if response is not None else _tool_response()
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
        assert self.calls, "the classifier never called the backend"
        return self.calls[-1]

    def payload_text(self) -> str:
        """Every string that crossed the wire, flattened. THE boundary assertion."""
        return json.dumps(self.last_call, default=str)


def _classifier(client: SpyClient, **kwargs: Any) -> OpenAICompatibleClassifier:
    return OpenAICompatibleClassifier(client=client, **kwargs)


# ---------------------------------------------------------------------------
# A valid response becomes a Classification.
# ---------------------------------------------------------------------------


def test_a_tool_call_becomes_a_classification(installed_taxonomy: None) -> None:
    """The happy path: the model calls the tool, we return what it said."""
    client = SpyClient(_tool_response(folder=FOLDER, confidence=0.95, reason="An invoice."))
    result = _classifier(client).classify(_message(), default_taxonomy().folders)

    assert result.folder == FOLDER
    assert result.confidence == pytest.approx(0.95)
    assert result.reason == "An invoice."


def test_the_result_survives_the_coercion_invariant(installed_taxonomy: None) -> None:
    """End to end through ``classify``: a legal, confident answer is preserved.

    Proves this module's output is actually usable by the decision layer,
    rather than merely well-formed on its own.
    """
    client = SpyClient(_tool_response(folder=FOLDER, confidence=0.95))
    result = classify(_message(), default_taxonomy(), _classifier(client))

    assert result.folder == FOLDER
    assert result.deterministic is False


# ---------------------------------------------------------------------------
# deterministic is always False.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.95, 1.0])
def test_deterministic_is_always_false(installed_taxonomy: None, confidence: float) -> None:
    """A model result may NEVER claim to be a rule, at any confidence.

    Parametrized across the range because the field is set unconditionally and
    a regression that tied it to confidence would pass a single-value test.
    """
    client = SpyClient(_tool_response(confidence=confidence))
    result = _classifier(client).classify(_message(), default_taxonomy().folders)

    assert result.deterministic is False


# ---------------------------------------------------------------------------
# The prompt is built from the LIVE taxonomy descriptions.
# ---------------------------------------------------------------------------


def test_the_prompt_carries_the_taxonomy_descriptions(installed_taxonomy: None) -> None:
    """The descriptions ARE the decision criteria. They must reach the model."""
    taxonomy = default_taxonomy()
    client = SpyClient()
    _classifier(client).classify(_message(), taxonomy.folders)

    sent = client.payload_text()
    for folder in taxonomy.folders:
        assert folder in sent, f"folder {folder!r} missing from the prompt"
    # A distinctive clause from the Business-Waiting description. Asserting on
    # the TEXT, not just the names, is what proves the criteria travelled.
    assert "whose court the ball is in" in sent


def test_the_prompt_reads_the_installed_taxonomy_not_a_hardcoded_copy(
    installed_taxonomy: None,
) -> None:
    """THE anti-hardcode test. A custom taxonomy must reach the model verbatim.

    Written against ``build_prompt`` with a taxonomy that shares no folder name
    with the default set: a module carrying a baked-in copy of the descriptions
    would still emit the default nine and fail every assertion here.
    """
    custom = Taxonomy(
        folders=("Litigation", "Filings", "Triage-Me"),
        descriptions={
            "Litigation": "Active matters with a court deadline the attorney owns.",
            "Filings": "Documents already submitted to the court. Nothing to do.",
            "Triage-Me": "Anything this firm's classifier is not sure about.",
        },
        # Every Taxonomy declares the folder that absorbs doubt, and this one
        # renames it along with everything else -- which is the point: sharing
        # no name with the default set has to include the uncertainty folder.
        review_folder="Triage-Me",
    )
    prompt = build_prompt(_message(), custom)

    assert "Active matters with a court deadline the attorney owns." in prompt
    assert "Documents already submitted to the court." in prompt
    # And the default vocabulary must be absent -- the proof there is no copy.
    assert "Business-Waiting" not in prompt
    assert "Newsletters" not in prompt


# ---------------------------------------------------------------------------
# THE DATA BOUNDARY: sender, subject, snippet. Never a body.
# ---------------------------------------------------------------------------


def test_only_sender_subject_and_snippet_are_sent(installed_taxonomy: None) -> None:
    """The three fields go. The buyer's objection is answered by this test."""
    client = SpyClient()
    message = _message(
        sender="billing@vendor.example",
        subject="Invoice 4021 for July",
        snippet="Amount due: $420.00.",
    )
    _classifier(client).classify(message, default_taxonomy().folders)

    sent = client.payload_text()
    assert "billing@vendor.example" in sent
    assert "Invoice 4021 for July" in sent
    assert "Amount due: $420.00." in sent


def test_no_other_message_field_reaches_the_wire(installed_taxonomy: None) -> None:
    """Everything that is NOT one of the three stays home.

    ``Message`` carries no body field at all -- bodies live behind
    ``get_thread``, which this module never calls -- so the marker is planted
    in every OTHER field instead. A module that serialised the whole model
    (``message.model_dump()``) rather than naming three fields would ship all
    of them, and that is exactly the regression this catches.
    """
    client = SpyClient()
    message = _message(
        id=BODY_MARKER,
        thread_id=BODY_MARKER,
        received=BODY_MARKER,
        containers=(BODY_MARKER,),
    )
    _classifier(client).classify(message, default_taxonomy().folders)

    assert BODY_MARKER not in client.payload_text()


def test_the_message_model_carries_no_body_to_send(installed_taxonomy: None) -> None:
    """The boundary is structural, not a choice this module makes each call.

    "We send a snippet, not the mail" is only truthful if there is no mail here
    to send. Asserted against the model rather than the prompt so the claim is
    about what this code CAN reach.
    """
    assert "body" not in Message.model_fields
    assert "html" not in Message.model_fields


def test_an_oversized_snippet_is_truncated(installed_taxonomy: None) -> None:
    """Bounds both the mail leaving and the hostile text arriving.

    The marker is planted PAST ``MAX_SNIPPET_CHARS`` and a second marker before
    it, so the test proves the cut happens at the boundary rather than that the
    snippet was dropped entirely -- a module sending no snippet at all would
    pass a one-sided version of this.
    """
    client = SpyClient()
    kept = "KEPT-INSIDE-THE-LIMIT"
    message = _message(
        snippet=kept + "A" * MAX_SNIPPET_CHARS + BODY_MARKER,
    )
    _classifier(client).classify(message, default_taxonomy().folders)

    sent = client.payload_text()
    assert kept in sent, "the snippet must still be sent, just bounded"
    assert BODY_MARKER not in sent


# ---------------------------------------------------------------------------
# Base URL and model are CONFIG, not hardcoded. THE agnostic property.
# ---------------------------------------------------------------------------


def test_the_configured_model_is_what_gets_sent(installed_taxonomy: None) -> None:
    """The model id reaching the wire is the configured one, not a constant."""
    client = SpyClient()
    _classifier(client, model="Qwen3.8-27B-4bit").classify(_message(), default_taxonomy().folders)

    assert client.last_call["model"] == "Qwen3.8-27B-4bit"
    assert client.last_call["model"] != DEFAULT_MODEL


def test_no_endpoint_is_baked_into_the_module() -> None:
    """There is NO built-in base URL. The caller supplies it, always.

    The agnostic property in its strongest form: a module with no default
    endpoint cannot quietly inherit one deployment's infrastructure. See
    ``DEFAULT_BASE_URL`` for why this is None rather than the local server, and
    what the wiring lane is expected to do about it.
    """
    instance = OpenAICompatibleClassifier(client=SpyClient())

    assert DEFAULT_BASE_URL is None
    assert instance.base_url is None


def test_the_documented_local_default_is_recorded_without_a_scheme() -> None:
    """The expected endpoint is stated, as a host, not as a url.

    Scheme-less so the containment scan in ``test_containment.py`` keeps
    owning the egress allowlist -- a classifier lane does not get to widen the
    connector's reachable-host set by writing a literal.
    """
    assert DEFAULT_BASE_URL_HOST == "localhost:11434"
    assert not DEFAULT_BASE_URL_HOST.startswith("http")
    assert DEFAULT_MODEL == "Qwen3-4B-Instruct-2507-4bit"


def test_a_hosted_endpoint_is_a_config_change_and_nothing_else() -> None:
    """Pointing at OpenRouter changes three values and touches no code path."""
    instance = OpenAICompatibleClassifier(
        "sk-real-key",
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-4o-mini",
        requires_key=True,
        client=SpyClient(),
    )

    assert instance.base_url == "https://openrouter.ai/api/v1"
    assert instance.model == "openai/gpt-4o-mini"
    assert instance.requires_key is True


# ---------------------------------------------------------------------------
# The credential: optional for a local endpoint, loud when actually required.
# ---------------------------------------------------------------------------


def test_a_missing_required_key_fails_loudly_naming_the_env_var() -> None:
    """An endpoint that needs a key and has none fails at STARTUP, not at 401."""
    with pytest.raises(ClassifierBackendError) as excinfo:
        OpenAICompatibleClassifier("", base_url="https://api.openai.com/v1", requires_key=True)

    message = str(excinfo.value)
    assert API_KEY_ENV_VAR in message
    assert "https://api.openai.com/v1" in message
    assert "ACTION REQUIRED" in message


def test_a_whitespace_only_key_counts_as_missing() -> None:
    """A key of spaces is a key nobody set, and must fail the same way."""
    with pytest.raises(ClassifierBackendError):
        OpenAICompatibleClassifier("   ", requires_key=True)


def test_a_keyless_local_endpoint_constructs_fine() -> None:
    """The default is a local server needing no credential. It must not fail.

    The mirror image of the test above, and the reason it exists: a rule that
    rejected every keyless construction would make the local default -- the
    whole privacy claim -- unusable.
    """
    instance = OpenAICompatibleClassifier(client=SpyClient())

    assert instance.requires_key is False


def test_the_placeholder_key_is_not_key_shaped() -> None:
    """A stand-in credential must be unmistakable in any log or bug report."""
    assert not LOCAL_PLACEHOLDER_KEY.startswith("sk-")
    assert "not-required" in LOCAL_PLACEHOLDER_KEY


# ---------------------------------------------------------------------------
# Failure raises. It never guesses.
# ---------------------------------------------------------------------------


def test_an_api_error_raises_rather_than_guessing(installed_taxonomy: None) -> None:
    """A backend outage becomes a FAILED receipt, never a fabricated folder."""
    failing = SpyClient(error=APIConnectionError(request=None))  # type: ignore[arg-type]  # the SDK types this as httpx.Request; the stub never reads it
    with pytest.raises(ClassifierBackendError) as excinfo:
        _classifier(failing).classify(_message(), default_taxonomy().folders)

    assert "No folder was guessed" in str(excinfo.value)


def test_any_sdk_error_is_caught_at_the_base_class(installed_taxonomy: None) -> None:
    """``OpenAIError`` is the SDK's base; catching it covers every subclass."""
    failing = SpyClient(error=OpenAIError("upstream exploded"))
    with pytest.raises(ClassifierBackendError):
        _classifier(failing).classify(_message(), default_taxonomy().folders)


def test_prose_instead_of_a_tool_call_raises_naming_the_model(
    installed_taxonomy: None,
) -> None:
    """NO PROSE FALLBACK. A parser that usually works misfiles mail silently."""
    client = SpyClient(_FakeResponse([_FakeChoice(_FakeMessage(None))]))
    with pytest.raises(ClassifierBackendError) as excinfo:
        _classifier(client, model="tiny-no-tools").classify(_message(), default_taxonomy().folders)

    message = str(excinfo.value)
    assert "tiny-no-tools" in message
    assert "will NOT parse prose" in message


def test_an_empty_choices_list_raises(installed_taxonomy: None) -> None:
    """A response with nothing in it is a broken endpoint, not a classification."""
    client = SpyClient(_FakeResponse([]))
    with pytest.raises(ClassifierBackendError) as excinfo:
        _classifier(client).classify(_message(), default_taxonomy().folders)

    assert "no choices" in str(excinfo.value)


def test_a_wrong_tool_name_raises(installed_taxonomy: None) -> None:
    """Only one tool was offered. Calling another is a provider fault."""
    client = SpyClient(_tool_response(name="some_other_function"))
    with pytest.raises(ClassifierBackendError) as excinfo:
        _classifier(client).classify(_message(), default_taxonomy().folders)

    assert "some_other_function" in str(excinfo.value)


def test_malformed_tool_arguments_raise(installed_taxonomy: None) -> None:
    """Invalid JSON in the arguments is a model fault, not a folder."""
    client = SpyClient(_tool_response(raw_arguments="{not json at all"))
    with pytest.raises(ClassifierBackendError) as excinfo:
        _classifier(client).classify(_message(), default_taxonomy().folders)

    assert "not valid JSON" in str(excinfo.value)


def test_tool_arguments_missing_a_field_raise(installed_taxonomy: None) -> None:
    """A partial tool call fails at the boundary with the field named."""
    client = SpyClient(_tool_response(raw_arguments=json.dumps({"folder": FOLDER})))
    with pytest.raises(ClassifierBackendError) as excinfo:
        _classifier(client).classify(_message(), default_taxonomy().folders)

    assert "cannot use" in str(excinfo.value)


def test_an_out_of_range_confidence_raises(installed_taxonomy: None) -> None:
    """5.0 would clear the floor and file confidently. It is refused at the edge."""
    client = SpyClient(
        _tool_response(
            raw_arguments=json.dumps({"folder": FOLDER, "confidence": 5.0, "reason": "x"})
        )
    )
    with pytest.raises(ClassifierBackendError):
        _classifier(client).classify(_message(), default_taxonomy().folders)


# ---------------------------------------------------------------------------
# The coercion invariant is NOT re-implemented here.
# ---------------------------------------------------------------------------


def test_an_out_of_taxonomy_folder_is_returned_faithfully(
    installed_taxonomy: None,
) -> None:
    """This module does NOT sanitise. ``classify.coerce`` owns that rule.

    Cleaning it here would hide from the ledger that the model proposed
    something illegal -- which is the signal a prompt injection produces.
    """
    client = SpyClient(_tool_response(folder="Attacker-Controlled-Folder"))
    result = _classifier(client).classify(_message(), default_taxonomy().folders)

    assert result.folder == "Attacker-Controlled-Folder"


def test_the_decision_layer_coerces_what_this_module_returned(
    installed_taxonomy: None,
) -> None:
    """And the invariant still fires upstream, with the suggestion preserved."""
    client = SpyClient(_tool_response(folder="Attacker-Controlled-Folder"))
    result = classify(_message(), default_taxonomy(), _classifier(client))

    assert result.folder == "Review"
    assert "Attacker-Controlled-Folder" in result.reason


def test_a_low_confidence_answer_is_returned_unmodified(
    installed_taxonomy: None,
) -> None:
    """The floor belongs to ``classify``, and is not duplicated here."""
    low = MIN_CONFIDENCE - 0.1
    client = SpyClient(_tool_response(confidence=low))
    result = _classifier(client).classify(_message(), default_taxonomy().folders)

    assert result.confidence == pytest.approx(low)


# ---------------------------------------------------------------------------
# Tool calling is actually requested.
# ---------------------------------------------------------------------------


def test_tool_calling_is_required_on_the_request(installed_taxonomy: None) -> None:
    """Structured output is requested via tools, not hoped for from prose."""
    client = SpyClient()
    _classifier(client).classify(_message(), default_taxonomy().folders)

    call = client.last_call
    assert call["tool_choice"] == "required"
    assert call["tools"][0]["function"]["name"] == CLASSIFY_TOOL_NAME


def test_the_tool_schema_constrains_the_folder_to_the_installed_taxonomy(
    installed_taxonomy: None,
) -> None:
    """The enum is built from the live vocabulary, so a provider can enforce it."""
    client = SpyClient()
    taxonomy = default_taxonomy()
    _classifier(client).classify(_message(), taxonomy.folders)

    schema = client.last_call["tools"][0]["function"]["parameters"]
    assert schema["properties"]["folder"]["enum"] == list(taxonomy.folders)
