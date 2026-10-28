"""Every public entry path imports from a COLD interpreter.

WHY THIS FILE EXISTS
    The rest of the suite imports through ``tests/conftest.py``, which pulls
    ``server`` in first. That seeds ``inbox_zero_connector.utils.paths`` into
    ``sys.modules`` before anything else runs, so a circular import between
    ``config`` and ``mailboxscope`` is invisible to all of it.

    On 2026-08-18 that is exactly what happened: 1036 tests passed while
    ``inbox-zero-connector paths`` -- the shipped console script -- died with
    ``ImportError: cannot import name 'APP_NAME' from partially initialized
    module``. Four of five cold entry paths were broken and no test noticed,
    because no test imported the package the way a user does.

    Each case below runs in a SUBPROCESS on purpose. Importing in-process
    would reuse this interpreter's already-populated ``sys.modules`` and pass
    no matter what, which is the very hole that let the cycle ship.

    ``test_importing_the_package_logs_nothing`` guards the same class of defect
    on the logging side: module-level code runs before
    ``logging_config.setup`` has installed a sink, so anything it logs goes to
    loguru's DEFAULT stderr handler at DEBUG, which no setting of ours reaches.
    Same reason for the subprocess -- by the time the suite is running, the
    import side effect has already happened and cannot be observed in-process.
"""

from __future__ import annotations

import subprocess  # ruff: ignore[suspicious-subprocess-import]  fixed argv, no shell, no untrusted input
import sys

import pytest

ENTRY_POINTS = [
    "import inbox_zero_connector",
    "import inbox_zero_connector.config",
    "import inbox_zero_connector.services.watch.scope",
    "import inbox_zero_connector.utils",
    "import inbox_zero_connector.utils.paths",
    "import inbox_zero_connector.apps.cli.main",
]


@pytest.mark.parametrize("statement", ENTRY_POINTS)
def test_entry_point_imports_cold(statement: str) -> None:
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]  argv is a literal list; shell=False
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"`{statement}` fails from a cold interpreter:\n{result.stderr}"


def test_console_script_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "inbox_zero_connector.apps.cli.main", "paths"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"the shipped CLI entry point fails:\n{result.stderr}"


SILENT_IMPORT_PROBE = """
import sys
from loguru import logger

seen = []
logger.remove()
logger.add(seen.append, level="TRACE")

import inbox_zero_connector  # noqa: F401
from inbox_zero_connector.services.auth import scopes  # noqa: F401

for record in seen:
    sys.stdout.write("LEAK " + record.record["message"] + "\\n")
"""


def test_importing_the_package_logs_nothing() -> None:
    """No module-level import may log: no sink of ours is configured that early.

    Regressed once already: ``scopes.NO_SEND_ENFORCEMENT`` was a module-level
    constant computed by ``enforcement_for``, which logs, so every CLI command
    printed two DEBUG lines above its own output -- on a quiet-console install
    too, since the records predate every sink ``setup`` installs.
    """
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]  argv is a literal list; shell=False
        [sys.executable, "-c", SILENT_IMPORT_PROBE],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stderr}"
    leaks = [line for line in result.stdout.splitlines() if line.startswith("LEAK ")]
    assert leaks == [], f"import-time log lines: {leaks}"
