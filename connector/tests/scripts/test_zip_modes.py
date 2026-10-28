"""Executable-bit contract: a shipped ``install.sh`` must arrive runnable.

Both builders write every member through an explicit ``zipfile.ZipInfo``
rather than ``ZipFile.write``, because the convenience method stamps the
file's real mtime and byte-stability depends on a fixed epoch. The cost of
that choice is that ``external_attr`` is no longer derived from anything: it
is whatever the builder assigns, and a single hardcoded ``0o644 << 16``
silently stripped the executable bit off every script in the kit. The
customer symptom is ``./install.sh`` exiting 126 -- permission denied -- on a
fresh unzip, which reads as a broken download rather than a packaging defect.

The mode is taken from the GIT INDEX (``git ls-files -s``), not from the
working tree's ``stat``. The index is the same on every checkout and in CI,
so two builds of one commit still agree byte-for-byte; a working-tree stat
varies with umask, filesystem, and how the tree was created, which would make
the artifact's bytes depend on the machine that built it.

These tests drive the builders as SUBPROCESSES for the same reason
``test_build_custom.py`` does: ``scripts/`` is not a package, and the exit
code plus the artifact on disk are the contract the packager depends on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- the scripts ARE the thing under test.
import sys
import zipfile
from pathlib import Path

import pytest

CONNECTOR_ROOT = Path(__file__).resolve().parents[2]
KIT_BUILDER = "scripts/packaging/build_zips.py"
CUSTOM_BUILDER = "scripts/packaging/build_custom.py"

VERSION = (CONNECTOR_ROOT / "VERSION").read_text(encoding="utf-8").strip()

# The shipped shell entrypoints. Each is mode 100755 in the git index, so
# each must land in the zip with its executable bits intact.
EXECUTABLE_MEMBERS = ("install.sh", "examples/tier-ai.sh", "examples/tier-basic.sh")

# A member that is emphatically NOT executable. Without it the test would
# pass just as well against a builder that stamped 0o755 on everything, which
# would hand the customer a world-executable README and a config file.
PLAIN_MEMBER = "README.md"


def _git_sees_the_tree() -> bool:
    """Whether ``git ls-files`` from the connector root reports a real work tree.

    Both builders ship git-TRACKED members only, so the happy path needs one.
    The pre-commit hook runs this suite inside a ``git checkout-index`` copy
    under ``.git/``, which is not a work tree: there the member list is empty
    by construction and these tests would report a defect that does not exist.
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
    reason="builders ship git-tracked members; this tree is not a git work tree of the connector",
)

FAKE_VAULT = """\
import json
import sys

params = json.loads(sys.argv[sys.argv.index("--params") + 1])
print(json.dumps({"success": True, "result": {
    "name": params["query"],
    "value": "sk-FAKE-value",
    "fields": {"password": "sk-FAKE-password"},
}}))
"""

PROFILE = {
    "name": "modecheck",
    "zip_tier": "enhanced",
    "config": {
        "logging": {"level": "DEBUG"},
        "credentials": {"google_client_id": "1234-abc.apps.googleusercontent.com"},
    },
    "secrets": {"credentials": {"classifier_api_key": {"vault": "entry-x", "field": "password"}}},
}


def _modes(zip_path: Path) -> dict[str, int]:
    """Unix permission bits for every member of ``zip_path``, by arcname."""
    with zipfile.ZipFile(zip_path) as archive:
        return {info.filename: (info.external_attr >> 16) & 0o777 for info in archive.infolist()}


def _assert_modes(zip_path: Path) -> None:
    """Every shell entrypoint is 0o755 and the plain member is not executable."""
    modes = _modes(zip_path)
    for member in EXECUTABLE_MEMBERS:
        assert member in modes, f"{member} missing from {zip_path.name}"
        assert modes[member] == 0o755, f"{member} stored as {modes[member]:o}, expected 755"
    assert modes[PLAIN_MEMBER] == 0o644, f"{PLAIN_MEMBER} stored as {modes[PLAIN_MEMBER]:o}"


@NEEDS_WORK_TREE
def test_kit_zip_stores_shell_entrypoints_executable(tmp_path: Path) -> None:
    """``build_zips.py`` preserves the git index mode on every member it ships."""
    out_dir = tmp_path / "dist"
    built = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed argv, no shell.
        [sys.executable, KIT_BUILDER, "--tier", "enhanced", "--out", str(out_dir)],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, f"builder exited {built.returncode}: {built.stderr}"
    _assert_modes(out_dir / f"inbox-zero-connector-enhanced-{VERSION}.zip")


@NEEDS_WORK_TREE
def test_custom_zip_stores_shell_entrypoints_executable(tmp_path: Path) -> None:
    """``build_custom.py`` preserves the same modes; it has its own writer."""
    vault = tmp_path / "vault.py"
    vault.write_text(FAKE_VAULT, encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(PROFILE), encoding="utf-8")
    out_dir = tmp_path / "handoff"

    built = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed argv, no shell.
        [
            sys.executable,
            CUSTOM_BUILDER,
            "--profile",
            str(profile),
            "--out",
            str(out_dir),
            "--vault-cmd",
            f"{sys.executable} {vault}",
        ],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, f"builder exited {built.returncode}: {built.stderr}"
    zips = sorted(out_dir.glob("*.zip"))
    assert len(zips) == 1, f"expected one artifact, got {[z.name for z in zips]}"
    _assert_modes(zips[0])


@NEEDS_WORK_TREE
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows cannot exec a .sh directly (WinError 193); the mode assertions still cover the contract",
)
def test_unzipped_install_script_is_runnable(tmp_path: Path) -> None:
    """The customer-visible proof: unzip, then run ``install.sh`` without 126.

    Exit 126 is the shell's "found it, cannot execute it" -- the exact symptom
    of a stripped executable bit. The script's OWN exit code is not asserted:
    running the real installer's argument parsing is not this test's business,
    only that the kernel was willing to start it at all.
    """
    unzip = shutil.which("unzip")
    if unzip is None:
        pytest.skip("unzip is not installed; the mode assertions still cover the contract")

    out_dir = tmp_path / "dist"
    built = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed argv, no shell.
        [sys.executable, KIT_BUILDER, "--tier", "enhanced", "--out", str(out_dir)],
        cwd=CONNECTOR_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, f"builder exited {built.returncode}: {built.stderr}"

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed argv, no shell.
        [
            unzip,
            "-q",
            str(out_dir / f"inbox-zero-connector-enhanced-{VERSION}.zip"),
            "-d",
            str(extracted),
        ],
        capture_output=True,
        check=True,
    )

    script = extracted / "install.sh"
    assert script.is_file(), "install.sh did not survive the round trip"
    ran = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed argv, no shell.
        [str(script), "--help"],
        cwd=extracted,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode != 126, "install.sh unzipped without its executable bit (exit 126)"
