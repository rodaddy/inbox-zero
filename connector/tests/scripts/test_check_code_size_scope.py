"""What the size receipt has to actually cover, and what its success line means.

``scripts/check_code_size.py`` is the receipt every lane in this kit runs to
claim the size LAW holds. A bare run globs only ``<connector>/src``, so the
``tests/`` tree -- where sprawl accumulates fastest and where nobody is
watching -- is unreachable from the exact invocation that is being trusted.
Worse, the success line always names ``PACKAGE_ROOT`` even when explicit paths
outside ``src`` were passed, so a green receipt can name a root it never
walked. Both make the receipt mean less than readers assume.

These tests were authored RED-FIRST for issue #24: they fail against the
unfixed script and encode the required post-fix behaviour -- a bare run walks
BOTH ``src`` and ``tests``, the success line names each root actually walked
with its ``.py`` file count, and explicit-path mode names the paths it examined
rather than the ``src`` constant. Thresholds (500 code lines/file, 50/function)
and the exit grammar (``0`` pass, ``1`` finding, ``3`` harness error) are
unchanged by this lane.

The script is driven as a SUBPROCESS against a synthesized project layout: its
scope constants are derived from its own location on disk, so the only honest
way to point it at controlled input is to copy it into a controlled tree, and
the exit code is only observable across the process boundary anyway.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_code_size.py"

SMALL_MODULE = '''"""A small documented module.

Three lines so the docstring heuristics elsewhere stay quiet, and far under
the 500-code-line file ceiling.
"""


def tiny() -> int:
    """Return one.

    Well under the 50-code-line function ceiling.
    """
    return 1
'''

BIG_FILE_HEADER = '''"""A module that breaches the file ceiling without any big function.

520 distinct simple assignments at module level: over the 500-line file
ceiling, with no function at all, so only the file finding can fire.
"""

'''


def _big_file_source() -> str:
    """Build a module of 520 module-level code lines and no functions."""
    body = "\n".join(f"x{index} = {index}" for index in range(520))
    return BIG_FILE_HEADER + body + "\n"


def _layout(tmp_path: Path, *, tests_source: str) -> Path:
    """Build a throwaway connector-shaped project the script can run inside.

    The script resolves its roots relative to ``<script>/..``, so the copy has
    to sit at ``<root>/scripts/`` for ``<root>/src/`` and ``<root>/tests/`` to
    be the trees it walks. Returns the layout root.
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check_code_size.py")
    (tmp_path / "src" / "ok.py").write_text(SMALL_MODULE, encoding="utf-8")
    name = "test_big.py" if "x0 = 0" in tests_source else "test_ok.py"
    (tmp_path / "tests" / name).write_text(tests_source, encoding="utf-8")
    return tmp_path


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the copied script from ``root`` with the current interpreter."""
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "check_code_size.py"), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(root),
    )


def _root_with_count(stdout: str, root: str, count: int) -> bool:
    """True when one output line names ``root`` and the digit ``count``.

    Either order, so ``src: 1`` and ``1 file(s) under .../src`` both satisfy
    it -- the requirement is that the root and its count are reported
    together, not a particular phrasing.
    """
    pattern_a = re.compile(rf"{re.escape(root)}\D*\b{count}\b")
    pattern_b = re.compile(rf"\b{count}\b.*{re.escape(root)}")
    return any(
        pattern_a.search(line) or pattern_b.search(line) for line in stdout.splitlines()
    )


def test_bare_run_reaches_tests_tree(tmp_path: Path) -> None:
    """A bare run must walk tests/ too, or the receipt never saw the sprawl."""
    root = _layout(tmp_path, tests_source=_big_file_source())
    result = _run(root)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "test_big.py" in result.stdout


def test_bare_run_names_both_roots_and_counts(tmp_path: Path) -> None:
    """A clean bare run must name each root it walked and how many files."""
    root = _layout(tmp_path, tests_source=SMALL_MODULE)
    result = _run(root)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "src" in result.stdout
    assert "tests" in result.stdout
    assert _root_with_count(result.stdout, "src", 1), result.stdout
    assert _root_with_count(result.stdout, "tests", 1), result.stdout


def test_explicit_paths_report_the_paths_not_src(tmp_path: Path) -> None:
    """Explicit-path mode must name what it examined, never the src constant."""
    root = _layout(tmp_path, tests_source=SMALL_MODULE)
    result = _run(root, "tests/test_ok.py")
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "/src" not in result.stdout, result.stdout
    assert "test_ok.py" in result.stdout
