"""Shared fixtures.

Every test redirects config and data to a tmp_path so nothing touches the real
user config, the real ledger, or the OS keychain. No test performs network I/O
and no test needs a credential.

The fixtures here also bound the two PROCESS-global surfaces a test can leave
behind: the settings/server singletons (``isolated_dirs``) and loguru's sink
registry (``_reset_loguru_sinks``). Both are global by the library's design, so
isolation cannot come from the test's own scope -- it has to be taken back
afterwards, here, once, for every test.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from inbox_zero_connector.apps.server import main as server
from inbox_zero_connector.config import reset_settings
from inbox_zero_connector.config_paths import CONFIG_DIR_ENV, DATA_DIR_ENV
from inbox_zero_connector.services.triage.taxonomy import default_taxonomy, write_taxonomy


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    config = tmp_path / "config"
    data = tmp_path / "data"
    config.mkdir()
    data.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(config))
    monkeypatch.setenv(DATA_DIR_ENV, str(data))
    reset_settings()
    server.reset_state()
    yield tmp_path
    reset_settings()
    server.reset_state()


@pytest.fixture(autouse=True)
def _isolate_repo_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the committed ``config/`` and ``secrets/`` layers at a tmp checkout.

    ``isolated_dirs`` above redirects the INSTALLED per-user layer, which used to
    be the only file the loader read. It now reads four layers, two of which
    resolve from ``PROJECT_ROOT`` -- so without this the suite would silently
    load THIS repo's real ``config/connector-config.json``, and a test asserting
    a default would be asserting whatever the checkout happens to say today.

    ``_layered_files`` reads the module global at call time, so setting the
    attribute is enough; a test that wants the real files opts back in by
    setting it itself.
    """
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    monkeypatch.setattr("inbox_zero_connector.config.PROJECT_ROOT", repo)


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches ``osascript`` or a PowerShell toast, ever.

    ``services.notify.notify`` shells out to the desktop notifier, and the
    watch loop calls it on a failure streak. The loop tests drive the loop with
    failing fake providers on purpose, so the first full run after #64 landed
    put hundreds of real "watcher still failing" banners on the operator's screen
    (2026-09-01 20:15, one per test that hit the streak). The notifier is a
    process-global side effect like the loguru sinks above, so it is fenced
    here for every test rather than remembered per test. ``test_notify.py``
    patches the same attribute itself to assert on the argv; a test-level
    ``monkeypatch`` layers over this one and unwinds first, so both hold.
    """
    import subprocess  # ruff: ignore[suspicious-subprocess-import] -- the fence stands in for the notifier's own subprocess call
    from types import SimpleNamespace

    from inbox_zero_connector.services import notify as notify_module

    def _swallow(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0)

    # Only the NAME inside notify.py is replaced, never the global module:
    # packaging and service-unit tests run real subprocesses on purpose.
    shim = SimpleNamespace(run=_swallow, SubprocessError=subprocess.SubprocessError)
    monkeypatch.setattr(notify_module, "subprocess", shim)


@pytest.fixture(autouse=True)
def _reset_loguru_sinks() -> Iterator[None]:
    """Drop every loguru sink after each test and restore one on stderr (#187).

    It also resets the two NON-sink process-global surfaces ``logging_config.
    setup`` configures -- the record patcher and the bound extras (#80). See the
    teardown comment; ``logger.remove()`` does not touch either one.

    Loguru's sink registry is PROCESS-global, so a sink outlives the test that
    added it. Aimed at a captured stream -- a ``StringIO``, or the buffer
    ``capsys`` swaps in -- the pair goes wrong as soon as the stream closes: the
    sink is still live, the next log line anywhere in the run writes into a
    closed file, and loguru prints its own ``Logging error in Loguru Handler #N
    ... ValueError: I/O operation on closed file`` on stderr. It is attributed
    to whatever test logged next, which is the expensive part -- the noise
    appears in an innocent test's captured output and the next person to assert
    on stderr there debugs a defect that is not in the code they are reading.
    ``tests/services/auth/test_uninitialized_refusal_surface.py`` documents
    working around exactly this.

    TEARDOWN, not setup, and the difference matters: a setup-only reset would
    leave the LAST test of a module holding its sink into the next module, which
    is the cross-suite bleed the issue names. Running after each test bounds a
    leak to the test that made it.

    IT RESTORES A SINK RATHER THAN LEAVING THE LOGGER SILENT. ``logger.remove()``
    alone stops the noise by making every later test log into nothing, which
    would silently disarm the tests that assert on log OUTPUT -- a much worse
    failure than the one being fixed, because it passes.

    THE RESTORED SINK RESOLVES ``sys.stderr`` AT WRITE TIME, not at teardown,
    and that distinction is the whole of the #187 residual. ``logger.add(
    sys.stderr, ...)`` PINS whatever object the name holds at the moment the
    fixture runs. This fixture declares no ``monkeypatch`` dependency, so pytest
    finalizes it BEFORE ``monkeypatch`` undoes a ``setattr(sys, "stderr", ...)``
    -- and a test that redirects stderr that way (``tests/apps/cli/
    test_operator_refusal_surface.py`` does) hands its throwaway ``StringIO``
    straight into the restore line, making the leaked buffer the process's ONLY
    sink. The fixture built to bound a leak would be the thing creating it: every
    later test logs into a dead buffer, and once that buffer closes loguru starts
    printing ``I/O operation on closed file`` again. Wrapping the write in a
    lambda defers the lookup to each log call, so the sink follows whatever
    redirection is in force instead of owning a stream of its own.
    ``sys.__stderr__`` would also dodge the pin, and is rejected because it stops
    following legitimate redirection -- the behaviour the docstring promises.

    ``logging_config.setup`` calls ``logger.remove()`` itself and re-adds the
    real sinks, so a test that exercises real logging configuration is
    unaffected: it installs its own sinks during the test and this hands the
    process back a clean one afterwards.

    Proven by ``tests/test_loguru_sink_reset.py``, which leaks a sink onto a
    closed ``StringIO`` in one test, leaks another through ``monkeypatch.setattr(
    sys, "stderr", ...)`` in a second, and asserts in both cases that the next
    test's stderr is clean and its only sink is not the leaked buffer.
    """
    yield
    logger.remove()
    # THE PATCHER AND THE BOUND EXTRAS ARE NOT SINKS, so ``logger.remove()``
    # above does not touch them (#80). ``logging_config.setup`` installs both
    # -- ``logger.configure(patcher=_redact, extra={"correlation_id": ...,
    # "service": ...})`` -- and they are process-global exactly like the sink
    # registry, so a test that exercises real logging configuration leaves the
    # connector's redaction running over every later record and ``service`` /
    # ``correlation_id`` bound into every later ``record["extra"]``. Observed:
    # ``{'correlation_id': '-', 'service': 'inbox-zero-connector'}`` on a record
    # emitted by a test that never called ``setup``. Worse to debug than a
    # leaked sink, because there is no handler in the registry to point at: a
    # test asserting a value reaches a sink verbatim fails against a patcher it
    # never installed. Proven by ``tests/test_loguru_sink_reset.py``.
    #
    # ``patcher=None`` DOES NOT CLEAR IT. loguru documents ``None`` as "leave
    # the previously configured patcher alone" (``_logger.py`` ``configure``:
    # "If not ``None``, this will replace previously configured ``patcher``"),
    # so the obvious spelling is a silent no-op -- it clears ``extra`` and
    # leaves ``_redact`` running, and because ``_redact`` re-inserts
    # ``correlation_id`` itself the cleared ``extra`` comes straight back on
    # the next record. A no-op callable is the actual reset.
    logger.configure(patcher=lambda record: None, extra={})
    logger.add(lambda message: sys.stderr.write(message), level="DEBUG")


@pytest.fixture
def installed_taxonomy() -> None:
    write_taxonomy(default_taxonomy())


# The Graph-adapter harness is registered as a PLUGIN, not imported.
#
# ``_outlook_harness`` holds the mocked-transport ``Recorder`` and the
# ``recorder``/``provider`` fixtures that the seven ``test_outlook_*`` modules
# share after the 2026-08-19 split of ``test_outlook_provider.py`` (it had grown
# past the repo's 500-code-line ceiling). Importing the fixture FUNCTIONS into
# each module is the obvious move and it does not survive contact with ruff:
# every test that takes ``provider`` as a parameter shadows the imported name,
# so F811 fires on each one, and ``--fix`` deletes the import outright -- which
# it did, turning 123 tests into fixture-not-found errors before this was
# caught. Plugin registration has no name to shadow.
#
# The fixtures are NOT defined in this file, because ``provider`` is a name
# another adapter wants: ``test_gmail_provider.py`` defines its own. A
# module-local fixture shadows a plugin one, so Gmail keeps its own and the
# ambiguity stays visible in that file rather than being resolved silently here.
pytest_plugins = ["_outlook_harness"]


# Shared harness modules (_gmail_harness, ...) live at tests/ root; mirrored
# tests live in subpackages. pytest inserts each test file's own directory, so
# the root must be added once, here, for bare harness imports to resolve.
sys.path.insert(0, str(Path(__file__).parent))
