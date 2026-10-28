"""Docstring census for the exemplar-conformance audit, and the gate that holds it.

Walks every .py under src/ and reports, deterministically:
  - modules with no module docstring, or a docstring under 3 lines
  - every function/method (def / async def) with no docstring
  - function docstrings that are a single line on functions >10 code lines

Default mode is report-only: print every finding, exit 0. That is what an
audit wants -- the whole picture, including the editorial notes, with the
judgment (does the prose state HOW and WHY) left to the reviewer.

``--check`` is the same report used as a gate, for the pre-commit hook. It
exits 1 when a MODULE-NO-DOC or FUNC-NO-DOC line was printed, because those
are MECHANICAL facts: the docstring is either there or it is not, and a
machine can be trusted with that call. MODULE-THIN-DOC and FUNC-ONE-LINE-DOC
stay advisory in both modes -- "this should say more" is an editorial
judgment behind a line-count proxy, and blocking on it would train people to
pad docstrings to clear the threshold rather than write better ones.

Exit grammar (_graph/done-means/README.md): 0 pass, 1 the thing under test
failed, 3 harness error -- an unreadable or absent src/ tree, or a file that
does not parse, which means the checker cannot answer rather than that the
answer is bad.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_HARNESS = 3


def code_lines(node: ast.AST) -> int:
    """Rough code-line span of a node (end_lineno - lineno)."""
    return (getattr(node, "end_lineno", 0) or 0) - (getattr(node, "lineno", 0) or 0)


def audit_module(path: Path) -> int:
    """Print one module's findings; return how many of them block.

    Blocking is the mechanical half only -- MODULE-NO-DOC and FUNC-NO-DOC.
    Raises OSError/SyntaxError/UnicodeDecodeError to the caller, which owns
    the harness-error verdict; a file that will not parse is a broken checker
    input, not a docstring finding.
    """
    rel = path.relative_to(SRC.parent.parent)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    blocking = 0
    mod_doc = ast.get_docstring(tree)
    if not mod_doc:
        print(f"MODULE-NO-DOC {rel}")
        blocking += 1
    elif len(mod_doc.splitlines()) < 3:
        print(f"MODULE-THIN-DOC {rel} ({len(mod_doc.splitlines())} lines)")
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        doc = ast.get_docstring(node)
        if doc is None:
            print(f"FUNC-NO-DOC {rel}:{node.lineno} {node.name}")
            blocking += 1
        elif len(doc.splitlines()) == 1 and code_lines(node) > 10:
            print(f"FUNC-ONE-LINE-DOC {rel}:{node.lineno} {node.name} ({code_lines(node)} lines)")
    return blocking


def main() -> int:
    """Print the census to stdout, one finding per line; gate on --check.

    Returns EXIT_HARNESS when the tree cannot be read or parsed, EXIT_FINDING
    when --check was passed and a blocking finding was printed, else EXIT_OK.
    The report is emitted identically in both modes: --check changes only the
    verdict, never the output, so a developer sees the same lines whether the
    hook ran it or they did.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any module or function is missing a docstring",
    )
    args = parser.parse_args()

    if not SRC.is_dir():
        print(f"HARNESS-ERROR src tree not found: {SRC}", file=sys.stderr)
        return EXIT_HARNESS

    blocking = 0
    for path in sorted(SRC.rglob("*.py")):
        try:
            blocking += audit_module(path)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            print(f"HARNESS-ERROR cannot read {path}: {exc}", file=sys.stderr)
            return EXIT_HARNESS

    if args.check and blocking:
        print(
            f"\nFAILED: {blocking} module(s)/function(s) with no docstring.",
            file=sys.stderr,
        )
        return EXIT_FINDING
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
