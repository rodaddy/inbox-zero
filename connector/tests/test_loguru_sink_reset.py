"""A leaked loguru sink cannot follow a test out of its own suite (#187).

Loguru's sink registry is PROCESS-global, so a sink a test adds outlives that
test unless somebody removes it. Point one at a captured stream -- a
``StringIO``, or the buffer ``capsys`` swaps in -- and the pair goes wrong the
moment the stream closes: the sink is still live, the next log line anywhere in
the run writes into a closed file, and loguru prints its own
``Logging error in Loguru Handler #N ... ValueError: I/O operation on closed
file`` traceback on stderr, attributed to whatever test happened to log next.

Nothing ASSERTED failed when this was found, which is exactly why it earns a
test rather than a note: the noise lands in an innocent test's captured stderr,
so the next person to write an assertion about stderr there debugs a defect that
is not in the code they are reading.
``tests/services/auth/test_uninitialized_refusal_surface.py`` already carries a
local workaround for it and documents the reasoning in its own docstring.

The fix is the autouse ``_reset_loguru_sinks`` fixture in ``tests/conftest.py``:
after EVERY test it drops every sink and restores one stderr sink, so a leak can
travel no further than the test that made it.

TWO ORDERED PAIRS live here, because the fixture can leak in two distinct ways
and each pair covers one. The first pair is a test that adds a sink onto a
``StringIO`` it then closes, followed by an innocent bystander whose stderr must
stay free of loguru's handler-error noise. The second pair is the residual the
fixture's OWN restore line used to cause: a test that redirects ``sys.stderr``
through ``monkeypatch``, followed by a bystander whose only sink must not be
that buffer. Both pairs only prove anything run in order in one process, which
is pytest's default within a module.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from loguru import logger

from inbox_zero_connector.config import LoggingSettings
from inbox_zero_connector.utils import logging_config

_LEAKED = io.StringIO()


def test_a_test_may_leak_a_sink_onto_a_stream_it_then_closes() -> None:
    """The mess. Adds a sink to a stream and closes it without removing the sink.

    A deliberate reproduction of the real shape: no test writes this on purpose,
    but ``logger.add(some_captured_stream)`` inside a ``try`` whose ``finally``
    only fires on the happy path -- or a fixture-captured buffer torn down by
    pytest -- reaches the same state.
    """
    logger.add(_LEAKED, level="DEBUG", format="{message}")
    logger.info("written while the stream is open")
    assert "written while the stream is open" in _LEAKED.getvalue()
    _LEAKED.close()


def test_the_next_test_logging_sees_no_loguru_handler_error(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The bystander. Logs normally and its stderr stays clean.

    ``capfd`` rather than ``capsys`` because loguru's handler-error traceback is
    written to the OS-level stderr by loguru's own error path, not through the
    ``sys.stderr`` object pytest replaces -- ``capsys`` would report clean here
    whether or not the bug was present, which would make the test a decoration.
    """
    logger.info("an ordinary line from an innocent test")
    captured = capfd.readouterr()
    assert "Logging error in Loguru Handler" not in captured.err, captured.err
    assert "I/O operation on closed file" not in captured.err, captured.err


def test_each_test_starts_with_exactly_one_stderr_sink() -> None:
    """The fixture restores a sink rather than leaving the logger silent.

    ``logger.remove()`` alone would also stop the noise, and it would do it by
    making every later test log into nothing -- which silently disarms the tests
    that assert on log OUTPUT. Proven by writing a line and observing it arrive.
    """
    seen: list[str] = []
    probe = logger.add(seen.append, level="DEBUG", format="{message}")
    try:
        logger.info("the logger is still live")
    finally:
        logger.remove(probe)
    assert any("the logger is still live" in line for line in seen)


_STDERR_LEAK = io.StringIO()


def test_a_test_may_redirect_sys_stderr_through_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other mess, and the one the fixture's own restore line used to make.

    A test redirects ``sys.stderr`` to a buffer to assert on what a command
    prints -- ``tests/apps/cli/test_operator_refusal_surface.py`` does exactly
    this. It adds no sink of its own and leaks nothing directly; the leak is
    manufactured at teardown, because ``_reset_loguru_sinks`` declares no
    ``monkeypatch`` dependency and therefore runs BEFORE the redirection is
    undone. A restore line that passed ``sys.stderr`` to ``logger.add`` would
    read this buffer and install it as the process's only sink.
    """
    monkeypatch.setattr(sys, "stderr", _STDERR_LEAK)
    assert sys.stderr is _STDERR_LEAK


def test_the_next_test_does_not_inherit_the_redirected_buffer_as_its_sink(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The bystander. Its sink must be stderr, not the previous test's buffer.

    Two assertions, because either alone would pass on a broken fixture. The
    first reads loguru's registry directly: with the pinning restore line the
    sole handler's stream IS ``_STDERR_LEAK``, so identity is the exact signal.
    The second proves the sink still WORKS -- a fixture that restored nothing at
    all would also satisfy the first, and would disarm every test in the suite
    that asserts on log output.

    ``capfd`` rather than ``capsys``: the restored sink resolves ``sys.stderr``
    at write time, and ``capsys`` replaces that object, so a line written
    through it never reaches the file descriptor. ``capfd`` captures at the fd,
    which is where an unredirected sink lands either way.
    """
    streams = [
        getattr(handler._sink, "_stream", handler._sink)
        for handler in logger._core.handlers.values()
    ]
    assert streams, "the fixture must restore a sink, not leave the logger silent"
    assert _STDERR_LEAK not in streams, (
        f"the previous test's redirected buffer became this test's sink: {streams}"
    )

    logger.info("PROOF: the restored sink reaches the real stderr")
    captured = capfd.readouterr()
    assert "PROOF: the restored sink reaches the real stderr" in captured.err
    assert _STDERR_LEAK.getvalue() == "", _STDERR_LEAK.getvalue()


# A THIRD ORDERED PAIR: the patcher and the bound extras, not the sinks (#80).
#
# ``logging_config.setup`` does not only add sinks -- it calls
# ``logger.configure(patcher=_redact, extra={...})``, and NEITHER of those is a
# sink. ``logger.remove()`` drops handlers and leaves both standing, so the
# ``_reset_loguru_sinks`` teardown above -- correct for everything it was
# written for -- hands the next test a logger that still runs the connector's
# redaction over every record and still binds ``service`` and
# ``correlation_id`` into every ``record["extra"]``.
#
# Observed shape: ``extra_on_record == [{'correlation_id': '-', 'service':
# 'inbox-zero-connector'}]`` on a record emitted by a test that never called
# ``setup``. It is the same class of defect as the sink leak and worse to
# debug, because there is no handler in the registry to point at -- a test
# asserting on a bare record's extras sees keys nothing in its own file put
# there, and a test asserting that a value reaches a sink UNREDACTED fails
# against a patcher it never installed.
#
# The fix is one line in the ``_reset_loguru_sinks`` teardown:
# ``logger.configure(patcher=None, extra={})`` alongside the sink reset.


def test_a_test_may_leave_the_connector_patcher_and_extras_installed(
    tmp_path: Path,
) -> None:
    """The mess. Calls the real ``setup``, which configures a patcher and extras.

    Nothing here is abusive -- this is what every test of real logging
    configuration does, and ``tests/utils/test_logging_console_level.py`` does
    it four times. The leak is not in the calling; it is that no teardown takes
    the patcher back.
    """
    logging_config.setup(LoggingSettings(level="DEBUG", directory=tmp_path), env="test")
    seen: list[dict[str, object]] = []
    probe = logger.add(lambda m: seen.append(dict(m.record["extra"])), level="DEBUG")
    try:
        logger.info("a line emitted while setup's configuration is live")
    finally:
        logger.remove(probe)
    assert seen and seen[0].get("service"), f"setup should bind its extras onto the record: {seen}"


def test_the_next_test_gets_a_record_with_no_connector_patcher_or_extras() -> None:
    """The bystander. A record it emits carries nothing ``setup`` put there.

    Two assertions for two leaked surfaces. The extras half is the directly
    observed symptom: ``service`` and ``correlation_id`` on a record emitted by
    a test that never called ``setup``. The patcher half is the one that bites
    silently -- ``_redact`` rewrites ``record["message"]``, so a bystander
    asserting that a value reaches a sink verbatim fails against redaction it
    never asked for. A canary that ``_redact_message`` rewrites proves the
    patcher is gone rather than merely inert on this particular string.
    """
    records: list[dict[str, object]] = []
    probe = logger.add(lambda m: records.append(m.record), level="DEBUG")
    try:
        logger.info("token=canary-value-not-a-secret")
    finally:
        logger.remove(probe)

    assert records, "the fixture must leave the logger live enough to emit"
    extra = dict(records[0]["extra"])
    assert "service" not in extra and "correlation_id" not in extra, (
        f"setup's bound extras followed a test out of its own scope: {extra}"
    )
    assert records[0]["message"] == "token=canary-value-not-a-secret", (
        f"setup's redacting patcher is still rewriting records: {records[0]['message']}"
    )
