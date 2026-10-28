"""No log line may contain a message body, subject, address, or token.

THE TEST THAT MATTERS IN THIS FILE is
``test_a_full_sweep_writes_no_mail_content_to_any_sink``: it drives a whole
mocked mailbox through the connector -- list, read a thread, sample sent mail,
file, flag, draft -- with every logging sink captured, then asserts that no
line anywhere contains the mock's body text, subject lines, or addresses.

Why a sweep rather than per-call assertions: the leak this guards against is
not a call site that obviously logs a body. It is a call site that logs
something innocuous-looking which happens to CARRY one -- a whole response
dict, a repr of a model, an exception whose message interpolated the server's
reply. A sweep with distinctive sentinel strings catches all of those without
having to predict which one somebody will write next.

The sentinels are deliberately weird (``ZZQX``-prefixed) so a match cannot be
a coincidence and a failure names exactly which value escaped.

The G3 block at the end covers the two ROUTES content can take to a sink -- a
bound field and the message string -- with MULTI-WORD values, which is where
the round-1 message pass failed: its value group matched one whitespace-
delimited token, so it blanked the first word of a subject and shipped the
rest. It also pins ``find_content_interpolations``, the AST guard that keeps
content out of a message string in the first place, in both directions: proven
to FAIL on each format spelling it claims to cover, and proven not to refuse
the ``key=value`` discipline the package actually logs with.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from loguru import logger

from inbox_zero_connector.apps.server import main as server
from inbox_zero_connector.config import ConnectorConfig, Posture, VoiceRouting, load_settings
from inbox_zero_connector.providers.base import (
    Container,
    DraftResult,
    MailProvider,
    Message,
    SentMessage,
    SentSelection,
    Thread,
)
from inbox_zero_connector.utils.logging_config import (
    ProviderCall,
    ToolCall,
    find_content_interpolations,
    is_sensitive_field,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# Canary values. Every one stands for private content that must NEVER reach a
# sink. Named CANARY_*, not SECRET_*, so flake8-bandit's hardcoded-credential
# rule stays ON for the rest of the suite rather than being suppressed for the
# one file whose whole job is to prove credentials do not leak.
CANARY_BODY = "ZZQX-BODY-the-settlement-figure-is-four-hundred-thousand"
CANARY_SUBJECT = "ZZQX-SUBJECT-Ferraro-matter-mediation"
CANARY_ADDRESS = "ZZQX-ADDRESS-opposing.counsel@rival.example"
CANARY_SNIPPET = "ZZQX-SNIPPET-please-review-before-Friday"
CANARY_SENT_BODY = "ZZQX-SENTBODY-as-discussed-our-position-remains"
# Stands in for an OAuth token. The NAME deliberately avoids "token"/"secret"
# so flake8-bandit does not flag the canary itself; the VALUE is what the
# redaction tests look for in the log.
CANARY_CREDENTIAL_SHAPED = "ZZQX-CREDVALUE-ya29-authenticates-nothing"

# Not a credential. The Graph tests below run against httpx.MockTransport, so
# this string is never sent anywhere and authenticates nothing -- it exists
# only to satisfy the adapter's "do I already have a token" branch without
# touching the OS keychain. Named, and named WITHOUT a credential-shaped word,
# so flake8-bandit's hardcoded-password rule stays ON for the rest of the file.
MOCK_TRANSPORT_SENTINEL = "mock-transport-only-authenticates-nothing"

ALL_CANARIES = (
    CANARY_BODY,
    CANARY_SUBJECT,
    CANARY_ADDRESS,
    CANARY_SNIPPET,
    CANARY_SENT_BODY,
    CANARY_CREDENTIAL_SHAPED,
)


class LeakyProvider(MailProvider):
    """A mailbox whose every field is a sentinel. Nothing here may be logged."""

    name = "google"

    def authenticate(self, interactive: bool = False) -> str:
        return "owner@example.com"

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[Message]:
        return [
            Message(
                id="m1",
                thread_id="t1",
                sender=CANARY_ADDRESS,
                subject=CANARY_SUBJECT,
                received="2026-08-17T10:00:00Z",
                snippet=CANARY_SNIPPET,
            )
        ]

    def get_thread(self, thread_id: str) -> Thread:
        return Thread(
            id=thread_id,
            subject=CANARY_SUBJECT,
            messages=[
                Message(
                    id="m1",
                    thread_id=thread_id,
                    sender=CANARY_ADDRESS,
                    subject=CANARY_SUBJECT,
                    received="2026-08-17T10:00:00Z",
                    snippet=CANARY_SNIPPET,
                )
            ],
            bodies=[CANARY_BODY],
            participants=[CANARY_ADDRESS, "owner@example.com"],
            labels=["Prospects"],
        )

    def list_containers(self) -> list[Container]:
        raise NotImplementedError

    def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
        return [
            SentMessage(
                id="s1",
                thread_id="t1",
                recipients=[CANARY_ADDRESS],
                subject=CANARY_SUBJECT,
                sent="2026-08-17T09:00:00Z",
                body=CANARY_SENT_BODY,
            )
        ]

    def file_message(self, message_id: str, folder: str) -> str:
        return f"InboxZero/{folder}"

    def poll_changes(self, cursor, *, page_token=None, container_id=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def tag_message(self, message_id: str, label: str) -> str:
        return label

    def set_important(self, message_id: str, important: bool = True) -> str:
        return "IMPORTANT"

    def create_draft(self, thread_id: str, body: str) -> DraftResult:
        return DraftResult(draft_id="d1", thread_id=thread_id, recipients=[CANARY_ADDRESS])

    def create_self_draft(self, subject: str, body: str) -> str:
        return "d1"


@pytest.fixture
def sink() -> Iterator[list[str]]:
    """Capture every log line the process emits, at DEBUG.

    Captures the RENDERED line plus the bound extras, so a value that escaped
    through a bound field is caught as surely as one in the message.

    ``load_settings()`` is called FIRST, on purpose. It configures the real
    sinks, and ``logging_config.setup`` starts with ``logger.remove()`` -- so a
    capture sink added before that call is torn down mid-test, and the sweep
    then asserts over an empty list, which passes while proving nothing. Load
    the real configuration, THEN attach the probe on top of it.

    Teardown removes by id and tolerates the sink already being gone, because a
    test that triggers a reconfiguration legitimately removes it.
    """
    load_settings()

    captured: list[str] = []

    def write(message: Any) -> None:
        captured.append(str(message))
        captured.append(repr(message.record["extra"]))

    # diagnose=False mirrors the app's own sinks, which force it off on all of
    # them: loguru's default is True, and it renders each frame's local VALUES,
    # so a probe built on the default would leak the very content it is here to
    # prove absent -- and would be reporting on a sink shape the app never uses.
    sink_id = logger.add(write, level="DEBUG", format="{message}", diagnose=False)
    try:
        yield captured
    finally:
        try:
            logger.remove(sink_id)
        except ValueError:
            # Already removed by a reconfiguration inside the test. Logged
            # rather than swallowed silently: if this fires unexpectedly, the
            # capture stopped early and the assertions saw less than they think.
            logger.debug("SINK: capture sink was already removed by a reconfiguration")


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None) -> Iterator[LeakyProvider]:
    """Point the server at the sentinel mailbox with drafts and routing on."""
    provider = LeakyProvider()
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_owner", "owner@example.com")
    monkeypatch.setattr(
        server,
        "_config",
        ConnectorConfig(
            posture=Posture.FILE_READ_DRAFT,
            drafts_enabled={"google": True},
            routing=VoiceRouting(owner_domain="example.com", sales_labels=("Prospects",)),
        ),
    )
    yield provider


def test_a_full_sweep_writes_no_mail_content_to_any_sink(
    wired: LeakyProvider, sink: list[str]
) -> None:
    """THE redaction test: drive the whole connector, assert nothing leaked."""
    server.list_inbox(limit=10)
    server.get_thread("t1")
    server.read_sent_sample(after="2026-01-01")
    server.file_message("m1", "Business-Action")
    server.mark_important("m1", important=True)
    server.create_draft("t1", "a reply the owner will read before sending")
    server.get_voice_routing()
    server.get_daily_digest()

    assert sink, "the sweep produced no log lines at all -- the test proves nothing"

    blob = "\n".join(sink)
    leaked = [secret for secret in ALL_CANARIES if secret in blob]
    assert leaked == [], f"private mail content reached a log sink: {leaked}"


def test_the_sweep_did_log_something_diagnosable(wired: LeakyProvider, sink: list[str]) -> None:
    """Redaction must not be achieved by logging nothing.

    The complement of the test above, and the reason it is a separate test: a
    logger that emitted no lines would pass the leak assertion perfectly while
    destroying the observability the standard requires. This asserts the five
    points are actually present.
    """
    server.file_message("m1", "Business-Action")

    blob = "\n".join(sink)
    assert "TOOL: file_message" in blob, "no tool line -- the five points are missing"
    assert "phase=start" in blob, "no ENTRY point"
    assert "phase=ok" in blob, "no EXIT point"
    assert "message_id=m1" in blob, "identifiers must be logged; they are not content"
    assert "folder=Business-Action" in blob, "the taxonomy folder is a closed-set name, not content"


def test_a_failing_provider_call_logs_the_failure_without_the_content(
    wired: LeakyProvider, sink: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The FAILURE point fires, carries a traceback, and still leaks nothing.

    Exception paths are where content leaks: an error message that interpolated
    a provider response carries whatever that response held.
    """

    def explode(message_id: str, folder: str) -> str:
        raise RuntimeError(f"upstream said: {CANARY_BODY}")

    monkeypatch.setattr(wired, "file_message", explode)
    with pytest.raises(RuntimeError):
        server.file_message("m1", "Business-Action")

    blob = "\n".join(sink)
    assert "phase=failed" in blob, "the FAILURE point did not fire"
    assert "error_type=RuntimeError" in blob, "the failure line must name the error type"
    assert CANARY_BODY not in blob, "an exception message carried mail content into the log"


def test_a_graph_error_echoing_the_draft_body_never_reaches_an_exception_or_a_sink(
    sink: list[str],
) -> None:
    """T5: an upstream error body is UNTRUSTED text, and a length bound is not redaction.

    ``create_draft`` POSTs the owner's reply as ``{"comment": body}``. Graph,
    a proxy, or a WAF in front of either routinely echoes the offending request
    content back in the error payload -- so a failure response can carry the
    draft body verbatim. Copying an excerpt of that response into the
    ``ProviderError`` message put private mail into an exception that
    ``ProviderCall`` then logs with ``exc_info=True``, i.e. into every sink.

    Truncating to 300 characters bounded the SIZE of the leak, not the leak: a
    subject line and the opening of a body fit comfortably inside 300 chars.
    The fix is that no free-text upstream excerpt travels at all; the exception
    carries the status, the Graph error ``code``, and the request id instead --
    which is what actually diagnoses a Graph failure.
    """
    import httpx

    from inbox_zero_connector.providers.base import ProviderError
    from inbox_zero_connector.providers.outlook.provider import GRAPH_BASE, OutlookProvider

    def echoes_the_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"request-id": "req-abc-123"},
            json={
                "error": {
                    "code": "ErrorInvalidRecipients",
                    "message": f"The request body was rejected: {CANARY_BODY}",
                }
            },
        )

    client = httpx.Client(base_url=GRAPH_BASE, transport=httpx.MockTransport(echoes_the_body))
    provider = OutlookProvider(client=client, token=MOCK_TRANSPORT_SENTINEL)

    with pytest.raises(ProviderError) as caught:
        provider.list_inbox()

    assert CANARY_BODY not in str(caught.value), (
        "the upstream error excerpt carried mail content into the exception message"
    )
    blob = "\n".join(sink)
    assert CANARY_BODY not in blob, "the upstream error excerpt reached a log sink"
    # Complement: dropping the excerpt must not drop DIAGNOSABILITY with it.
    assert "400" in str(caught.value), "the status must survive"
    assert "ErrorInvalidRecipients" in str(caught.value), "the Graph error code must survive"
    assert "req-abc-123" in str(caught.value), "the request id must survive"


def test_explicitly_passing_a_body_field_is_redacted_not_logged(sink: list[str]) -> None:
    """The backstop: a future call site that passes content by name is caught.

    The primary defence is that no call site passes content. This proves the
    second layer works, so the day somebody adds ``body=...`` to a log call the
    value does not ship.
    """
    with ProviderCall("google", "probe", body=CANARY_BODY, subject=CANARY_SUBJECT) as call:
        call.guard("probe", address=CANARY_ADDRESS)

    blob = "\n".join(sink)
    assert CANARY_BODY not in blob
    assert CANARY_SUBJECT not in blob
    assert CANARY_ADDRESS not in blob
    assert "[redacted]" in blob, "the redaction marker should say a value was withheld"


def test_a_token_shaped_field_is_redacted(sink: list[str]) -> None:
    """Credentials, the standard's own minimum set, on this package's wrapper."""
    with ToolCall(
        "probe", access_token=CANARY_CREDENTIAL_SHAPED, client_secret=CANARY_CREDENTIAL_SHAPED
    ):
        pass

    assert CANARY_CREDENTIAL_SHAPED not in "\n".join(sink)


# ---------------------------------------------------------------------------
# G3 -- a MULTI-WORD value may not reach a sink by either route
# ---------------------------------------------------------------------------

CANARY_MULTIWORD_SUBJECT = "ZZQX-SUBJ Ferraro matter mediation continued"
CANARY_MULTIWORD_BODY = "ZZQX-BOD the settlement figure is four hundred thousand"


def test_a_multiword_value_in_a_message_string_is_redacted_whole(sink: list[str]) -> None:
    """G3, at a real sink: the round-1 pass leaked every word after the first.

    Its value group was ``\\S+`` -- ONE whitespace-delimited run -- so
    ``subject=Ferraro matter mediation`` rendered as ``subject=[redacted]
    matter mediation`` and shipped the rest of the subject to all three sinks.
    A multi-word subject or body is the ordinary case, so the pass leaked
    almost everything it claimed to withhold.

    Asserted on the WORDS AFTER THE FIRST, not just the whole string, because
    the whole string was already absent under the broken version -- the first
    token was correctly blanked, which is exactly what made the leak look like
    a pass.
    """
    logger.info(f"PROBE: subject={CANARY_MULTIWORD_SUBJECT}")
    logger.warning(f"PROBE: body={CANARY_MULTIWORD_BODY} message_id=m1")

    blob = "\n".join(sink)
    assert "matter mediation continued" not in blob, "the subject's tail reached a sink"
    assert "four hundred thousand" not in blob, "the body's tail reached a sink"
    assert CANARY_MULTIWORD_SUBJECT not in blob
    assert CANARY_MULTIWORD_BODY not in blob
    assert "[redacted]" in blob, "the marker should say a value was withheld"


@pytest.mark.parametrize(
    ("message", "leaked", "kept"),
    [
        # A bare multi-word value runs to end of line.
        ("P: subject=Ferraro matter mediation", "matter mediation", None),
        # A quoted value keeps its internal separators.
        ('P: body="four hundred, thousand" id=1', "thousand", "id=1"),
        ("P: subject='quoted multi word' message_id=m9", "multi word", "message_id=m9"),
        # Comma and semicolon end a bare value, so the NEXT field survives.
        ("P: subject=A matter, folder=Action", "A matter", "folder=Action"),
        ("P: subject=A matter; folder=Action", "A matter", "folder=Action"),
        # A greedy bare value swallows following pairs; they must still be
        # judged on their own names rather than hidden inside the redaction.
        ("P: subject=Long Multi Word phase=ok ms=4", "Long Multi Word", "phase=ok"),
        ("P: subject=Long Multi Word phase=ok ms=4", "Long Multi Word", "ms=4"),
    ],
)
def test_the_message_pass_covers_the_value_shapes_it_claims(
    sink: list[str], message: str, leaked: str, kept: str | None
) -> None:
    """The shapes the docstring promises: multi-word, quoted, separator-delimited.

    ``kept`` is asserted alongside because over-redaction passes a leak test
    perfectly while destroying the diagnostics: a pass that swallowed the rest
    of the line would look identical here without it.
    """
    logger.info(message)

    blob = "\n".join(sink)
    assert leaked not in blob, f"a sensitive value reached a sink from {message!r}"
    if kept is not None:
        assert kept in blob, f"a non-sensitive field was destroyed in {message!r}"


def test_multiword_content_cannot_reach_a_sink_as_a_bound_field(sink: list[str]) -> None:
    """The OTHER route. Content bound by name is redacted by name, reliably."""
    with ProviderCall(
        "google", "probe", subject=CANARY_MULTIWORD_SUBJECT, body=CANARY_MULTIWORD_BODY
    ) as call:
        call.guard("probe", subject=CANARY_MULTIWORD_SUBJECT)

    blob = "\n".join(sink)
    assert "matter mediation continued" not in blob
    assert "four hundred thousand" not in blob


def test_the_guard_refuses_content_in_a_message_string() -> None:
    """THE STRUCTURAL GUARD: content cannot enter a message string at all.

    The redaction pass above is a backstop with a real limit -- it covers the
    ``name=value`` shape and nothing else, because free prose carries no name
    to match and a rule that blanked unnamed text would blank real diagnostics.
    So the primary mechanism is not "sanitise the message better"; it is that a
    content-bearing value never gets built into a message in the first place,
    which makes the parsing problem not exist.

    It walks the AST of EVERY file in the package rather than checking known
    call sites. That is the point: round 2 of this kit's review found guards
    applied at call sites and bypassed by a path that was not on the list. A
    file added tomorrow is covered by this without anybody remembering it.
    """
    package = Path(server.__file__).resolve().parent

    offenders = find_content_interpolations(package)

    assert offenders == [], (
        "a logging call builds content into its message string, where redaction "
        f"by field name cannot reach it: {offenders}"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        'logger.info(f"X: upstream said {body}")',
        'logger.warning(f"X: {subject} arrived")',
        'logger.error("X: %s failed" % body)',
        'logger.info("X: {}".format(message.subject))',
        'logger.debug(f"X: {thread.snippet}")',
        # A wrapper method whose first argument is folded into the message.
        'call.fallback(f"degraded on {subject}")',
        'call.guard(f"clamped {body}")',
        # LOGURU'S OWN BRACE FORMATTING. `logger.info("x {}", body)` renders
        # `body` INTO the message exactly as an f-string would, and a guard
        # that read only the first argument saw a clean literal and passed.
        'logger.info("X: upstream said {}", body)',
        'logger.warning("X: {} arrived", message.subject)',
        'logger.error("X: {sub} failed", sub=subject)',
        # logger.log's message is the SECOND argument; the first is the level.
        'logger.log("INFO", f"X: upstream said {body}")',
        'logger.log("WARNING", "X: {}", body)',
        # logger.opt(...) returns a logger, so the same two shapes apply.
        'logger.opt(exception=True).error(f"X: {body}")',
        'logger.opt(lazy=True).info("X: {}", body)',
    ],
)
def test_the_guard_is_not_vacuous(tmp_path: Path, snippet: str) -> None:
    """Every spelling the guard claims to cover, proven to FAIL the guard.

    Without this, ``find_content_interpolations`` returning ``[]`` on a clean
    package is indistinguishable from a function that returns ``[]`` always --
    and a guard that cannot fail is not a guard. The three format spellings are
    listed separately because they render identically at the sink: catching the
    f-string and missing ``%`` would leave the same leak under another name.
    """
    (tmp_path / "probe.py").write_text(f"def f():\n    {snippet}\n", encoding="utf-8")

    assert find_content_interpolations(tmp_path), f"the guard missed {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        'logger.info(f"X: subject={subject}")',  # name attached -- redactable
        'logger.info(f"X: body_len={len(body)}")',  # a count, not content
        'logger.debug(f"X: error_type={type(exc).__name__}")',
        'logger.info(f"X: folder={folder} message_id={message_id}")',
        'logger.warning(f"X: cap={MAX_BODY_CHARS}")',  # a module constant
        'logger.info("X: a literal sentence")',
        # Loguru brace formatting carrying an id or a count is the shape the
        # standard ASKS for, so widening the guard must not refuse it.
        'logger.info("X: message_id={}", message_id)',
        'logger.info("X: body_len={}", len(body))',
        # logger.log's LEVEL argument is a level name, never a message. A guard
        # that scanned every positional arg blindly would judge it as content.
        'logger.log("INFO", "X: a literal sentence")',
        'logger.log(level, "X: message_id={}", message_id)',
    ],
)
def test_the_guard_allows_the_discipline_the_package_uses(tmp_path: Path, snippet: str) -> None:
    """The complement: the guard must not refuse the logging the standard wants.

    A guard that flagged ``body_len`` or ``subject={subject}`` would be
    satisfied only by logging nothing, and the observability standard requires
    five points per operation. These are the shapes the package actually uses.
    """
    (tmp_path / "probe.py").write_text(f"def f():\n    {snippet}\n", encoding="utf-8")

    assert find_content_interpolations(tmp_path) == [], f"the guard refused {snippet!r}"


@pytest.mark.parametrize(
    ("name", "sensitive"),
    [
        # The standard's minimum set, in the spellings that actually appear.
        ("password", True),
        ("PASSPHRASE", True),
        ("api_key", True),
        ("api-key", True),
        ("apiKey", True),
        ("Authorization", True),
        ("cookie", True),
        # CHANGED with the credential-precedence fix, deliberately. This row
        # used to assert False on the reasoning "an id, not the session
        # value". That reasoning does not survive: a session id IS the bearer
        # value in every session-cookie scheme -- presenting it IS
        # authenticating, so it is the credential, not a handle to one. No call
        # site in this package binds `session_id`; the name existed only in
        # this table, so nothing loses a diagnostic field by redacting it.
        ("session_id", True),
        ("credential", True),
        ("private_key", True),
        ("refresh_token", True),
        # This connector's own private content.
        ("body", True),
        ("subject", True),
        ("bodyPreview", True),
        ("recipients", True),
        ("sender", True),
        ("email_address", True),
        # Identifiers and counts are what the standard says to log INSTEAD.
        ("message_id", False),
        ("thread_id", False),
        ("draft_id", False),
        ("body_len", False),
        ("recipient_count", False),
        ("message_ids", False),
        ("folder", False),
        ("provider", False),
        ("voice", False),
    ],
)
def test_the_field_name_rule_classifies_each_spelling(name: str, sensitive: bool) -> None:
    """The rule is a NAME table, so it is asserted directly, without logging.

    Separator and case normalisation is the part that silently rots: without
    it, ``api_key`` is caught and ``apiKey`` is not, and nothing says so.
    """
    assert is_sensitive_field(name) is sensitive


# ---------------------------------------------------------------------------
# THE REDACTION GUARD'S INPUT DOMAIN -- AND WHY THE TABLE ABOVE IS NOT ONE
# ---------------------------------------------------------------------------
#
# `test_the_field_name_rule_classifies_each_spelling` (above) LOOKS like the
# input/output characterization this section provides. It is not. Every name in
# it is one somebody already thought of -- `body`, `subject`, `bodyPreview`,
# `recipients`. It restates SENSITIVE_FIELD_PATTERNS in a second file and
# checks the two copies agree. Add `payload` to the source list and that test
# does not fail; FORGET to add it and it still passes. A test that cannot
# distinguish those two worlds is testing nothing, and its own docstring
# concedes the shape: "The rule is a NAME table, so it is asserted directly."
#
# THE OUTPUT NOBODY TESTED is what the guard returns for a name nobody
# enumerated. The table below is that question, and the answer is that the
# approach cannot hold -- which is a STRUCTURAL verdict, not a missing row.
# Adding `payload` to the denylist would repair this one probe and leave the
# next synonym walking through, while making the guard LOOK fixed.

CONTENT_CARRYING_NAMES_A_DEVELOPER_MIGHT_PICK = [
    "payload",
    "msg",
    "data",
    "raw",
    "preview",
    "excerpt",
    "blob",
    "quote",
    "extract",
    "summary",
    "record",
    "entry",
    "detail",
    "note",
    "line",
    "doc",
    "value",
    "item",
    "chunk",
    "fragment",
    "reply",
    "draft",
    "letter",
    "memo",
]
"""Ordinary names for a variable that would hold mail text. None is exotic."""


@pytest.mark.xfail(
    strict=True,
    reason="STRUCTURAL: a name denylist cannot enumerate the synonym space. "
    "Fix is an opaque content type (SecretStr pattern), owned by a separate design.",
)
@pytest.mark.parametrize("name", CONTENT_CARRYING_NAMES_A_DEVELOPER_MIGHT_PICK)
def test_a_content_carrying_value_is_redacted_under_any_field_name(name: str) -> None:
    """THE REAL QUESTION: does mail content reach a sink, under ANY name?

    Not "is this name on the list" -- that question has a bounded answer and a
    list can be made to pass it. This asks the question the confidentiality
    promise actually makes, and a name-based rule cannot answer it in
    principle. Every parameter here returns False today, so every one of these
    ships a lawyer's mail to every sink.

    xfail(strict=True), so the day the opaque-content type lands this turns RED
    and has to be converted to a passing assertion. The defect is RECORDED as a
    defect rather than frozen as intended behaviour.
    """
    assert is_sensitive_field(name), (
        f"a value bound to {name!r} is logged verbatim; the denylist has no entry for it"
    )


@pytest.mark.parametrize("name", ["token_id", "password_id", "api_key_id", "secret_id"])
def test_appending_id_does_not_disarm_the_credential_rule(name: str) -> None:
    """THE SUFFIX INVERSION. Recorded as a strict-xfail defect; now FIXED.

    ``is_sensitive_field`` used to check ``ID_FIELD_SUFFIXES`` FIRST and return
    False unconditionally when it matched, so the guard was defeated on the
    standard's OWN minimum set merely by appending ``_id`` -- ``token_id``,
    ``password_id``, ``api_key_id`` all returned False and shipped the value to
    every sink.

    This was NOT the "synonyms are infinite" problem. It was a hole reachable
    from the denylist itself: the name was ON the list and the guard still said
    no. An id suffix exempts an OPAQUE IDENTIFIER (``message_id``); it never
    overrides an explicit credential match.

    The xfail(strict=True) marker was REMOVED rather than left in place, because
    the precedence split in ``logging_config`` makes this pass for real. The
    broader domain this defect belonged to -- every credential base against
    every suffix and separator spelling -- is covered by
    ``test_no_id_or_count_suffix_disarms_the_credential_vocabulary`` below.
    """
    assert is_sensitive_field(name), f"{name!r} defeats the credential rule via the _id suffix"


@pytest.mark.xfail(
    strict=True, reason="names are normalised by separator/case but not by unicode confusables"
)
@pytest.mark.parametrize("name", ["\u0455ubject", "subje\u0441t"])
def test_a_homoglyph_of_an_enumerated_name_is_still_caught(name: str) -> None:
    """Cyrillic look-alikes of names that ARE on the denylist return False.

    Included because it shows the failure is not only about unlisted synonyms:
    even an ENUMERATED name is evaded by a character that renders identically.
    """
    assert is_sensitive_field(name), f"{name!r} renders as an enumerated name but is not caught"


@pytest.mark.parametrize("value", [None, 123, b"subject", ["subject"], object()])
def test_the_field_name_rule_fails_loudly_on_a_non_string(value: object) -> None:
    """WRONG-TYPE partition. Documents that it RAISES rather than returning False.

    Raising is the correct posture per the standard (fail loudly, never
    silently mis-answer a security question), so this is asserted as intended
    behaviour rather than xfailed. It is recorded because a caller that passes a
    non-string gets an exception, not a quiet False, and that is worth knowing.
    """
    with pytest.raises((AttributeError, TypeError)):
        is_sensitive_field(value)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PRECEDENCE: CREDENTIAL VOCABULARY OUTRANKS THE ID/COUNT SUFFIX EXEMPTION
# ---------------------------------------------------------------------------
#
# `ID_FIELD_SUFFIXES` exists for a real reason and must keep working:
# `message_id`, `thread_id`, `body_len` are identifiers and counts, which is
# exactly what the standard says to log INSTEAD of a value. The defect is not
# that the exemption exists -- it is that it was UNCONDITIONAL and ran first,
# so it overrode the denylist sitting beside it.
#
# The distinction the fix encodes is about what the BASE NAME names, not about
# which check happens to run first:
#
#   * A name whose base names a CREDENTIAL is never loggable, suffix or not.
#     `token_id` is the id OF a token, and the token vocabulary is
#     categorically unloggable -- an id that dereferences a credential in a
#     store is a bearer value in every scheme where it is accepted.
#   * A name whose base names CONTENT, carrying an id/count suffix, refers to
#     a bounded opaque identifier and stays loggable. `message_id` is the id of
#     a message and is the diagnostic handle the standard asks for.
#
# These tests parametrize the CROSS PRODUCT of both vocabularies against every
# suffix in ID_FIELD_SUFFIXES and every separator spelling, rather than the
# three names this lane was handed. A denylist extended by three entries
# repairs three probes and admits the fourth synonym.

_SUFFIX_SPELLINGS: Final = [
    "{base}_{suffix}",
    "{base}-{suffix}",
    "{base}{suffix}",
    "{base}.{suffix}",
]
"""Separator spellings that `_normalize_name` is supposed to collapse to one."""


def _credential_bases() -> list[str]:
    """Every credential-vocabulary pattern, as a caller would spell a base name."""
    return [
        "password",
        "passphrase",
        "secret",
        "token",
        "api_key",
        "authorization",
        "cookie",
        "session",
        "credential",
        "private_key",
    ]


@pytest.mark.parametrize("base", _credential_bases())
@pytest.mark.parametrize("suffix", ["id", "ids", "count", "len"])
@pytest.mark.parametrize("template", _SUFFIX_SPELLINGS)
def test_no_id_or_count_suffix_disarms_the_credential_vocabulary(
    base: str, suffix: str, template: str
) -> None:
    """THE DOMAIN, not the three names the bug report happened to carry.

    Cross product of the whole credential vocabulary against every entry in
    ``ID_FIELD_SUFFIXES`` and every separator spelling. Before the fix the
    suffix check returned False unconditionally and ran first, so EVERY cell of
    this product returned False -- the guard was defeated on the standard's own
    minimum set by appending four characters.
    """
    name = template.format(base=base, suffix=suffix)
    assert is_sensitive_field(name), (
        f"{name!r} defeats the credential rule via the {suffix!r} suffix"
    )


@pytest.mark.parametrize(
    "name",
    [
        "API_KEY_ID",
        "apiKeyId",
        "api-key-id",
        "ApiKeyIds",
        "TOKEN_COUNT",
        "tokenLen",
        "Private-Key-Id",
        "REFRESH_TOKEN_ID",
        "client_secret_id",
        "AuthorizationIds",
    ],
)
def test_credential_precedence_holds_across_case_and_separator_spellings(name: str) -> None:
    """Case and separator normalisation must not be a second way through.

    ``_normalize_name`` collapses case and separators so one rule covers every
    spelling. If precedence were implemented against the RAW name instead of
    the normalised one, ``apiKeyId`` would walk through while ``api_key_id``
    was caught, and nothing would say so.
    """
    assert is_sensitive_field(name), f"{name!r} is a credential spelling that returns False"


@pytest.mark.parametrize(
    "name",
    [
        # Opaque provider handles: the diagnostic vocabulary the standard wants.
        "message_id",
        "thread_id",
        "draft_id",
        "container_id",
        "messageIds",
        "MESSAGE_ID",
        "message-id",
        # Counts and lengths: a size is not a value.
        "body_len",
        "subject_len",
        "recipient_count",
        "snippet_len",
        "bodyLen",
        # Names with no vocabulary hit at all.
        "folder",
        "provider",
        "voice",
        "limit",
    ],
)
def test_identifier_and_count_names_stay_loggable(name: str) -> None:
    """THE REGRESSION SIDE. The exemption must survive the precedence fix.

    The cheap way to close the credential hole is to delete the suffix rule or
    reorder the two checks, and either would take ``message_id`` and
    ``body_len`` with it -- the fields that make a log line diagnosable at all.
    A fix that redacts these has replaced one defect with another.
    """
    assert not is_sensitive_field(name), f"{name!r} is an identifier or count and must be logged"


@pytest.mark.parametrize(
    "name",
    [
        "body",
        "subject",
        "snippet",
        "bodyPreview",
        "recipients",
        "sender",
        "email_address",
        "content",
    ],
)
def test_content_names_without_a_suffix_are_still_redacted(name: str) -> None:
    """Content vocabulary with no id/count suffix keeps its existing verdict."""
    assert is_sensitive_field(name), f"{name!r} is content and must be redacted"


@pytest.mark.parametrize("name", ["", "   ", "_", "___", "-", "id", "count", "len", "ids"])
def test_empty_and_bare_suffix_names_do_not_crash_and_are_not_sensitive(name: str) -> None:
    """EMPTY / DEGENERATE partition, recorded as ACTUAL behaviour.

    A bare ``id`` with no base name, and a name that normalises to the empty
    string, must return a verdict rather than raise. None of these carries a
    vocabulary hit, so False is correct -- but it is the ABSENCE of a hit that
    makes it correct, not the suffix rule.
    """
    assert is_sensitive_field(name) is False


@pytest.mark.parametrize("value", [None, 123, b"token_id", ["token_id"], object(), 1.5])
def test_the_precedence_rule_still_fails_loudly_on_a_non_string(value: object) -> None:
    """WRONG-TYPE partition, re-asserted AFTER the fix.

    The existing wrong-type test pins this for the pre-fix code path. This one
    pins it for names that reach the new precedence branch, so a fix that added
    an early ``isinstance`` guard returning False -- silently mis-answering a
    security question -- would be caught.
    """
    with pytest.raises((AttributeError, TypeError)):
        is_sensitive_field(value)  # type: ignore[arg-type]


def test_credential_precedence_is_proven_at_the_sink_not_only_the_predicate(
    sink: list[str],
) -> None:
    """SINK-LEVEL PROOF. A predicate returning True is not a redacted log line.

    The prior lane's finding was that content reached a REAL sink under
    ``payload``, ``body_id``, and ``excerpt`` -- proven by driving loguru, not
    by calling the predicate. So this drives the actual ``ProviderCall``
    patcher with credential-shaped field names and asserts the VALUE is absent
    from what the sink captured, which is the promise the connector makes.
    """
    with ProviderCall(
        "google",
        "probe",
        token_id=CANARY_CREDENTIAL_SHAPED,
        password_id=CANARY_CREDENTIAL_SHAPED,
        api_key_id=CANARY_CREDENTIAL_SHAPED,
        apiKeyId=CANARY_CREDENTIAL_SHAPED,
        secret_count=CANARY_CREDENTIAL_SHAPED,
    ) as call:
        call.guard("probe", token_len=CANARY_CREDENTIAL_SHAPED)

    assert sink, "the call produced no log lines at all -- the test proves nothing"
    blob = "\n".join(sink)
    assert CANARY_CREDENTIAL_SHAPED not in blob, (
        f"a credential value reached a log sink under an _id/_count field name: {blob!r}"
    )


def test_the_sink_proof_is_not_vacuous(sink: list[str]) -> None:
    """The sink test above must be able to FAIL. Prove the capture works.

    Without this, a sink fixture that captured nothing, or a ProviderCall that
    logged nothing, would make the redaction assertion pass for the wrong
    reason. Here the same machinery carries a value under a name that is NOT
    sensitive, and that value MUST appear.
    """
    marker = "ZZQX-NOTSENSITIVE-plain-identifier-value"
    with ProviderCall("google", "probe", message_id=marker):
        pass

    blob = "\n".join(sink)
    assert marker in blob, (
        "the sink captured nothing under a non-sensitive name, so the redaction "
        "assertion above would pass even if redaction were broken"
    )
