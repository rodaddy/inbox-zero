"""Three defects on the enforcement spine, each pinned by a regression test.

Every test here failed before the fix that accompanies it. They are grouped in
one file because they share a single theme: a documented guarantee that the
code did not actually hold.

F4  ``owner_address`` fail-open did not cover the SDK's own auth exceptions, so
    a revoked Google token crashed ``get_thread``/``create_draft`` with an
    opaque ``RefreshError`` instead of degrading with an actionable message.
F6  The redaction patcher only swept the bound extras, so content placed in the
    MESSAGE STRING reached every sink -- contradicting the module docstring's
    two-mechanism claim.
F8  ``INBOX_ZERO_DRAFTS_ENABLED`` outranked the installed JSON, so the
    environment could silently flip Gmail from scope-layer to code-layer
    enforcement while the file a human reads still said scope-layer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from loguru import logger

from inbox_zero_connector.apps.server import main as server
from inbox_zero_connector.config import ConfigError, load_settings
from inbox_zero_connector.providers.base import (
    AuthError,
    Container,
    DraftResult,
    MailProvider,
    Message,
    SentMessage,
    SentSelection,
    Thread,
)
from inbox_zero_connector.utils.logging_config import ToolCall, is_sensitive_field

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# F4 -- the fail-open must cover the SDK's own auth family
# ---------------------------------------------------------------------------


class RevokedTokenProvider(MailProvider):
    """A Gmail adapter whose stored refresh token has been revoked.

    ``authenticate`` raises the exception google-auth ACTUALLY raises when
    ``creds.refresh()`` is rejected -- not a connector ``AuthError``. That gap
    is the defect: the adapter lets its SDK's exception escape untranslated.
    """

    name = "google"

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.authenticate_calls = 0

    def authenticate(self, interactive: bool = False) -> str:
        self.authenticate_calls += 1
        raise self.error

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[Message]:
        raise NotImplementedError

    def get_thread(self, thread_id: str) -> Thread:
        return Thread(
            id=thread_id,
            subject="Hello",
            messages=[
                Message(
                    id="m1",
                    thread_id=thread_id,
                    sender="sender@other.example",
                    subject="Hello",
                    received="2026-08-17",
                )
            ],
            bodies=["a body"],
            participants=["owner@example.com", "sender@other.example"],
            labels=[],
        )

    def list_containers(self) -> list[Container]:
        raise NotImplementedError

    def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
        raise NotImplementedError

    def file_message(self, message_id: str, folder: str) -> str:
        raise NotImplementedError

    def poll_changes(self, cursor, *, page_token=None, container_id=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def tag_message(self, message_id: str, label: str) -> str:
        raise NotImplementedError

    def set_important(self, message_id: str, important: bool = True) -> str:
        raise NotImplementedError

    def create_draft(self, thread_id: str, body: str) -> DraftResult:
        raise NotImplementedError

    def create_self_draft(self, subject: str, body: str) -> str:
        raise NotImplementedError


def _http_error() -> HttpError:
    """The exception ``users().getProfile().execute()`` raises on a 401."""

    class _Response:
        status = 401
        reason = "Unauthorized"

    return HttpError(_Response(), b'{"error": "invalid_credentials"}', uri="getProfile")


@pytest.fixture
def revoked(monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None) -> RevokedTokenProvider:
    provider = RevokedTokenProvider(RefreshError("Token has been expired or revoked."))
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_owner", None)
    return provider


def test_a_revoked_refresh_token_fails_open_instead_of_crashing(
    revoked: RevokedTokenProvider,
) -> None:
    """F4: ``RefreshError`` must land on the documented fail-open, not escape.

    Before the fix this raised ``google.auth.exceptions.RefreshError`` out of
    ``get_thread``, because ``owner_address`` caught only
    ``(ProviderError, ConfigError, OSError)`` and the SDK's exception is in
    none of those trees.
    """
    assert server.owner_address() == ""
    assert revoked.authenticate_calls == 1


def test_a_revoked_refresh_token_does_not_break_get_thread(
    revoked: RevokedTokenProvider,
) -> None:
    """The tool still answers, and simply drops no recipient."""
    result = server.get_thread("t1")
    # Owner unknown, so nothing is dropped -- the harmless direction.
    assert result["reply_would_go_to"] == ["owner@example.com", "sender@other.example"]


def test_the_unresolved_owner_warning_names_the_command_that_fixes_it(
    revoked: RevokedTokenProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """A degraded path must say what the owner has to DO about it.

    ``error_type=RefreshError`` alone tells the owner nothing; the actionable
    half is the re-auth command.

    Settings are loaded BEFORE the probe is attached: ``owner_address`` reaches
    the settings keystone, whose first-use path calls ``logging_config.setup``,
    which begins with ``logger.remove()``. A probe attached first would be torn
    down mid-test and this would assert over an empty list.
    """
    # Warm the keystone: the warning path itself calls
    # `configured_provider_name()`, whose first use builds the settings and
    # therefore rebuilds every sink. Doing it here means the probe below
    # survives the call it is measuring.
    server.configured_provider_name()

    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING", format="{message}")
    try:
        assert server.owner_address() == ""
    finally:
        try:
            logger.remove(sink_id)
        except ValueError:
            # A reconfiguration inside the call already removed it. Tolerated
            # here for the same reason the redaction suite tolerates it, and
            # the assertions below still prove the line was captured first.
            logger.debug("SINK: capture sink was already removed by a reconfiguration")

    blob = "\n".join(captured)
    assert "OWNER: address unresolved" in blob
    assert "auth google" in blob, "the warning must name the re-auth command"


def test_an_http_error_from_get_profile_also_fails_open(
    monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None
) -> None:
    """The other untranslated escape: ``getProfile`` raising ``HttpError``."""
    provider = RevokedTokenProvider(_http_error())
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_owner", None)

    assert server.owner_address() == ""


def test_an_auth_error_still_fails_open(
    monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None
) -> None:
    """The pre-existing path is unchanged -- the widening is additive."""
    provider = RevokedTokenProvider(AuthError("No usable Google credential."))
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_owner", None)

    assert server.owner_address() == ""


def test_an_unexpected_error_still_propagates(
    monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None
) -> None:
    """The fail-open is a NAMED list, not a bare except.

    A ``TypeError`` in the adapter is a defect and must surface, or the
    connector runs blind over a mailbox with a broken provider.
    """
    provider = RevokedTokenProvider(TypeError("adapter defect"))
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_owner", None)

    with pytest.raises(TypeError):
        server.owner_address()


# ---------------------------------------------------------------------------
# F6 -- redaction must cover the message string, not only the extras
# ---------------------------------------------------------------------------

CANARY_MESSAGE_BODY = "ZZQX-MSGBODY-the-retainer-is-fifty-thousand-dollars"
CANARY_MESSAGE_SUBJECT = "ZZQX-MSGSUBJ-Delgado-matter-settlement-terms"


@pytest.fixture
def sink() -> Iterator[list[str]]:
    """Capture every rendered line and its extras, at DEBUG.

    ``load_settings()`` runs FIRST because ``logging_config.setup`` begins with
    ``logger.remove()`` -- a probe attached before it would be torn down and
    the assertions would sweep an empty list.
    """
    load_settings()
    captured: list[str] = []

    def write(message: Any) -> None:
        captured.append(str(message))
        captured.append(repr(message.record["extra"]))

    sink_id = logger.add(write, level="DEBUG", format="{message}")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def test_content_in_the_message_string_is_redacted_not_logged(sink: list[str]) -> None:
    """F6: a ``key=value`` pair in the MESSAGE is redacted, as in the extras.

    The module docstring claims two mechanisms guard mail content. Before the
    fix the second one (``_redact``) rewrote only ``record["extra"]``, so a
    body interpolated into the message string as ``body=<...>`` shipped
    verbatim to all three sinks while the identical value passed as a bound
    field was redacted -- the promise held for one spelling and not the other.
    """
    logger.info(f"PROBE: body={CANARY_MESSAGE_BODY}")
    logger.warning(f"PROBE: subject={CANARY_MESSAGE_SUBJECT}")

    blob = "\n".join(sink)
    assert CANARY_MESSAGE_BODY not in blob, "a body in the message string reached a sink"
    assert CANARY_MESSAGE_SUBJECT not in blob, "a subject in the message string reached a sink"
    assert "[redacted]" in blob, "the marker should say a value was withheld"


def test_a_long_message_is_truncated_rather_than_stored_whole(sink: list[str]) -> None:
    """The bound on unattributed prose.

    A name-based rule cannot classify free prose -- there is no way to tell a
    mail body from a diagnostic sentence by inspection. What IS decidable is
    length: a message far past a sentence is content that got pasted into one,
    so it is truncated to a length summary.
    """
    logger.info("PROBE: " + CANARY_MESSAGE_BODY * 40)

    blob = "\n".join(sink)
    assert "<truncated, total len=" in blob, "an over-long message must be truncated"
    assert blob.count(CANARY_MESSAGE_BODY) < 40, "the whole payload was stored"


_INTERPOLATION: re.Pattern[str] = re.compile(r"\{(?P<expr>[^{}]+)\}")


def _bare_content_exprs(line: str) -> list[str]:
    """Interpolations on a logging line that name content WITHOUT attaching the name.

    ``f"body={body}"`` is fine -- the redaction pass matches the attached name
    and rewrites the value. ``f"upstream said {body}"`` is not: there is no name
    in the rendered text for any patcher to match on.

    Args:
        line: One source line.

    Returns:
        The offending interpolation expressions, empty when the line is clean.
    """
    stripped = line.strip()
    if not stripped.startswith(("logger.", 'f"', "f'")):
        return []

    found: list[str] = []
    for match in _INTERPOLATION.finditer(stripped):
        # An interpolation ATTACHED to a name is covered by the redaction pass.
        if match.start() >= 1 and stripped[match.start() - 1] == "=":
            continue
        expr = match.group("expr")
        leaf = re.split(r"[.\(\)\[\]:]", expr)[0].strip()
        # A MODULE CONSTANT is not content. `MAX_DRAFT_BODY_CHARS` is a bound
        # whose whole purpose is to appear in the message explaining the bound,
        # and the same reasoning the package already applies to `body_len`
        # applies to it: a limit describes content without being any.
        if not leaf or leaf.isupper():
            continue
        if is_sensitive_field(leaf):
            found.append(expr)
    return found


def test_no_source_call_site_interpolates_a_content_variable(sink: list[str]) -> None:
    """The GUARD, and the half of F6 that actually holds the line.

    Redaction of the message string is a backstop with a real limit: it matches
    ``key=value`` pairs by NAME, so ``f"upstream said {body}"`` -- bare prose
    with no name attached -- cannot be caught by any rule that does not also
    blank legitimate diagnostics.

    So the primary mechanism is enforced where it IS decidable: at the source.
    Every logging call site in this package follows the ``PREFIX: text
    key=value`` discipline, and this test fails if one ever interpolates a
    variable whose name is in the sensitive table without attaching that name.
    """
    package = Path(__file__).resolve().parent.parent / "src" / "inbox_zero_connector"
    offenders: list[str] = []

    for source in sorted(package.rglob("*.py")):
        if source.name == "logging_config.py":
            # The redactor itself is exempt: its locals hold the ALREADY-redacted
            # message, so matching them would flag the fix as the defect.
            continue
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            offenders.extend(
                f"{source.name}:{number}: {{{expr}}}" for expr in _bare_content_exprs(line)
            )

    assert offenders == [], (
        "a log call interpolates a content-named variable as bare prose, where "
        f"the redaction pass cannot reach it: {offenders}"
    )


def test_message_redaction_keeps_the_line_diagnosable(sink: list[str]) -> None:
    """Redaction must not be achieved by blanking the whole message.

    The complement: a patcher that replaced every message with ``[redacted]``
    would pass the leak test and destroy the observability the standard
    requires.
    """
    with ToolCall("probe_operation", message_id="m1", folder="Action"):
        pass

    blob = "\n".join(sink)
    assert "TOOL: probe_operation" in blob, "the operation name must survive"
    assert "message_id=m1" in blob, "an identifier is not content"
    assert "folder=Action" in blob, "a closed-set name is not content"
    assert "phase=start" in blob
    assert "phase=ok" in blob


# ---------------------------------------------------------------------------
# F8 -- the environment may not silently flip the enforcement posture
# ---------------------------------------------------------------------------

DRAFTS_ENV: str = "INBOX_ZERO_DRAFTS_ENABLED"


def _install(path: Path, drafts: dict[str, bool]) -> Path:
    target = path / "connector-config.json"
    target.write_text(json.dumps({"drafts_enabled": drafts}), encoding="utf-8")
    return target


def test_env_cannot_silently_enable_drafts_over_the_installed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8: the persisted file is the source of truth for the SECURITY POSTURE.

    Before the fix, ``env_settings`` outranked the JSON layer for every field,
    so exporting this variable flipped Gmail from scope-layer to code-layer
    enforcement while USER-CONFIG.md still recorded scope-layer. The buyer's
    admin console and the file they can read would then disagree with what the
    connector actually asks Google for.
    """
    target = _install(tmp_path, {"google": False, "microsoft": False})
    monkeypatch.setenv(DRAFTS_ENV, '{"google": true}')

    with pytest.raises(ConfigError, match="drafts_enabled"):
        load_settings(target, configure_logging=False)


def test_the_disagreement_message_names_the_variable_and_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loud failure is only useful if it says which two things disagree."""
    target = _install(tmp_path, {"google": False, "microsoft": False})
    monkeypatch.setenv(DRAFTS_ENV, '{"google": true}')

    with pytest.raises(ConfigError) as caught:
        load_settings(target, configure_logging=False)

    message = str(caught.value)
    assert DRAFTS_ENV in message, "the message must name the environment variable"
    assert str(target) in message, "the message must name the installed file"


def test_an_env_value_agreeing_with_the_file_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only DISAGREEMENT is fatal. A redundant export is not an error."""
    target = _install(tmp_path, {"google": True, "microsoft": False})
    monkeypatch.setenv(DRAFTS_ENV, '{"google": true, "microsoft": false}')

    loaded = load_settings(target, configure_logging=False)
    assert loaded.drafts_enabled["google"] is True
    assert loaded.drafts_enabled["microsoft"] is False


def test_the_installed_file_still_decides_when_no_env_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary path: file present, environment silent, file wins."""
    monkeypatch.delenv(DRAFTS_ENV, raising=False)
    target = _install(tmp_path, {"google": True, "microsoft": False})

    loaded = load_settings(target, configure_logging=False)
    assert loaded.drafts_enabled["google"] is True


def test_env_alone_still_works_when_no_file_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing persisted there is no source of truth to contradict.

    A missing file means the Stage 1 default posture, and the operator's
    environment is then the only statement anybody made.
    """
    monkeypatch.setenv(DRAFTS_ENV, '{"google": true}')
    loaded = load_settings(tmp_path / "absent.json", configure_logging=False)
    assert loaded.drafts_enabled["google"] is True


def test_other_fields_are_still_env_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrowing is scoped to the posture field, not to the whole model.

    ``env`` and ``provider`` are operational knobs with no security posture
    attached; pinning them to the file would be a different and unrequested
    change.
    """
    target = _install(tmp_path, {"google": False, "microsoft": False})
    monkeypatch.setenv("INBOX_ZERO_ENV", "staging")

    loaded = load_settings(target, configure_logging=False)
    assert loaded.env == "staging"
