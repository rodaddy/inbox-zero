"""The standards rules no off-the-shelf linter can express, asserted here.

Three rules from ``_DOCS/STANDARDS-python.md`` are invisible to ruff and mypy,
because each is about WHERE something happens rather than whether the code is
well-formed. ``os.environ.get()`` inside a service is perfectly valid Python;
so is ``json.load(open("config.json"))``. Both pass every linter, and both were
real shipped bugs in the repos this standard was derived from.

    keystone       only config.py reads the environment
    no-print       only the CLI writes to stdout (here it is also a PROTOCOL
                   rule: stdout carries the MCP frames)
    size ceilings  500 code lines per file, 50 per function

Each check is written to fail on a deliberate violation, and the size one is
exercised RED against a planted oversize function in
``test_the_size_check_rejects_an_oversized_function``. A check nobody has seen
fail is not enforcement, it is a file.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "inbox_zero_connector"
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

PY_FILES = sorted(SRC.rglob("*.py"))

ENV_READERS = frozenset({"environ", "getenv"})

CONFIG_MODULES = frozenset({"config.py", "config_paths.py"})
"""The ONLY modules permitted to read the environment.

Narrower than the standard's own exemption list (which also frees
``settings.py`` and ``vault.py``): this package has neither, and an exemption
for a file that does not exist is an exemption waiting to be claimed by
whatever gets created with that name.

``config_paths.py`` is the keystone's PATHS HALF, split out of ``config.py``
for the 500-code-line ceiling and re-exported from it, exactly as
``apps/cli/*.py`` were split out of ``main.py`` below. It is one module by
every measure the law cares about -- same privilege, same audit surface, and
no caller imports it directly. Path resolution is the part of configuration
that cannot go through ``Settings``, because it decides where the layered
files that BUILD a ``Settings`` are read from; keeping it in ``config.py``
would have meant shrinking documentation to fit a ceiling. This entry is a
split, not a second reader: a THIRD name here needs a real argument.
"""

# apps/cli/desktop.py is the desktop-config subcommand's renderer,
# apps/cli/setup.py is the interactive first-install wizard, and
# apps/cli/show_scopes.py is the scope-manifest renderer, all split out of
# main.py for the code-line ceiling: CLI-only, dispatched by main.py, never
# imported by the server process, and their stdout is their product. setup.py
# additionally refuses to run at all without a TTY on both streams, so its
# prints cannot reach a served process even by accident.
STDOUT_WRITERS = frozenset({
    "apps/cli/main.py",
    # credentials.py holds the auth/logout verbs, split out of main.py for the
    # code-line ceiling. Its stdout IS the product: the scope trade is printed
    # at the consent screen, and the logout prompt is a question a human answers.
    "apps/cli/credentials.py",
    "apps/cli/desktop.py",
    "apps/cli/diagnose.py",
    "apps/cli/init_config.py",
    "apps/cli/setup.py",
    "apps/cli/show_scopes.py",
    "apps/cli/list_folders.py",
    "apps/cli/voice.py",
    "apps/cli/digest.py",
    # dry_run_preview.py renders the watch --once --dry-run table; the preview
    # IS its product and it never runs in the server process (#66).
    "apps/cli/dry_run_preview.py",
    "apps/cli/service_unit.py",
    # uninstall.py IS a printout: the guided offboarder's product is the list of
    # steps it deliberately does not take for the operator (#68). CLI-only.
    "apps/cli/uninstall.py",
    # undo.py renders the reversal preview; the table IS its product, and the
    # printed refusal is what tells an operator why the mailbox is unchanged
    # (#66). CLI-only; never in the server process.
    "apps/cli/undo.py",
    # status.py prints the watcher health readout; the three-line report IS its
    # product and the exit code is the other half of it (#64). CLI-only.
    "apps/cli/status.py",
    # update.py prints the version verdict; that one line IS its product, and it
    # is the whole point of the verb (#67). CLI-only, never in the server.
    "apps/cli/update.py",
    "providers/outlook/consent.py",
})
"""Modules allowed a ``print``. All are CLI-path only.

The CLI modules' product IS their stdout, and ``apps/cli/show_scopes.py``
keeps printing rather than returning text because the diagnose bundle captures
that same stdout. ``providers/outlook/consent.py``
prints the Microsoft browser prompt and device code, on the interactive CLI
path only -- the MCP server never reaches it, because ``provider.py`` passes
``interactive=False``, which raises first.
"""


def test_the_source_tree_was_actually_found() -> None:
    """A check that examined nothing is not a pass. Assert it examined something."""
    assert SRC.is_dir(), f"package root does not exist: {SRC}"
    assert len(PY_FILES) >= 10, f"only {len(PY_FILES)} file(s) found under {SRC}"


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_only_the_config_keystone_reads_the_environment(path: Path) -> None:
    """LAW: nothing but config.py touches os.environ / os.getenv.

    This is what makes every component's inputs arrive through a constructor
    and therefore replaceable in a test. It is also a security property here:
    the environment holds the buyer's OAuth client identifiers, so one module
    reads them and one module can be audited for what it does with them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in ENV_READERS
    ]
    offenders += [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "getenv"
    ]
    if path.name in CONFIG_MODULES:
        assert offenders, "config.py is the keystone and must read the environment"
        return
    assert offenders == [], (
        f"environment read outside the config keystone: {offenders}. "
        "Move the setting into config.Settings and pass it down."
    )


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_nothing_but_the_cli_writes_to_stdout(path: Path) -> None:
    """STDOUT IS THE MCP PROTOCOL CHANNEL. One stray print corrupts a frame.

    ruff's T20 already refuses ``print`` package-wide, with per-file exemptions
    for the two CLI-path modules. This asserts the EXEMPTION LIST itself has
    not grown -- a per-file-ignore added in pyproject is invisible to the
    linter's own output, so nothing else would notice.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prints = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    if path.relative_to(SRC).as_posix() in STDOUT_WRITERS:
        return
    assert prints == [], (
        f"{path.name} writes to stdout at line(s) {prints}. stdout carries the MCP "
        "JSON-RPC frames; log to stderr through loguru instead."
    )


def _run_size_check(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the size check as a subprocess -- the thing under test is a script."""
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "check_code_size.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_package_is_within_the_code_line_ceilings() -> None:
    """LAW: 500 code lines per file, 50 per function. ruff has no line rule."""
    result = _run_size_check()
    assert result.returncode == 0, f"size ceiling breached:\n{result.stdout}"


def test_the_size_check_rejects_an_oversized_function(tmp_path: Path) -> None:
    """RED proof. Without this, a passing size check could be scanning nothing.

    The standard records exactly this failure: a size checker whose path
    constant went stale after a move printed "Size OK" on every run because it
    examined an empty directory, and nothing noticed.
    """
    probe = tmp_path / "oversized.py"
    body = "\n".join(f"    total += {index}" for index in range(60))
    probe.write_text(
        f'"""Deliberately oversized."""\n\n\ndef bloated() -> int:\n    total = 0\n{body}\n'
        "    return total\n",
        encoding="utf-8",
    )
    result = _run_size_check(str(probe))
    assert result.returncode == 1, (
        f"the size check did NOT reject an oversized function:\n{result.stdout}"
    )
    assert "bloated()" in result.stdout


def test_the_size_check_ignores_comments_and_docstrings(tmp_path: Path) -> None:
    """The complement: a check that flagged documentation would teach deletion.

    This package is deliberately docstring-heavy, because the docstrings are
    the security argument an auditor reads. If they counted toward the ceiling,
    the cheapest way to go green would be to delete the explanations.
    """
    probe = tmp_path / "documented.py"
    comments = "\n".join(f"    # explanatory line {index}" for index in range(200))
    probe.write_text(
        f'"""Padded with comments and docstrings."""\n\n\ndef fine() -> int:\n'
        f'    """A long docstring does not make a function oversized."""\n{comments}\n'
        "    return 1\n",
        encoding="utf-8",
    )
    result = _run_size_check(str(probe))
    assert result.returncode == 0, f"documentation was counted as code:\n{result.stdout}"
