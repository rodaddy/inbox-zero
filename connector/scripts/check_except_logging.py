#!/usr/bin/env python3
"""Enforce the rule ruff cannot express: no except body that neither logs nor raises.

WHY THIS SCRIPT EXISTS
    ``_DOCS/STANDARDS-observability.md`` ## Enforcement names this as a REQUIRED
    CUSTOM CHECK, in those words: "every catch/except body contains a log call
    or a re-raise -- a custom rule; stock 'no empty block' checks miss the
    bound-but-unlogged case."

    That last clause is the whole point. ruff's ``S110`` catches ``except:
    pass`` and ``E722`` catches a bare ``except:``, and BOTH are satisfied by
    the failure this rule is actually about::

        except (OSError, JSONDecodeError):
            return None          # not empty, not bare, and tells nobody

    A handler that returns a fallback value is not an empty block, so every
    stock rule passes it. Six of the seven handlers this check was written
    against were exactly that shape.

WHY AST AND NOT A REGEX
    A regex cannot tell ``logger.warning(...)`` from the word "log" inside a
    docstring, and this package is deliberately docstring-heavy -- its
    docstrings are the security argument an auditor reads, and several of them
    discuss logging at length. A regex would find "logger" in the prose
    explaining why a handler logs and pass a handler that does not.

WHAT SATISFIES THE RULE
    Any of the following, ANYWHERE in the handler body including nested inside
    an ``if``:

      * a call on a logger -- ``logger.warning(...)``, ``self._log.error(...)``,
        or any attribute call whose base name is in ``LOGGER_NAMES``
      * a call to one of this package's five-point emitters -- ``call.guard()``,
        ``call.fallback()``, ``call.retry()`` -- which log by construction
      * a bare ``raise``
      * ``raise X from exc`` / any ``raise``

    A nested ``def`` or ``class`` inside the handler does NOT count on its
    behalf: a logger call inside a function that is merely DEFINED there has
    not run. Walking into it would pass a handler whose only log line is in
    dead code.

WHAT THIS CHECK DOES NOT CLAIM
    It proves a handler SAYS something, not that it says the right thing at the
    right level. A ``logger.debug`` on a path that warranted ``error`` passes
    here and is a review question, not a mechanical one. This is a floor.

Exit codes:
    0  every except handler logs or re-raises
    1  a real finding -- a silent handler
    3  harness error; the check could not run and told you nothing

Usage:
    uv run python scripts/check_except_logging.py            # the whole package
    uv run python scripts/check_except_logging.py path.py    # specific files
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Final

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src"
"""Resolved from THIS file's location, and verified to exist before scanning.

Same reasoning as ``check_code_size.py``: a path constant derived from
``__file__`` breaks silently when the script moves, and the sibling check in
the standard printed "OK" on every run for exactly that reason. The existence
guard in ``main`` turns that into exit 3 rather than a false green.
"""

LOGGER_NAMES: Final[frozenset[str]] = frozenset({"logger", "log", "logging", "_logger", "_log"})
"""Base names whose attribute calls count as logging.

``logger`` is this package's convention (``from loguru import logger``, used in
``watcher.py``, ``ledger.py``, and now four more). The rest are here so the
check does not silently stop enforcing if a module adopts a different but
still-obvious binding.
"""

EMITTER_METHODS: Final[frozenset[str]] = frozenset({"guard", "fallback", "retry", "record_refusal"})
"""Five-point emitter methods that log by construction.

``record_refusal`` is ``services/watch/receipts.record_refusal``: the whole
refusal arm of a filing (ERROR log with the live traceback, FAILED ledger row,
intent settle, trace) moved there at 0.2.14 so every audit write sits in one
module, and the ``except`` in ``round.py`` that calls it holds no logger line
of its own.

``ProviderCall``/``ToolCall`` in ``utils/logging_config.py`` wrap
``logger.warning`` -- ``call.fallback("reason")`` IS the fallback log point the
observability standard asks for. Accepted by METHOD NAME rather than by
resolving the receiver's type, because the receiver is a local whose type this
check cannot see; the names are distinctive enough that a false accept is
unlikely and a false REJECT would push authors toward a redundant second line.
"""


def _is_logging_call(node: ast.Call) -> bool:
    """True when ``node`` reports the error: logger, emitter, or a script's print.

    ``print`` counts ONLY because of where this rule is applied. In the package
    it would not: ``test_standards_enforcement.py`` enforces that nothing but
    the CLI writes to stdout, since stdout carries the MCP JSON-RPC frames, so
    a ``print`` in a handler there is caught by that check instead. In
    ``scripts/`` -- run by the git hook and by CI -- stdout IS the report and no
    loguru sink is configured, so a logger call would write where nobody is
    reading. Rejecting ``print`` here would push these scripts toward a log line
    the operator never sees, which is the silence the rule exists to prevent.
    """
    func = node.func
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in EMITTER_METHODS:
        return True
    base = func.value
    while isinstance(base, ast.Attribute):
        base = base.value
    return isinstance(base, ast.Name) and base.id in LOGGER_NAMES


def _satisfying_nodes(handler: ast.ExceptHandler) -> list[ast.AST]:
    """Every node in ``handler`` that satisfies the rule, excluding nested defs.

    Nested ``def``/``class`` bodies are pruned deliberately: a logger call in a
    function that is only DEFINED inside the handler has not run, and counting
    it would pass a handler whose only log line is unreachable.
    """
    found: list[ast.AST] = []
    queue: list[ast.AST] = list(handler.body)
    while queue:
        node = queue.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        # A raise and a logging call are the SAME answer to this question --
        # "did this handler do something about the error" -- so they are one
        # condition, not two arms that happen to agree.
        if isinstance(node, ast.Raise) or (isinstance(node, ast.Call) and _is_logging_call(node)):
            found.append(node)
        queue.extend(ast.iter_child_nodes(node))
    return found


def _handler_findings(path: Path, tree: ast.AST) -> list[str]:
    """Report every except handler in ``tree`` that neither logs nor re-raises."""
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if _satisfying_nodes(node):
            continue
        findings.append(
            f"{path}:{node.lineno} except handler neither logs nor re-raises "
            "(log it, then re-raise or document the fail-open)"
        )
    return findings


def check(paths: list[Path]) -> list[str]:
    """Return every silent handler across ``paths``. Empty means clean.

    Raises:
        SyntaxError: A file could not be parsed. Propagated so ``main`` can
            report exit 3 -- a file this check could not read is a file it
            proved nothing about, and skipping it would be a false green.
    """
    findings: list[str] = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        findings.extend(_handler_findings(path, tree))
    return findings


def main(argv: list[str]) -> int:
    """Run the check. See the module docstring for the exit grammar."""
    if argv:
        targets = [Path(item) for item in argv]
    else:
        if not PACKAGE_ROOT.is_dir():
            print(f"HARNESS: package root does not exist: {PACKAGE_ROOT}")
            return 3
        targets = sorted(PACKAGE_ROOT.rglob("*.py"))
    if not targets:
        print("HARNESS: no python files to examine -- a check that scanned nothing")
        return 3

    missing = [path for path in targets if not path.is_file()]
    if missing:
        for path in missing:
            print(f"HARNESS: no such file: {path}")
        return 3

    try:
        findings = check(targets)
    except (SyntaxError, UnicodeDecodeError) as exc:
        # DOCUMENTED FAIL-CLOSED, and the reason this handler does not use a
        # logger: this is a standalone check script run by a git hook and by
        # CI, where the report IS stdout and the exit code. It has no loguru
        # sink configured, so a logger call here would emit to a stream nobody
        # is reading while the operator saw nothing. `print` is the reporting
        # channel for this file, exactly as it is in `check_code_size.py`.
        #
        # The error is neither swallowed nor downgraded -- it is reported with
        # its message and returns exit 3, which the hook treats as a failure. A
        # file this check could not parse is a file it proved nothing about.
        print(f"HARNESS: could not parse a target, so nothing was proved: {exc}")
        return 3

    if findings:
        for finding in findings:
            print(finding)
        return 1
    print(f"Except-logging OK ({len(targets)} file(s) examined under {PACKAGE_ROOT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
