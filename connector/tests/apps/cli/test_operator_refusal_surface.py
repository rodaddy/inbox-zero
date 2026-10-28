"""An unauthenticated install refuses at the CLI boundary, cleanly (#195).

``ConfigError`` already had this treatment: ``cli.main`` renders it as one
stderr line and returns 1, and ``_FivePointCall.__exit__`` logs it without a
traceback. ``AuthError`` is the SAME KIND OF THING -- "run auth first" is an
instruction to the operator, not a stack for a bug report -- and it had none of
it. On a fresh install ``watch --once`` raised it out of
``providers/gmail/provider.py`` and printed two full tracebacks with no
one-line refusal at all.

These tests drive ``cli.main`` rather than the command functions, because the
handler under test lives at that boundary, and they keep a loguru sink pointed
at the captured buffer, because removing the sink is exactly what made the
0.2.10 refusal test blind to the tracebacks that shipped.
"""

from __future__ import annotations

import io
import sys
from typing import Any

import pytest
from loguru import logger

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.config import ConfigError
from inbox_zero_connector.config_paths import INSTALL_ID_ENV, reset_install_id_cache
from inbox_zero_connector.errors import OperatorRefusalError
from inbox_zero_connector.providers.base import AuthError, ProviderError
from inbox_zero_connector.services.triage.taxonomy import TaxonomyError
from inbox_zero_connector.utils import logging_config


@pytest.fixture(autouse=True)
def _fresh_install_id_cache() -> Any:
    """The layer-1 read is cached per process, so every test starts from cold."""
    reset_install_id_cache()
    yield
    reset_install_id_cache()


def _raise(exc: BaseException) -> Any:
    """Throw from inside a lambda, which cannot hold a ``raise`` statement."""
    raise exc


def test_both_refusal_types_share_one_base() -> None:
    """``ConfigError`` and ``AuthError`` are both operator refusals.

    The hierarchy IS the fix: keying the CLI handler and the logging wrapper on
    a shared base is what stops the next sibling refusal from having to be
    remembered in three separate places. ``AuthError`` keeps its
    ``ProviderError`` identity as well, because the provider layer's own
    callers catch that and must not start missing it.
    """
    assert issubclass(ConfigError, OperatorRefusalError)
    assert issubclass(AuthError, OperatorRefusalError)
    assert issubclass(AuthError, ProviderError)
    # Found by the #195 manual proof rather than by the issue: ``watch --once``
    # against a fresh config directory reaches this before the provider.
    assert issubclass(TaxonomyError, OperatorRefusalError)


def test_main_renders_a_taxonomy_error_as_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing taxonomy is a refusal too, and it stands in the fresh-install path.

    The #195 manual proof ran ``watch --once`` against an empty config dir and
    got THREE tracebacks -- two from the crash handler, one from the
    interpreter -- over a message that already said to run ``init-taxonomy``.
    The base catches it now; the label sends the operator at the right command.
    """
    monkeypatch.setattr(
        cli,
        "_dispatch",
        lambda args, command: _raise(TaxonomyError("No taxonomy installed. Run init-taxonomy.")),
    )
    status = cli.main(["paths"])
    captured = capsys.readouterr()
    assert status == 1
    # A ``LOG: sinks ready`` record can precede the refusal, so the assertion is
    # on the refusal LINE rather than on the head of the whole buffer.
    assert "Taxonomy error: No taxonomy installed. Run init-taxonomy." in captured.err
    assert "Traceback" not in captured.err


def test_main_renders_an_auth_error_as_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ``AuthError`` out of dispatch becomes a clean message and exit 1."""
    monkeypatch.setattr(
        cli,
        "_dispatch",
        lambda args, command: _raise(AuthError("No usable Google credential. Run auth first.")),
    )
    status = cli.main(["paths"])
    captured = capsys.readouterr()
    assert status == 1
    assert "No usable Google credential." in captured.err
    assert "Traceback" not in captured.err


def test_an_auth_error_is_logged_without_its_traceback() -> None:
    """``ProviderCall`` records the failure but does not dump the stack (#195).

    The FAILURE point still fires at ERROR and still names
    ``error_type=AuthError`` -- the observability standard forbids swallowing --
    while a plain ``ProviderError`` through the same wrapper keeps its
    traceback, so the exemption is scoped to the refusal base and did not
    quietly disarm the wrapper for every provider fault.
    """
    stream = io.StringIO()
    logger.remove()
    sink_id = logger.add(stream, level="DEBUG", colorize=False, format="{level} {message}")
    try:
        with (
            pytest.raises(AuthError),
            logging_config.ProviderCall("google", "authenticate"),
        ):
            raise AuthError("no usable credential")
        auth_error_output = stream.getvalue()
        stream.truncate(0)
        stream.seek(0)
        with (
            pytest.raises(ProviderError),
            logging_config.ProviderCall("google", "list_inbox"),
        ):
            raise ProviderError("a real break")
        provider_error_output = stream.getvalue()
    finally:
        logger.remove(sink_id)

    assert "error_type=AuthError" in auth_error_output
    assert "Traceback" not in auth_error_output, auth_error_output
    # The control: an ordinary provider fault keeps the stack it has always had.
    assert "Traceback" in provider_error_output, provider_error_output


def test_watch_once_on_an_unauthenticated_install_refuses_in_one_line(
    monkeypatch: pytest.MonkeyPatch, installed_taxonomy: None
) -> None:
    """The command the 0.2.10 smoke ran, with a credential-shaped fault instead.

    A configured-but-unauthenticated install is the ordinary state of a kit
    between ``setup`` and ``auth``, and it reaches the provider rather than
    stopping at the keychain guard. The provider raises ``AuthError``; the
    contract is the one ``ConfigError`` already meets -- exit 1, one line, zero
    tracebacks.

    stderr is captured by REPLACING ``sys.stderr`` rather than through
    ``capsys``: loguru sinks built by an earlier test still hold that test's
    captured stream, and their own "I/O operation on closed file" traceback
    lands in ``capsys.readouterr().err`` regardless of what this command does.

    A LOGURU SINK STAYS POINTED AT THE CAPTURED BUFFER. Removing it is what let
    two provider tracebacks ship under a test that asserted there were none.
    """
    monkeypatch.setenv(INSTALL_ID_ENV, "test-195")
    monkeypatch.setattr(logging_config, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "get_provider",
        lambda *args, **kwargs: _raise(
            AuthError(
                "No usable Google credential. Run "
                "'uv run inbox-zero-connector auth google' in a terminal first."
            )
        ),
    )
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    logger.remove()
    # Bound to the buffer OBJECT, not to ``sys.stderr`` by name, so the sink
    # cannot be re-pointed at a stream a later test has closed. ``backtrace``
    # and ``diagnose`` are left at loguru's defaults: suppressing them here
    # would hide the very rendering under test.
    sink_id = logger.add(stderr, level="DEBUG", colorize=False, format="{level} {message}")
    try:
        status = cli.main(["watch", "--once"])
    finally:
        logger.remove(sink_id)
    written = stderr.getvalue()
    assert status == 1
    assert "Traceback" not in written, written
    printed = [
        line
        for line in written.splitlines()
        if line.strip() and not line.startswith(("DEBUG", "INFO", "ERROR", "WARNING"))
    ]
    assert len(printed) == 1, written
    assert printed[0].startswith("Authentication error: ")
