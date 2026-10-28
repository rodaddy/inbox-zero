"""An uninitialized install refuses at the CLI boundary, cleanly (#171).

The keychain constructor raising is only half the fix. A ``ConfigError`` that
reaches ``main`` unhandled prints a traceback and exits 1 via ``SystemExit``
from the interpreter -- which is technically non-zero but is not a result
contract: the operator reads a stack trace, and a wrapper script cannot tell a
refusal from a crash. So ``main`` renders the config error as one line on
stderr and returns 1 itself.

These tests drive ``cli.main`` rather than the command functions, because the
handler under test lives at that boundary and calling the command directly
would step over it.
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
from inbox_zero_connector.services.auth import keychain
from inbox_zero_connector.utils import logging_config


@pytest.fixture(autouse=True)
def _fresh_install_id_cache() -> Any:
    """The layer-1 read is cached per process, so every test starts from cold."""
    reset_install_id_cache()
    yield
    reset_install_id_cache()


class _ForbiddenKeyring:
    """Any call is the bug. Records nothing because reaching it is already fatal."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def set_password(self, service: str, account: str, value: str) -> None:
        self.calls.append("set")

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append("get")
        return None

    def delete_password(self, service: str, account: str) -> None:
        self.calls.append("delete")


def test_main_renders_a_config_error_as_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``ConfigError`` out of dispatch becomes a clean message and exit 1."""
    monkeypatch.setattr(
        cli,
        "_dispatch",
        lambda args, command: (_ for _ in ()).throw(ConfigError("this install is not set up")),
    )
    status = cli.main(["paths"])
    captured = capsys.readouterr()
    assert status == 1
    assert "this install is not set up" in captured.err
    assert "Traceback" not in captured.err


def test_main_renders_a_startup_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ``load_settings`` call in ``main`` is inside the same handler."""
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: (_ for _ in ()).throw(ConfigError("bad layer")),
    )
    status = cli.main(["paths"])
    captured = capsys.readouterr()
    assert status == 1
    assert "bad layer" in captured.err
    assert "Traceback" not in captured.err


def test_logout_on_an_uninitialized_install_never_touches_the_keychain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shortest real command that reaches the keychain, end to end.

    ``logout --yes`` skips the prompt and goes straight to ``delete_token``,
    so it proves the whole chain -- CLI dispatch, constructor refusal, clean
    render -- without a provider, a network call, or a live credential.
    """
    forbidden = _ForbiddenKeyring()
    monkeypatch.setattr(keychain, "keyring", forbidden)
    monkeypatch.delenv(INSTALL_ID_ENV, raising=False)
    monkeypatch.setattr(keychain, "install_id", lambda: "")
    status = cli.main(["logout", "google", "--yes"])
    captured = capsys.readouterr()
    assert status == 1
    assert forbidden.calls == []
    assert "Traceback" not in captured.err


def test_watch_once_on_an_uninitialized_install_refuses_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
    installed_taxonomy: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command the 0.2.9 smoke ran, end to end: one line, exit 1, no traceback.

    ``watch --once`` reaches the keychain through the provider, and the watch
    loop's broad ``except Exception`` boundaries used to swallow the resulting
    ``ConfigError`` into a counted stream error -- so the loop logged a
    traceback, backed off, and the clean handler in ``cli.main`` never fired.
    The keyring here records every call so a refusal that leaked through to the
    OS credential store fails loudly rather than reading as a pass.

    stderr is read through ``capsys``. It used to be captured by REPLACING
    ``sys.stderr`` with a private buffer, because loguru sinks built by an
    earlier test still held that test's captured stream and their own "I/O
    operation on closed file" traceback landed in ``capsys.readouterr().err``
    regardless of what this command did. The autouse ``_reset_loguru_sinks``
    fixture in ``tests/conftest.py`` now drops every sink after each test
    (#187), so no sink survives to write into a closed buffer and ``capsys``
    reads exactly what this command wrote.

    A LOGURU SINK STAYS POINTED AT THE CAPTURED BUFFER, and that is the whole
    point of the test (#195). It used to call ``logger.remove()`` first, on the
    reasoning that the log stream was another module's surface -- which made it
    blind to exactly the defect that shipped. The 0.2.10 smoke found 48 stderr
    lines on this command: ``ProviderCall``'s failure point logged the
    ``ConfigError`` with ``opt(exception=True)`` at ``authenticate`` and again
    at ``list_containers``, so two full tracebacks printed above the one-line
    refusal. The test passed the whole time, because it had removed the sink
    that carried them.

    A ``ConfigError`` is not a call that broke -- it is an operator-facing
    refusal the CLI renders and returns 1 on. The failure is still RECORDED at
    ERROR with its ``error_type`` (nothing is swallowed); only the traceback is
    withheld, because a stack over the top of the sentence the operator is
    meant to read is what turned a one-line contract into 48.
    """
    forbidden = _ForbiddenKeyring()
    monkeypatch.setattr(keychain, "keyring", forbidden)
    monkeypatch.delenv(INSTALL_ID_ENV, raising=False)
    monkeypatch.setattr(keychain, "install_id", lambda: "")
    monkeypatch.setattr(logging_config, "setup", lambda *args, **kwargs: None)
    logger.remove()
    # Bound to the stream ``capsys`` has already swapped in, so the sink and the
    # reader below are looking at the same buffer. ``backtrace`` and ``diagnose``
    # are left at loguru's defaults: suppressing them here would hide the very
    # rendering under test.
    sink_id = logger.add(sys.stderr, level="DEBUG", colorize=False, format="{level} {message}")
    try:
        status = cli.main(["watch", "--once"])
    finally:
        logger.remove(sink_id)
    written = capsys.readouterr().err
    assert status == 1
    assert forbidden.calls == []
    assert "Traceback" not in written, written
    # The refusal is the LAST thing the operator reads and the only non-log
    # line. Log records are permitted above it -- observability is not the
    # thing #195 objects to -- but a traceback is not a record, it is a wall.
    printed = [
        line
        for line in written.splitlines()
        if not line.startswith(("DEBUG", "INFO", "ERROR", "WARNING"))
    ]
    printed = [line for line in printed if line.strip()]
    assert len(printed) == 1, written
    assert printed[0].startswith("Configuration error: ")


def test_a_config_error_is_logged_without_its_traceback() -> None:
    """``ProviderCall`` records the failure but does not dump the stack (#195).

    The narrow unit behind the end-to-end test above: the FAILURE point still
    fires at ERROR and still names ``error_type=ConfigError`` -- the
    observability standard forbids swallowing -- and a plain ``ValueError``
    through the same wrapper still gets its traceback, so the exemption is
    scoped to the refusal type and did not quietly disarm the wrapper.
    """
    stream = io.StringIO()
    logger.remove()
    sink_id = logger.add(stream, level="DEBUG", colorize=False, format="{level} {message}")
    try:
        with (
            pytest.raises(ConfigError),
            logging_config.ProviderCall("google", "authenticate"),
        ):
            raise ConfigError("no install id")
        config_error_output = stream.getvalue()
        stream.truncate(0)
        stream.seek(0)
        with (
            pytest.raises(ValueError),
            logging_config.ProviderCall("google", "authenticate"),
        ):
            raise ValueError("a real break")
        value_error_output = stream.getvalue()
    finally:
        logger.remove(sink_id)

    assert "error_type=ConfigError" in config_error_output
    assert "Traceback" not in config_error_output, config_error_output
    # The control: an ordinary failure keeps the stack it has always had.
    assert "Traceback" in value_error_output, value_error_output
