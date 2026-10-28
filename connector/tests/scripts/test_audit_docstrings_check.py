"""Blocking mode for the docstring census: what makes zero findings stay zero.

``scripts/audit_docstrings.py`` was a report-only census -- it printed every
undocumented module and function and then exited 0 regardless, so the tree
reaching zero findings was a fact about one afternoon and not a property
anything defended. ``--check`` is what turns the census into a gate: the same
report, plus exit 1 when a MODULE-NO-DOC or FUNC-NO-DOC line was printed.

FUNC-ONE-LINE-DOC stays advisory on purpose. "This one-liner should say more"
is a judgment about prose quality that a line-count heuristic cannot make
correctly, and a gate that blocks on it would train people to pad docstrings
to three lines rather than write better ones. Missing entirely is mechanical;
too thin is editorial. Only the mechanical half blocks.

The script is driven as a SUBPROCESS against a synthesized ``src/`` tree
rather than imported. Its scope constant is derived from the script's own
location on disk, so the only honest way to point it at controlled input is
to place a copy of it in a controlled project layout -- and the exit code,
which is the whole contract the pre-commit hook consumes, is only observable
through the process boundary anyway.

Exit grammar under test: ``0`` pass, ``1`` finding, ``3`` harness error.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_docstrings.py"

DOCUMENTED = '''"""A module docstring.

It has three lines so MODULE-THIN-DOC does not fire.
"""


def documented() -> int:
    """Return one.

    Long enough that the thin-doc heuristic stays quiet.
    """
    return 1
'''

UNDOCUMENTED = '''"""A module docstring.

Three lines, so only the function is at fault here.
"""


def bare() -> int:
    return 1
'''

ONE_LINE_DOC = '''"""A module docstring.

Three lines, so only the one-line function docstring is notable.
"""


def sprawling() -> int:
    """One line only."""
    total = 0
    total += 1
    total += 2
    total += 3
    total += 4
    total += 5
    total += 6
    total += 7
    total += 8
    total += 9
    total += 10
    return total
'''


def _project(tmp_path: Path, module_source: str) -> Path:
    """Build a throwaway project the script can be run from.

    The script resolves ``src`` as ``<script>/../../src``, so the copy has to
    sit at ``<root>/scripts/`` for ``<root>/src/`` to be the tree it walks.
    Returns the path of the copied script, which is what the tests invoke.
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src").mkdir()
    copied = tmp_path / "scripts" / "audit_docstrings.py"
    shutil.copy(SCRIPT, copied)
    (tmp_path / "src" / "mod.py").write_text(module_source, encoding="utf-8")
    return copied


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the copied script with the current interpreter, capturing stdout."""
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_fails_on_undocumented_function(tmp_path: Path) -> None:
    """An undocumented function is the mechanical failure --check exists for."""
    script = _project(tmp_path, UNDOCUMENTED)
    result = _run(script, "--check")
    assert result.returncode == 1, result.stdout
    assert "FUNC-NO-DOC" in result.stdout
    assert "bare" in result.stdout


def test_check_passes_on_documented_tree(tmp_path: Path) -> None:
    """A fully documented tree is the state the gate is holding in place."""
    script = _project(tmp_path, DOCUMENTED)
    result = _run(script, "--check")
    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


def test_one_line_docstring_is_advisory(tmp_path: Path) -> None:
    """FUNC-ONE-LINE-DOC prints and does not block: editorial, not mechanical."""
    script = _project(tmp_path, ONE_LINE_DOC)
    result = _run(script, "--check")
    assert result.returncode == 0, result.stdout
    assert "FUNC-ONE-LINE-DOC" in result.stdout


def test_default_mode_still_reports_without_failing(tmp_path: Path) -> None:
    """No flag is byte-identical to the old census: report everything, exit 0."""
    script = _project(tmp_path, UNDOCUMENTED)
    result = _run(script)
    assert result.returncode == 0, result.stdout
    assert "FUNC-NO-DOC" in result.stdout


def test_unreadable_src_is_a_harness_error(tmp_path: Path) -> None:
    """A missing src/ is exit 3 -- the harness is broken, not the tree."""
    (tmp_path / "scripts").mkdir()
    copied = tmp_path / "scripts" / "audit_docstrings.py"
    shutil.copy(SCRIPT, copied)
    result = _run(copied, "--check")
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
