"""The lazy add-on loader, driven by modules that are REALLY broken on disk.

WHY A SECOND LOADER MODULE EXISTS BESIDE
``test_watcher_containment_edges.py``. That file proves #174's split by
patching the ``import_module`` name and handing the loader an exception object
built in the test -- a ``ModuleNotFoundError`` with ``name`` set by hand. That
is the right shape for asserting which ARM a given exception takes, and it is
the wrong shape for asserting which exception the interpreter actually raises,
because the test author picks both. Issue #189 is exactly the gap between the
two: a circular import inside an INSTALLED add-on raises ``ImportError`` whose
``name`` is the add-on's own dotted path, so the hand-built fixtures never
produced the case that routed a broken paid install to the quiet arm.

So every failure here is produced by the import system rather than described to
it. Each test writes a real package into ``tmp_path``, puts that directory on
``sys.path``, points the loader's module name at it, and lets the interpreter
fail on its own terms:

    a module that imports itself           -> ``ImportError``, ``name`` == itself
    a module with a syntax error           -> ``SyntaxError``, not an ImportError
    a module importing an absent dependency-> ``ModuleNotFoundError``, dep name
    a package that is simply not there     -> ``ModuleNotFoundError``, own name

AND SINCE #189 ROUND 4, THE SAME FOUR FAILURES IN THE PARENT ``__init__.py``.
Every row above plants an EMPTY parent package and breaks the child, which is
one of two places the interpreter runs add-on code: resolving a child forces the
parent to execute, so a parent that fails to import is reached by
``_add_on_is_on_disk``'s ``find_spec`` -- a call that used to sit OUTSIDE
``_drafting``'s try and caught only ``(ImportError, ValueError)``. A parent
``SyntaxError`` propagated out of the loader entirely, uncached and once per
message, and a parent importing a missing dependency was read as "not
installed", silently disabling paid drafting. Those cases are the
``_parent_*`` rows below, and they are the ones this file existed to catch and
did not, because ``_plant`` wrote an empty ``__init__.py`` every time.

THE ADD-ON'S REAL DOTTED PATH NEVER APPEARS HERE, and it does not need to. The
loader reads ``drafting_loader._DRAFTING_MODULE`` at call time, so pointing that
name at a planted module exercises the same code with none of the add-on's own
bytes involved -- which also keeps this file shippable in the basic zip, where
``scripts/packaging/tiers.json`` refuses the add-on's dotted path as a raw
substring.

#203 -- THE DISCRIMINATOR IS GONE, AND TWO ROWS HERE USED TO PIN ITS FICTION.
Four rounds of deciding whether a ``SystemExit`` was the operator's or the
add-on's all read the TRACEBACK, which answers which file ran, as a stand-in for
who asked. Each shipped and each was refuted by another way for a broken
add-on's exit to arrive wearing somebody else's frames -- a class body, an
ancestor ``__init__.py``, and finally import machinery the loader itself invoked
(a ``sys.meta_path`` finder, a ``sys.path_hooks`` entry), whose frames belong to
neither party. The last of those escaped the loader uncached, once per message,
while the identical raise site with ``RuntimeError`` was contained and loud.

The rule now has one source of truth: the loader is what asked for this import,
so everything the import raises is the add-on's, ``KeyboardInterrupt`` excepted.
``test_an_exit_from_import_machinery_on_a_healthy_tree_is_contained_and_loud``
and ``test_a_system_exit_with_no_add_on_frames_is_still_the_add_ons`` are the
two rows that asserted the opposite and now assert containment; the finder and
path-hook rows at the end of this file are the mechanisms that retired the walk.

THE TARGET IS ``drafting_loader`` RATHER THAN ``round`` SINCE #189 ROUND 7, and
the aim has to move with the code. The loader is its own module now -- it
answers a packaging question and never touches a message, a cursor, or a
provider -- and it reads its OWN globals, so a name re-aimed on ``round``'s
re-export would leave the loader still looking at the real add-on. ``round``
re-exports ``_drafting`` for its callers, and the rows that drive a round
end-to-end live in ``test_watcher_containment_edges.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from loguru import logger

from inbox_zero_connector.services.watch import drafting_loader as loader

PLANTED_PACKAGE = "izc_fake_addon"
"""The planted package's top-level name. Unique enough to never collide."""

PLANTED_VENDOR = "izc_fake_vendor"
"""A third-party SDK the planted add-on imports, for the shape #189 round 5 names.

A paid add-on's own ``__init__`` is usually a few imports; the module body that
blows up belongs to something it depends on. Keeping the vendor separate means
the failure arrives through an ``import`` statement in the add-on rather than
being written into the add-on itself, which is the real install shape.
"""


@pytest.fixture(autouse=True)
def _clear_loader_cache() -> Iterator[None]:
    """The tier switch, cleared on both sides so every row starts cold.

    ``_drafting`` is the loader's ONE cached answer since #203. It used to be
    two: ``_add_on_install_files`` cached where the add-on's install tree sat on
    disk, for the frame-walking discriminator that decided whether a
    ``SystemExit`` was the operator's. That discriminator is gone -- the loader
    asked for the import, so what the import raised is the add-on's -- and the
    cache went with it.

    Clearing on BOTH sides is what keeps these rows independent: they reuse
    ``PLANTED_PACKAGE`` across tests while planting different trees under it in
    a fresh ``tmp_path``, so an answer surviving a row would decide the next
    one against a tmpdir that no longer exists.
    """
    loader._drafting.cache_clear()
    yield
    loader._drafting.cache_clear()


@pytest.fixture
def captured_logs() -> Iterator[list[str]]:
    """Every WARNING-or-above line loguru emitted, as plain strings.

    A loguru SINK rather than pytest's ``caplog``, because this package logs
    through loguru and loguru does not propagate to the stdlib ``logging`` tree
    that ``caplog`` reads. Under ``caplog`` these assertions would pass or fail
    on an always-empty list, which is worse than no assertion.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING", format="{level} {message}")
    yield lines
    logger.remove(sink_id)


def _plant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str | None,
    module: str = "drafting",
    parent_body: str = "",
) -> str:
    """Write a real add-on module to disk and aim the loader at it.

    Args:
        tmp_path: pytest's per-test directory. Becomes the ``sys.path`` entry.
        monkeypatch: Undoes the ``sys.path`` entry and the name aim afterwards.
        body: The module's source, or None to plant the package with no such
            module in it -- the basic tier's shape, where the add-on is absent.
        module: The submodule name to point the loader at.
        parent_body: Source for the package's ``__init__.py``. Empty is a
            healthy parent and the default every pre-#189-round-4 row uses.
            Anything that fails to execute reaches the loader through
            ``find_spec``, because resolving a child imports its parent.

    Returns:
        The dotted path the loader will now try to import.
    """
    package = tmp_path / PLANTED_PACKAGE
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text(parent_body, encoding="utf-8")
    dotted = f"{PLANTED_PACKAGE}.{module}"
    if body is not None:
        (package / f"{module}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(loader, "_DRAFTING_MODULE", dotted)
    _evict(monkeypatch, PLANTED_PACKAGE, PLANTED_VENDOR)
    return dotted


def _plant_vendor(tmp_path: Path, body: str) -> None:
    """Write a third-party SDK module beside the planted add-on package.

    SEPARATE FROM ``_plant`` because it is a separate FILE with a separate
    owner: a paid add-on's ``__init__`` is a few imports, and the module body
    that blows up belongs to something it depends on. Planting the failure in
    the vendor rather than in the add-on makes it arrive through an ``import``
    statement, which is the real install shape and the one #189 round 5 names.

    Args:
        tmp_path: The ``sys.path`` entry ``_plant`` prepends.
        body: The vendor module's source.
    """
    (tmp_path / f"{PLANTED_VENDOR}.py").write_text(body, encoding="utf-8")


def _plant_three_levels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    top_body: str,
) -> None:
    """Plant a GRANDPARENT / package / module tree and aim the loader at it.

    THE INSTALL SHAPE THE ADD-ON ACTUALLY HAS, which two levels cannot express.
    The paid add-on ships inside this connector's own distribution, so the
    import path that reaches it runs a top-level package's ``__init__.py``
    before it runs the add-on's own -- and that ancestor file is where #189
    round 7's escape lives. ``_plant`` above writes only a package and a module,
    so no row built on it can reach the tier this one describes.

    ONLY THE TOP LEVEL VARIES. The middle package and the module are healthy in
    every row, so a failure can only have come from the grandparent, and the
    rows differ from each other in exactly one line of one file.

    Args:
        tmp_path: pytest's per-test directory. Becomes the ``sys.path`` entry.
        monkeypatch: Undoes the ``sys.path`` entry and the name aim afterwards.
        top_body: Source for the GRANDPARENT package's ``__init__.py``. Empty
            is a healthy install.
    """
    top = tmp_path / PLANTED_PACKAGE
    middle = top / "voice"
    middle.mkdir(parents=True)
    (top / "__init__.py").write_text(top_body, encoding="utf-8")
    (middle / "__init__.py").write_text("", encoding="utf-8")
    (middle / "drafting.py").write_text("MARKER = 'loaded'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(loader, "_DRAFTING_MODULE", f"{PLANTED_PACKAGE}.voice.drafting")
    _evict(monkeypatch, PLANTED_PACKAGE)


def _evict(monkeypatch: pytest.MonkeyPatch, *prefixes: str) -> None:
    """Drop already-imported planted modules so each row starts from a cold import.

    A module that failed to import leaves nothing in ``sys.modules``, but one
    that SUCCEEDED stays, and these rows reuse the same names across tests. A
    stale hit would make a row pass on the previous row's module.

    Args:
        monkeypatch: Restores the entries afterwards.
        prefixes: Dotted-path prefixes to evict.
    """
    for name in [name for name in sys.modules if name.startswith(prefixes)]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def test_a_circular_import_inside_an_installed_add_on_is_reported_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#189): a circular import took the QUIET not-installed arm.

    The module below imports a name from itself, which is what a hand-patched
    or half-refactored add-on does. The interpreter raises ``ImportError`` and
    sets ``exc.name`` to the partially initialized module -- the add-on's OWN
    dotted path. The pre-#189 loader compared ``exc.name`` to
    ``_DRAFTING_MODULE``, matched, and logged DEBUG "not installed": a paid
    add-on that is physically present and broken, reported as software the
    operator chose not to buy.

    The discriminator is the SPEC. ``find_spec`` succeeding means the files are
    there, so an import that then fails is a fault and not an absence.

    RED-FIRST, observed before the fix::

        AssertionError: a broken INSTALLED add-on must be reported at WARNING
        or above; captured=[]
    """
    dotted = _plant(
        tmp_path,
        monkeypatch,
        body=f"from {PLANTED_PACKAGE}.drafting import should_draft\n\ndef should_draft() -> bool:\n    return True\n",
    )

    assert loader._drafting() is None, "a broken add-on still degrades to no drafting"

    assert any(dotted in line for line in captured_logs), (
        f"a broken INSTALLED add-on must be reported at WARNING or above; captured={captured_logs}"
    )


def test_a_syntax_error_in_the_add_on_is_caught_and_cached_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#189): a ``SyntaxError`` escaped the loader and re-ran per message.

    ``SyntaxError`` is not an ``ImportError``, so the pre-#189 catch never saw
    it. The outer ``_apply`` boundary contained it, which kept the round alive
    -- but ``functools.cache`` stores NOTHING when the wrapped call raises, so
    the next message re-imported the same broken file and logged the same
    complaint again. One typo in a hand-patched install produced one WARNING
    per message for the life of the process.

    Both halves are asserted here: the call answers None rather than raising,
    and a SECOND call neither re-imports nor logs a second time.

    RED-FIRST, observed before the fix::

        SyntaxError: invalid syntax (drafting.py, line 1)
    """
    _plant(tmp_path, monkeypatch, body="def should_draft(:\n    return True\n")

    assert loader._drafting() is None, "a syntax error must be contained by the loader"
    first_round_lines = list(captured_logs)
    assert first_round_lines, "a broken INSTALLED add-on must be reported once"

    assert loader._drafting() is None
    assert captured_logs == first_round_lines, (
        "the failure must be cached, not re-imported and re-logged for every message; "
        f"captured={captured_logs}"
    )


def test_a_missing_dependency_of_an_installed_add_on_stays_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """#174's arm, now proven with a module the interpreter really imported.

    The add-on is present and imports a third-party name that is not installed,
    which is the original ticket's case. It stays loud after #189 widened the
    discriminator: the spec resolves, so the install is broken rather than
    absent, and the failing dependency's name is the operator's diagnosis.
    """
    missing = "izc_absent_third_party"
    _plant(tmp_path, monkeypatch, body=f"import {missing}\n")

    assert loader._drafting() is None

    assert any(missing in line for line in captured_logs), (
        f"the failing dependency must be named at WARNING or above; captured={captured_logs}"
    )


def test_an_add_on_that_is_genuinely_absent_stays_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """THE BASIC TIER, and the regression #189's fix could have caused.

    Making the broken case loud is only correct if the absent case stays
    silent. Here the package exists and the module inside it does not, which is
    what an install without the add-on looks like from the loader's side:
    ``find_spec`` answers None, so there is nothing broken to complain about.
    An operator who did not buy drafting must not read a warning every process
    about software they deliberately do not have.
    """
    _plant(tmp_path, monkeypatch, body=None)

    assert loader._drafting() is None

    assert not captured_logs, "an uninstalled basic tier must not warn about the add-on it lacks"


def test_an_absent_parent_package_stays_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """The SHIPPED basic tier's actual shape: the whole directory is missing.

    ``find_spec`` does not answer None for this one -- it RAISES
    ``ModuleNotFoundError`` naming the absent parent, because it has to import
    the parent package to ask it about the child. A discriminator that only
    handled a None spec would let the basic tier take the loud arm, which is
    the one regression that would reach every basic install rather than a
    hand-patched few.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(loader, "_DRAFTING_MODULE", "izc_no_such_package.drafting")

    assert loader._drafting() is None

    assert not captured_logs, "an absent add-on package is the basic tier, not a fault"


def test_a_working_add_on_still_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """THE POSITIVE CONTROL. Every other test here asserts None was returned.

    Without this row the whole file would pass against a loader that answered
    None unconditionally, which is the failure mode a widened catch invites.
    """
    _plant(tmp_path, monkeypatch, body="MARKER = 'loaded'\n")

    module = loader._drafting()

    assert module is not None
    assert getattr(module, "MARKER", None) == "loaded"
    assert not captured_logs, "a healthy add-on has nothing to complain about"


def test_a_parent_package_with_a_syntax_error_is_contained_and_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#189 round 4): the parent's ``SyntaxError`` ESCAPED the loader.

    The child module here is perfectly good. The parent ``__init__.py`` is not,
    and resolving a child forces the interpreter to execute its parent -- so the
    failure arrives inside ``_add_on_is_on_disk``'s ``find_spec``, which caught
    only ``(ImportError, ValueError)`` and was called on the line BEFORE
    ``_drafting``'s try. Both containments missed it: the narrow catch did not
    match the type, and the widened catch never ran because the raise happened
    above it.

    Both halves of the original #189 are asserted, one directory up. The call
    ANSWERS rather than raising, and the answer is CACHED -- ``functools.cache``
    stores nothing for a call that raised, so before the fix this re-imported
    the broken file and re-logged for every message in the round.

    RED-FIRST, observed before the fix::

        SyntaxError: invalid syntax (__init__.py, line 1)
    """
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body="def broken(:\n    pass\n",
    )

    assert loader._drafting() is None, "a broken parent must be contained, not raised"
    first_round_lines = list(captured_logs)
    assert first_round_lines, "a present-but-broken parent package must be reported once"

    assert loader._drafting() is None
    assert captured_logs == first_round_lines, (
        "the parent failure must be cached, not re-imported once per message; "
        f"captured={captured_logs}"
    )


def test_a_parent_package_raising_at_module_scope_is_reported_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#189 round 4): a ``RuntimeError`` from the parent escaped too.

    ``SyntaxError`` is not the only non-import way a module body fails. A parent
    that validates something at import time and refuses -- a config check, a
    version assertion -- raises an ordinary ``RuntimeError``, which matched
    neither ``ImportError`` nor ``ValueError`` and left the loader the same way.

    RED-FIRST, observed before the fix::

        RuntimeError: addon parent refuses to load
    """
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body="raise RuntimeError('addon parent refuses to load')\n",
    )

    assert loader._drafting() is None

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a parent that raises is a broken install, not an absent one; captured={captured_logs}"
    )


def test_a_parent_package_missing_a_dependency_stays_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#189 round 4): this one was CAUGHT, and answered "not installed".

    The worst of the three, because nothing escaped and nothing looked wrong.
    The parent imports a third-party name that is not installed,
    ``ModuleNotFoundError`` reaches the narrow catch, and the catch reported the
    add-on as absent -- so a paid, physically-present add-on with one missing
    dependency turned drafting off at DEBUG level. That is #174's silently
    disabled drafting exactly, reached through the parent instead of the child.

    The discriminator that fixes it is the PARENT's own spec, resolved without
    executing it: the directory is on disk here, so this is a fault.

    RED-FIRST, observed before the fix::

        AssertionError: a physically-present, broken paid add-on must be loud;
        it took the quiet not-installed arm instead; captured=[]
    """
    missing = "izc_absent_parent_dependency"
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body=f"import {missing}\n",
    )

    assert loader._drafting() is None

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a present parent with a missing dependency is a broken install; captured={captured_logs}"
    )


def test_a_parent_package_calling_sys_exit_does_not_abort_the_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#189 round 4): ``SystemExit`` from a module body unwound the round.

    ``SystemExit`` derives from ``BaseException``, so ``except Exception``
    catches neither this nor the ``_apply`` boundary behind it. A third-party
    file calling ``sys.exit()`` while being imported therefore aborted the whole
    polling round and abandoned every message behind the one being filed --
    which is the pre-#174 failure the lazy loader exists to prevent.

    The conversion is deliberate and narrow: a ``SystemExit`` raised BY A MODULE
    BODY is a broken add-on wearing an exit code, not an operator asking this
    process to stop, so it takes the loud arm and caches None like every other
    broken install. ``KeyboardInterrupt`` is not converted and still reaches the
    top.

    RED-FIRST, observed before the fix::

        SystemExit: 3
    """
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body="import sys\n\nsys.exit(3)\n",
    )

    assert loader._drafting() is None, "a module-body sys.exit must not abort the round"

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"an add-on that exits at import time is a broken install; captured={captured_logs}"
    )


def test_a_keyboard_interrupt_from_the_add_on_still_reaches_the_operator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """THE NEGATIVE CONTROL on the ``SystemExit`` conversion above.

    Widening a catch is only correct if it stops somewhere, and the line is
    ``KeyboardInterrupt``: an operator pressing Ctrl-C during a round must not
    have it swallowed and logged as a broken add-on. Without this row the
    conversion could grow into ``except BaseException`` and nothing here would
    notice.
    """
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body="raise KeyboardInterrupt\n",
    )

    with pytest.raises(KeyboardInterrupt):
        loader._drafting()

    assert not captured_logs, "an interrupt is the operator, not a fault to report"


def test_a_healthy_parent_with_an_absent_child_is_still_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """THE REGRESSION the parent-spec discriminator could have caused.

    ``_add_on_is_on_disk`` now answers True whenever the PARENT's files resolve,
    which is one step away from answering True for the basic tier -- where the
    parent package (this connector's own ``services``) is always present and the
    add-on directory inside it is not. It must not: this is the shipped basic
    kit, and every basic install would read an ERROR every process.

    The path that protects it is that ``find_spec`` for a missing child under a
    healthy parent returns None rather than raising, so the parent lookup is
    never consulted. This row is what proves that ordering still holds.
    """
    _plant(tmp_path, monkeypatch, body=None, parent_body="MARKER = 'parent ok'\n")

    assert loader._drafting() is None

    assert not captured_logs, "an absent child under a healthy parent is the basic tier"


@pytest.mark.parametrize(
    ("label", "vendor_body"),
    [
        pytest.param(
            "stdlib",
            "import asyncio\n\nraise asyncio.CancelledError('addon setup cancelled')\n",
            id="stdlib-CancelledError",
        ),
        pytest.param(
            "vendor",
            "class SDKFatal(BaseException):\n    pass\n\n\nraise SDKFatal('licence server unreachable')\n",
            id="vendor-BaseException",
        ),
    ],
)
def test_a_non_exception_from_a_vendor_module_is_contained_and_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
    label: str,
    vendor_body: str,
) -> None:
    """RED (#189 round 5): ``SystemExit`` was not the only non-``Exception``.

    Round 4 contained a module body's ``SystemExit`` by naming that one
    subclass. The enumeration cannot be completed, and both rows here are real
    rather than invented: ``asyncio.CancelledError`` is stdlib and is what an
    add-on doing async setup at import time raises, and a vendor SDK is free to
    root its own error hierarchy at ``BaseException`` precisely so a caller's
    bare ``except Exception`` cannot swallow it.

    THE FAILURE ARRIVES THROUGH THE ADD-ON'S OWN IMPORT, which is the install
    shape rather than a contrivance: the parent ``__init__`` does nothing but
    ``import`` the vendor module, and the vendor's module body is what raises.
    That reaches the loader through ``find_spec``, because resolving the child
    executes the parent.

    Both halves of #189 are asserted. The call ANSWERS rather than raising --
    uncontained it unwound ``_drafting``, ``_apply``, ``poll_once`` and the
    foreground loop, every one of them ``except Exception``, abandoning every
    message behind the one being filed. And the answer is CACHED, because
    ``functools.cache`` stores nothing for a call that raised, so before the fix
    the same broken file was re-imported and re-reported for every message.

    RED-FIRST, observed before the fix::

        asyncio.exceptions.CancelledError: addon setup cancelled
    """
    _plant_vendor(tmp_path, vendor_body)
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body=f"import {PLANTED_VENDOR}\n",
    )

    assert loader._drafting() is None, f"a {label} BaseException must be contained, not raised"
    first_round_lines = list(captured_logs)
    assert any("FAILED TO LOAD" in line for line in first_round_lines), (
        f"a present-but-broken paid add-on must be loud; captured={captured_logs}"
    )

    assert loader._drafting() is None
    assert captured_logs == first_round_lines, (
        f"the failure must be cached, not re-imported once per message; captured={captured_logs}"
    )


def test_a_broken_grandparent_package_is_still_reported_as_a_broken_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#189 round 5): the fail-open survived one directory up.

    ``_parent_files_resolve`` asks ``find_spec`` about the add-on's PARENT, and
    resolving the parent executes the GRANDparent. Answering a flat False when
    that raises brings back the exact conflation round 4 fixed one level down: a
    fully-present, paid, broken add-on takes the quiet not-installed arm, which
    is #189's first clause and #174's silently disabled drafting.

    The old docstring defended it on the grounds that the real grandparent is
    this connector's own shipped package and "in practice nothing here raises" --
    a statement about today's tree, not about the function, and the same
    assumption the round-4 residual already falsified. The discriminator now
    asks itself: when resolving a level raises, ask about ITS parent, until a
    level answers without executing anything.

    Here the grandparent raises at module scope, the middle package and the
    child are both healthy, and every file is on disk -- so this is a broken
    install and must be loud.

    RED-FIRST, observed before the fix::

        AssertionError: a physically-present, broken paid add-on must be loud;
        it took the quiet not-installed arm instead; captured=[]
    """
    _plant_three_levels(tmp_path, monkeypatch, top_body="raise RuntimeError('grandparent boom')\n")

    assert loader._drafting() is None

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a broken grandparent is a broken install, not an absent one; captured={captured_logs}"
    )


@pytest.mark.parametrize(
    ("label", "top_body"),
    [
        pytest.param(
            "bare-call",
            "import sys\n\nsys.exit('grandparent bailed')\n",
            id="grandparent-bare-sys-exit",
        ),
        pytest.param(
            "class-body",
            "import sys\n\n\nclass Config:\n    sys.exit('grandparent bailed')\n",
            id="grandparent-class-body-sys-exit",
        ),
        pytest.param(
            "helper-call",
            "import sys\n\n\ndef _shutdown() -> None:\n    sys.exit(3)\n\n\n_shutdown()\n",
            id="grandparent-helper-sys-exit",
        ),
    ],
)
def test_a_grandparent_calling_sys_exit_is_contained_loud_and_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
    label: str,
    top_body: str,
) -> None:
    """RED (#189 round 7): the containment stopped at the add-on's own directory.

    THE REPO ALREADY RULED THIS TIER, which is what makes it a defect rather
    than an open question.
    ``test_a_broken_grandparent_package_is_still_reported_as_a_broken_install``
    directly above plants THIS EXACT TREE with ``RuntimeError`` on the
    grandparent's module-scope line and asserts CONTAINED and loud. Change that
    one line to ``sys.exit`` and the loader re-raised instead: two rows, the
    same physically-present paid install, disagreeing on nothing but the type of
    the raise -- which is precisely the fact rounds 4 and 5 established is not a
    fact about who asked.

    TWO MECHANISMS PRODUCED IT AND THE FIX HAD TO ADDRESS BOTH. The anchor was
    ``find_spec`` on the add-on's immediate PARENT, and resolving the parent
    EXECUTES the grandparent -- so the very exit under judgement was re-raised
    inside the anchor lookup, caught by its own broad catch, and answered None.
    A None anchor made the origin test return False unconditionally, so the
    exit took the operator arm. And even with a healthy grandparent the anchor
    was one directory too deep: a frame whose file is the grandparent's own
    ``__init__.py`` sits ABOVE the add-on package's directory and could never
    match, though it is a file the import path MUST execute to reach the add-on.

    ALL THREE ROWS ARE THE SAME FILE, differing only in which frame kind runs
    the exit -- a bare module-scope call, a class body, and a helper the body
    calls. Round 6 established that frame kind is not the discriminator; these
    rows hold that ruling at the ancestor tier, where round 6 never looked.

    BOTH HALVES OF #189 ARE ASSERTED, because containment alone was never the
    whole claim. The call ANSWERS rather than raising -- uncontained it unwound
    ``_apply``, ``poll_once`` and the foreground loop, every one of them
    ``except Exception``. And the answer is CACHED: ``functools.cache`` stores
    nothing for a call that raised, so before the fix the same broken tree was
    re-entered and the complaint re-made for every message in the round.

    RED-FIRST, observed before the fix::

        SystemExit: grandparent bailed
        (propagating through round.py:425 _add_on_is_on_disk ->
         round.py:345 _reraise_if_operator_signal -> raise error)
    """
    _plant_three_levels(tmp_path, monkeypatch, top_body=top_body)

    assert loader._drafting() is None, (
        f"a {label} sys.exit from the add-on's grandparent is a broken install, "
        "not an operator shutdown, and must not abort the round"
    )
    first_call_lines = list(captured_logs)
    assert any("FAILED TO LOAD" in line for line in first_call_lines), (
        f"a physically-present, broken paid add-on must be loud; captured={captured_logs}"
    )

    assert loader._drafting() is None
    assert captured_logs == first_call_lines, (
        f"the failure must be cached, not re-entered once per message; captured={captured_logs}"
    )


def test_an_exit_from_import_machinery_on_a_healthy_tree_is_contained_and_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#203): THIS ROW ASSERTED THE OPPOSITE, AND THE SCENARIO WAS FICTION.

    It used to be ``test_an_operator_exit_with_a_healthy_add_on_tree_still_
    reaches_the_operator`` and it read a ``SystemExit`` raised from a patched
    ``import_module`` -- one carrying no add-on file on its traceback -- as the
    OPERATOR, asserting it propagated out of the loader. That described a
    person's shutdown, and no shipped path produces it: the raise happens INSIDE
    the call the loader made to import the add-on, and the operator does not
    reach inside the loader's own import to call ``sys.exit()``.

    What DOES produce that shape is import machinery the loader itself invoked
    -- a ``sys.meta_path`` finder or a ``sys.path_hooks`` entry belonging to a
    licence check, a lazy-import shim, or an editable-install finder -- and its
    frames are not the add-on's files either. Under the frame-walking rule those
    escaped the loader uncached, once per message, aborting the round with
    messages still queued, while the identical raise site with ``RuntimeError``
    was contained and loud. The type of the raise decided containment, which is
    the same defect rounds 4 through 7 each closed in a different disguise.

    THE RULE NOW HAS ONE SOURCE OF TRUTH: everything raised while the loader is
    importing the add-on is the add-on's, ``KeyboardInterrupt`` excepted. The
    patched ``import_module`` here stands for that machinery, so this row now
    asserts containment -- contained, reported once, cached.

    RED-FIRST, observed before the redo::

        Failed: DID NOT RAISE <class 'SystemExit'>
    """
    _plant_three_levels(tmp_path, monkeypatch, top_body="")

    def _machinery_exit(name: str, package: str | None = None) -> ModuleType:
        raise SystemExit(7)

    monkeypatch.setattr(loader, "import_module", _machinery_exit)

    assert loader._drafting() is None, (
        "a SystemExit raised inside the import the loader initiated is the "
        "add-on's, whatever frame it came from"
    )

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a broken paid install must be loud; captured={captured_logs}"
    )


def test_an_absent_grandparent_package_stays_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """THE REGRESSION the recursive discriminator could have caused.

    Walking up on a raise is only correct if the walk terminates in the basic
    tier's answer. Point the loader at a three-level path where NOTHING exists:
    every level raises ``ModuleNotFoundError``, the recursion runs out of dotted
    path, and the answer must be False -- quiet. A recursion that returned True
    at the top, or one that never terminated, would put an ERROR in front of
    every basic install, which is the one regression that reaches every install
    rather than a hand-patched few.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(loader, "_DRAFTING_MODULE", "izc_no_such_package.voice.drafting")
    _evict(monkeypatch, "izc_no_such_package")

    assert loader._drafting() is None

    assert not captured_logs, "an absent add-on tree is the basic tier, not a fault"


def test_a_system_exit_from_a_helper_the_add_on_calls_while_importing_is_the_add_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """THE RULING THAT REVERSED IN #189 ROUND 6, and why it had to.

    This row used to be
    ``test_a_system_exit_from_outside_a_module_body_still_reaches_the_operator``
    and asserted the OPPOSITE: that the add-on's parent ``__init__.py`` calling
    its own ``def _shutdown()`` which calls ``sys.exit(0)`` was the OPERATOR, so
    the loader re-raised it. The stated rationale was that a plain function "is
    the shape of an ``atexit`` hook, a signal handler, or a CLI helper the
    import path happens to reach" -- code reachable from somewhere OTHER than
    an import.

    THAT RATIONALE DOES NOT DESCRIBE THIS FIXTURE. The planted body calls
    ``_shutdown()`` at module scope, so the function runs if and only if the
    module is being imported. It is not reachable from anywhere else, it is the
    add-on's own file, and no person asked for anything -- which is the same
    sentence the loader already applies to a bare ``sys.exit()`` one line up.
    The old ruling was carrying a case its own reason did not cover.

    The ruling that replaced it asks whose FILE ran rather than what kind of
    frame it was: any traceback frame inside the add-on's package makes the
    ``SystemExit`` the add-on's. So this fixture is now contained and loud,
    exactly like the class body and the bare call.

    The genuine operator shutdown -- the half that must still NOT be converted
    -- is pinned by
    ``test_a_system_exit_with_no_add_on_frames_still_reaches_the_operator``
    below, which is the case the old rationale was actually describing.

    RED-FIRST, observed before the ruling changed::

        Failed: DID NOT RAISE ... (inverted: the old row asserted
        pytest.raises(SystemExit) and the new assertions failed against it)
    """
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body="import sys\n\n\ndef _shutdown() -> None:\n    sys.exit(0)\n\n\n_shutdown()\n",
    )

    assert loader._drafting() is None, (
        "a helper the add-on's own module body calls runs only during the "
        "import, so it is the add-on ending the program, not the operator"
    )

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a broken paid install must be loud; captured={captured_logs}"
    )


def test_a_system_exit_with_no_add_on_frames_is_still_the_add_ons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#203): "no add-on frames" WAS the discriminator, and it was a proxy.

    This row used to be ``..._still_reaches_the_operator`` and asserted that a
    ``SystemExit`` with none of the add-on's files on its traceback propagated
    out of the loader as an operator shutdown. The traceback was the proxy: it
    answers WHICH FILE RAN, and the question the loader has to answer is WHO
    ASKED. Those coincide only when the frames happen to be the add-on's, and
    four rounds of fixes each found another way for the add-on's failure to
    arrive with somebody else's frames on it -- a class body, an ancestor
    ``__init__.py``, a meta-path finder, a path hook.

    THE FRAMES CANNOT ANSWER IT, so the loader stops asking. Every raise that
    arrives while it is importing the add-on is the add-on's, because the loader
    is the only thing that asked for that import. The operator has no legitimate
    ``sys.exit()`` inside an import the loader initiated: their shutdown comes
    from their own tooling, a signal handler, or code ABOVE this loader, none of
    which is inside this call. ``KeyboardInterrupt`` remains the one exception,
    and it is pinned by
    ``test_a_keyboard_interrupt_from_the_add_on_still_reaches_the_operator``.

    RED-FIRST, observed before the redo::

        Failed: DID NOT RAISE <class 'SystemExit'>
    """
    _plant(tmp_path, monkeypatch, body="MARKER = 'loaded'\n")

    def _exit_during_the_import(_name: str) -> ModuleType:
        raise SystemExit(0)

    monkeypatch.setattr(loader, "import_module", _exit_during_the_import)

    assert loader._drafting() is None, (
        "the loader asked for this import, so what it raised is the add-on's"
    )

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a broken paid install must be loud; captured={captured_logs}"
    )


def test_a_class_body_calling_sys_exit_while_importing_is_contained_and_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """THE #189 ROUND-6 REFUTATION, at the loader.

    The discriminator used to read the INNERMOST frame's ``co_name`` and ask
    whether it was ``"<module>"``. A class body executing during an import
    carries the CLASS's name on its frame, not ``"<module>"``, so this exact
    fixture -- ``import sys`` / ``class Config:`` / ``sys.exit('bad config')``
    in the add-on's ``__init__.py`` -- was ruled to be the operator and
    re-raised straight out of the loader.

    Nothing above the loader catches it: ``_apply``, ``poll_once`` and the
    foreground loop all catch ``Exception``, and ``SystemExit`` is not one. So
    the round aborted with messages still queued, and because
    ``functools.cache`` stores nothing for a call that RAISED, the next message
    re-imported the same broken file and did it again.

    Moving the identical ``sys.exit`` call ONE INDENT out of the class body was
    already contained, loud and cached. That the indent decided containment is
    the whole defect.

    RED-FIRST, observed before the fix::

        src/inbox_zero_connector/services/watch/round.py:222:
            in _reraise_if_operator_signal
                raise error
        .../izc_fake_addon/__init__.py:2: in <module>
            class Config:
        E   SystemExit: bad config
    """
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body="import sys\n\n\nclass Config:\n    sys.exit('bad config')\n",
    )

    assert loader._drafting() is None, (
        "a class body executes if and only if the module is being imported, "
        "so it can never be the operator's tooling"
    )

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a broken paid install must be loud; captured={captured_logs}"
    )


def test_a_module_scope_comprehension_calling_sys_exit_is_contained_and_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """THE SIBLING FRAME KIND, pinned so the ruling cannot narrow back to one.

    A comprehension at module scope is the same category as the class body:
    it runs if and only if the file is being imported. It happens to be
    contained under BOTH discriminators, because CPython inlines comprehensions
    and the frame therefore reads ``"<module>"`` -- so this row would have
    passed before the fix too.

    It is here precisely BECAUSE it passed. The class body and the
    comprehension differ only in which frame name the interpreter happens to
    synthesise, which is an implementation detail that has changed before and
    is not a fact about who ended the program. Pinning both makes a future
    regression to any ``co_name`` test fail on at least one row, and documents
    that the list of frame kinds a module body may contain -- class bodies,
    comprehensions, generator expressions, decorators -- is not enumerable.
    """
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body="import sys\n\n\nSEATS = [sys.exit('no seats') for _ in range(1)]\n",
    )

    assert loader._drafting() is None, (
        "a module-scope comprehension executes only during the import"
    )

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a broken paid install must be loud; captured={captured_logs}"
    )


def test_a_class_body_sys_exit_is_cached_and_complained_about_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """#189'S SECOND CLAUSE for the class-body shape: once per process, not per message.

    ``functools.cache`` stores nothing for a call that RAISED, so the escape
    this fix closed was also an uncached escape: every message in the round
    re-imported the same broken file and re-raised. Answering None is what puts
    the answer in the cache.

    Five calls stand in for five messages in one round. One ERROR line, one
    import attempt, and the hot filing path never pays for the broken file
    again.
    """
    _plant(
        tmp_path,
        monkeypatch,
        body="MARKER = 'loaded'\n",
        parent_body="import sys\n\n\nclass Config:\n    sys.exit('bad config')\n",
    )

    results = [loader._drafting() for _ in range(5)]

    assert results == [None] * 5, f"every call answers None; results={results}"

    complaints = [line for line in captured_logs if "FAILED TO LOAD" in line]
    assert len(complaints) == 1, (
        f"one broken file is one complaint per process, not one per message; "
        f"complaints={complaints}"
    )


def test_a_meta_path_finder_that_exits_is_contained_loud_and_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """RED (#203): import MACHINERY the loader invoked, on a LOCATABLE install.

    THE FIFTH VARIANT OF THE SAME DEFECT, and the one that retired the
    frame-walking design. A ``sys.meta_path`` finder is third-party code the
    import system calls on the loader's behalf -- a licence check, a lazy-import
    shim, an editable-install finder -- and its frames are the FINDER's file,
    never the add-on's. The planted tree here is entirely healthy, so the old
    install-tree walk located it fine and then ruled the exit the operator's,
    because it could not find an add-on file on the traceback.

    THE ASYMMETRY IS THE PROOF IT WAS FICTION: the identical raise site with
    ``RuntimeError`` was contained, loud and cached, and only the TYPE of the
    raise decided whether the round survived. Nothing about who asked changed
    between the two.

    Five calls stand in for five messages in one round, because the escape was
    an UNCACHED escape -- ``functools.cache`` stores nothing for a call that
    raised, so every message re-paid for it.

    RED-FIRST, observed before the redo::

        SystemExit: licence check failed
    """
    _plant_three_levels(tmp_path, monkeypatch, top_body="")
    dotted = loader._DRAFTING_MODULE

    class _LicenceFinder:
        def find_spec(self, name: str, path: object = None, target: object = None) -> object | None:
            if name == dotted:
                raise SystemExit("licence check failed")
            return None

    monkeypatch.setattr(sys, "meta_path", [_LicenceFinder(), *sys.meta_path])

    results = [loader._drafting() for _ in range(5)]

    assert results == [None] * 5, (
        f"a finder's sys.exit is raised inside the import the loader asked for, "
        f"so it is the add-on's and every call answers None; results={results}"
    )

    complaints = [line for line in captured_logs if "FAILED TO LOAD" in line]
    assert len(complaints) == 1, (
        f"one broken install is one complaint per process, not one per message; "
        f"complaints={complaints}"
    )


def test_a_path_hook_that_exits_is_contained_and_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: list[str],
) -> None:
    """A SECOND, INDEPENDENT MECHANISM for #203, with no ``meta_path`` involved.

    ``sys.path_hooks`` entries are consulted to build a path-entry finder for a
    directory, which is a different limb of the import system from the meta-path
    row above. Pinning both is what stops the rule narrowing back to "the one
    finder kind we saw": the loader cannot enumerate the places the import
    system runs third-party code any more than it could enumerate the exception
    types or the frame kinds, and it no longer tries to.

    RED-FIRST, observed before the redo::

        SystemExit: path hook bail
    """
    _plant_three_levels(tmp_path, monkeypatch, top_body="")
    guarded = str(tmp_path / PLANTED_PACKAGE / "voice")

    def _hook(path: str) -> object:
        if path == guarded:
            raise SystemExit("path hook bail")
        raise ImportError(path)

    monkeypatch.setattr(sys, "path_hooks", [_hook, *sys.path_hooks])
    sys.path_importer_cache.clear()
    monkeypatch.setattr(sys, "path_importer_cache", dict(sys.path_importer_cache), raising=False)

    assert loader._drafting() is None, (
        "a path hook's sys.exit is raised inside the import the loader asked for"
    )

    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a broken paid install must be loud; captured={captured_logs}"
    )
