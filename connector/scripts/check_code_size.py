#!/usr/bin/env python3
"""Enforce the CODE-line ceilings ruff cannot express: 500/file, 50/function.

WHY THIS SCRIPT EXISTS
    ruff has no line-count rule at all. ``PLR0915`` counts STATEMENTS, which is
    a different measure -- a 400-line function with three branches scores low
    and passes every complexity rule ruff has. The size LAW in
    ``_DOCS/STANDARDS-python.md`` is about CODE LINES, so it needs its own
    mechanism or it is a wish.

WHAT COUNTS AS A CODE LINE
    Blanks, comment-only lines, and docstrings are EXCLUDED. That exclusion is
    what makes the ceiling meaningful in this package specifically: the
    connector is deliberately docstring-heavy, because the docstrings are the
    security argument an auditor reads. Counting them would push every module
    over a limit that exists to catch logic sprawl, and the fix would be to
    delete the explanations -- exactly backwards.

    Implemented with ``ast`` plus ``tokenize`` rather than a regex: a regex
    cannot tell a docstring from a string assigned to a variable, and getting
    that wrong in either direction makes the number meaningless.

Exit codes:
    0  every file and function is within the ceilings
    1  a real finding -- something is over
    3  harness error; the check could not run and told you nothing

Usage:
    uv run python scripts/check_code_size.py            # src/ AND tests/
    uv run python scripts/check_code_size.py path.py    # specific files

WHY THE BARE RUN WALKS TESTS/ TOO (#24)
    The bare invocation is the receipt every lane in this kit runs, and it used
    to glob ``src`` alone -- so the tests tree, where sprawl accumulates
    fastest and nobody is watching, was unreachable from the exact command
    being trusted. The success line had the mirror-image defect: it always
    named the ``src`` constant, even on a run handed explicit paths somewhere
    else entirely, so a green receipt could name a root it never walked. Both
    roots are walked now, and the success line names what was actually
    examined, with its file count.
"""

from __future__ import annotations

import ast
import sys
import tokenize
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
"""The connector package root, resolved from THIS file's location."""

DEFAULT_ROOTS = (PROJECT_ROOT / "src", PROJECT_ROOT / "tests")
"""The trees a bare run walks: BOTH ``src`` and ``tests`` (#24).

This is the same set ``_githooks/pre-commit`` passes explicitly, so the bare
receipt and the hook now cover the same code. Walking ``src`` alone left the
tests tree unexamined by the one command every lane runs to claim the size LAW
holds, while the success line still named ``src`` -- a receipt that could name
a root it never walked.

Each root is verified to exist before scanning. A path derived from ``__file__``
breaks silently when the script moves: the standard records a sibling check that
resolved to an empty directory after a relocation and printed "Size OK" on every
run thereafter. The existence check turns that into an exit 3, not a false green.
"""

FILE_CEILING = 500
FUNCTION_CEILING = 50


def _docstring_line_numbers(tree: ast.AST) -> set[int]:
    """Every line occupied by a docstring, so they can be excluded from the count."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def _attribute_docstring_lines(source: str) -> set[int]:
    """Lines of bare string expressions used as attribute docstrings.

    This package documents module constants and model fields with a bare string
    on the line after the assignment (PEP 258 attribute docstrings). ``ast``
    does not attach those to anything, so they are found here by shape: an
    ``Expr`` whose value is a plain string constant, at any level.
    """
    lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


def code_lines(path: Path) -> set[int]:
    """Return the set of line numbers in ``path`` that are CODE.

    Args:
        path: A Python source file.

    Returns:
        Line numbers that are neither blank, comment-only, nor docstring.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    excluded = _docstring_line_numbers(tree) | _attribute_docstring_lines(source)

    comment_only = _comment_only_lines(path)
    return {
        number
        for number, text in enumerate(source.splitlines(), start=1)
        if text.strip() and number not in excluded and number not in comment_only
    }


def _comment_only_lines(path: Path) -> set[int]:
    """Lines whose entire content is a ``#`` comment.

    A trailing comment on a real statement is NOT one of these -- that line is
    code and counts. ``tokenize`` rather than a string check so a ``#`` inside
    a string literal is not mistaken for a comment.
    """
    lines: set[int] = set()
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type == tokenize.COMMENT and token.line.strip().startswith("#"):
                lines.add(token.start[0])
    return lines


def _function_findings(path: Path, counted: set[int]) -> list[str]:
    """Report every function in ``path`` over the per-function ceiling."""
    findings: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end = node.end_lineno or node.lineno
        size = len([n for n in counted if node.lineno <= n <= end])
        if size > FUNCTION_CEILING:
            findings.append(
                f"{path.name}:{node.lineno} {node.name}() is {size} code lines "
                f"(ceiling {FUNCTION_CEILING})"
            )
    return findings


def check(paths: list[Path]) -> list[str]:
    """Return every ceiling breach across ``paths``. Empty means clean."""
    findings: list[str] = []
    for path in sorted(paths):
        counted = code_lines(path)
        if len(counted) > FILE_CEILING:
            findings.append(f"{path.name} is {len(counted)} code lines (ceiling {FILE_CEILING})")
        findings.extend(_function_findings(path, counted))
    return findings


def _walk_default_roots() -> list[tuple[str, list[Path]]] | None:
    """Collect the ``.py`` files under each default root, with the root's name.

    Returns ``None`` after printing the HARNESS line when a default root is
    missing, so the caller can exit 3 rather than report a false green.
    """
    walked: list[tuple[str, list[Path]]] = []
    for root in DEFAULT_ROOTS:
        if not root.is_dir():
            print(f"HARNESS: default root does not exist: {root}")
            return None
        walked.append((root.name, sorted(root.rglob("*.py"))))
    return walked


def main(argv: list[str]) -> int:
    """Run the check. See the module docstring for the exit grammar."""
    if argv:
        targets = [Path(item) for item in argv]
        summary = f"Size OK ({len(targets)} file(s) examined: {', '.join(argv)})"
    else:
        walked = _walk_default_roots()
        if walked is None:
            return 3
        targets = [path for _, paths in walked for path in paths]
        counts = ", ".join(f"{name} {len(paths)} file(s)" for name, paths in walked)
        summary = f"Size OK: {counts} examined under {PROJECT_ROOT}"
    if not targets:
        print("HARNESS: no python files to examine -- a check that scanned nothing")
        return 3

    findings = check(targets)
    if findings:
        for finding in findings:
            print(finding)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
