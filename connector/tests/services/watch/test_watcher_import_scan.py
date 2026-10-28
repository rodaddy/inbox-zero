"""The round module neither sleeps nor schedules -- proven by its imports.

Split from ``test_watcher.py`` on 2026-09-01 when the #81 AST rewrite pushed
that module past the repo's 500-code-line ceiling
(``scripts/check_code_size.py``). The seam is the concern: these tests read
``round.py`` as SOURCE and assert about its import statements; nothing here
builds a provider or drives a round.
"""

from __future__ import annotations

import ast
from pathlib import Path

from inbox_zero_connector.services.watch import round as watcher

_SCHEDULING_MODULES = frozenset({"time", "sched", "signal", "asyncio"})
"""Modules whose presence in ``round.py`` would mean it schedules or sleeps."""


def _imported_module_roots(source: str) -> set[str]:
    """Every top-level module name ``source`` imports, from its PARSED tree.

    AST rather than a substring scan (#81). The scan read the file as text, so
    the word ``time`` inside a docstring or a comment tripped it -- and that
    module's docstrings discuss timing at length, which makes the assertion a
    prose gate rather than a code one. Parsing asks the question that was
    actually meant: does an ``import`` statement name one of these modules?
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_module_neither_sleeps_nor_schedules() -> None:
    """A single round. The loop above it is another module's job."""
    source = Path(watcher.__file__).read_text(encoding="utf-8")
    assert _imported_module_roots(source) & _SCHEDULING_MODULES == set()


def test_the_scan_reads_imports_and_not_prose() -> None:
    """Both sides of the AST rewrite, so neither can silently regress (#81).

    A real ``import time`` is caught; the same word in a docstring, a comment,
    or an attribute name is not. The second half is what the substring scan got
    wrong -- ``round.py``'s own docstrings say ``time`` -- and a gate that fires
    on prose gets its banned list trimmed rather than the module fixed.
    """
    assert "time" in _imported_module_roots("import time\n")
    assert "asyncio" in _imported_module_roots("from asyncio import sleep\n")
    assert _imported_module_roots('"""A round takes time. import time is banned."""\n') == set()
    assert _imported_module_roots("# import sched\nx = signal.alarm\n") == set()
