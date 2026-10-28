"""The basic tier builds green, and its withheld capability is withheld whole.

WHY THIS TEST EXISTS
    The basic SKU is sold without drafting: ``tiers.json`` withholds
    ``services/voice/**`` and pins ``posture: file_only``. Withholding the
    SERVICE alone is not the same as withholding the CAPABILITY -- the ``voice``
    CLI verb lives under ``apps/cli/``, which the tier shipped, and it imports
    the service that is not there. The artifact therefore carried a command that
    could only ever raise ``ModuleNotFoundError`` at the customer's machine, and
    ``assert_zip.py`` caught it at package time as two ``forbidden_content``
    violations, so ``--tier basic`` could not build at all.

    The judge was right and the manifest was stale: ``apps/cli/voice.py`` landed
    (6ebeb1e) after the exclude list was written and nothing added it. This test
    is the standing check that the tier still builds -- exit 0 means the builder
    wrote BOTH cuts and its own judge accepted them, which is the whole claim a
    packager makes.

WHY EXIT CODE AND NOT A MEMBER LIST
    Asserting "``services/voice`` is absent" would have passed the entire time
    the bug existed. The exit code is the judge's verdict on the finished bytes,
    which is the assertion that actually failed before the fix.
"""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- the script IS the thing under test.
import sys
from pathlib import Path

import pytest

CONNECTOR_ROOT = Path(__file__).resolve().parents[2]
KIT_BUILDER = "scripts/packaging/build_zips.py"

VERSION = (CONNECTOR_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _git_sees_the_tree() -> bool:
    """Whether ``git ls-files`` from the connector root reports a real work tree.

    The builder ships git-TRACKED members only. The pre-commit hook runs this
    suite inside a ``git checkout-index`` copy under ``.git/``, which is not a
    work tree: there the member list is empty by construction and this test
    would report a defect that does not exist.
    """
    git = shutil.which("git")
    if git is None:
        return False
    probe = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed argv, shell=False.
        [git, "ls-files", "--", "install.sh"],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "install.sh"


NEEDS_WORK_TREE = pytest.mark.skipif(
    not _git_sees_the_tree(),
    reason="the builder ships git-tracked members; this tree is not a git work tree",
)


@NEEDS_WORK_TREE
def test_basic_tier_builds_and_passes_its_own_judge(tmp_path: Path) -> None:
    """``--tier basic`` exits 0: both cuts written AND accepted by ``assert_zip``."""
    out_dir = tmp_path / "dist"
    built = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed argv, no shell.
        [sys.executable, KIT_BUILDER, "--tier", "basic", "--out", str(out_dir)],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, (
        f"builder exited {built.returncode}:\n{built.stdout}\n{built.stderr}"
    )
    assert (out_dir / f"inbox-zero-connector-basic-{VERSION}.zip").is_file()
    assert (out_dir / f"inbox-zero-connector-basic-{VERSION}-with-tests.zip").is_file()
