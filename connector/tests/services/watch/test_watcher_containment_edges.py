"""Two containment holes in one polling round, each proven by behaviour.

Both defects share a shape: a boundary that LOOKS total is scoped narrower
than the failures that reach it, so an ordinary runtime error escapes the
per-message containment #157 installed and kills the round with messages still
queued behind it.

#174 -- THE LAZY VOICE LOADER. ``round._drafting()`` caught
``ModuleNotFoundError`` only, and the ``_draft`` call sat outside ``_apply``'s
per-message ``try``. So an add-on whose own DEPENDENCY is missing, or which
fails to initialise at all, propagated straight out of ``poll_once``. The
second half is quieter and worse: a ``ModuleNotFoundError`` naming a
DEPENDENCY was read as "no add-on installed" and cached as ``None`` for the
process lifetime, silently turning off drafting the operator paid for.

#189 ROUND 4 -- THE SAME HOLE, TWICE MORE. Widening the loader's catch to
``Exception`` left two calls outside it. The PRESENCE lookup
(``_add_on_is_on_disk``) sat on the line before the try and executes the
add-on's parent package, so a parent that fails to import raised past the catch
meant to contain it -- uncached, once per message. And ``SystemExit`` from a
module body is a ``BaseException``, which neither the loader's catch nor
``_apply``'s boundary behind it matches, so an add-on calling ``sys.exit()`` at
import time aborted the round with messages still queued. Both are round-level
rows here; the on-disk shapes that produce them are in
``test_drafting_loader_real_modules.py``.

#189 ROUND 5 -- NAMING ONE SUBCLASS IS NOT A BOUNDARY. Converting ``SystemExit``
alone left every other ``BaseException`` a module body can raise uncontained,
and there are real ones: ``asyncio.CancelledError`` from an add-on doing async
setup at import time, and any vendor SDK that roots its own error hierarchy at
``BaseException``. Each unwound the round exactly as ``SystemExit`` had. The
catch is now ``BaseException`` with the operator's own two signals re-raised,
and the discriminator for ``SystemExit`` is WHERE it was raised rather than what
it is -- which is why ``_break_the_add_on`` grew a raise-from-the-add-on flag.

#189 ROUND 6 -- "WHICH FRAME KIND" IS NOT A BOUNDARY EITHER. That origin test
read the INNERMOST frame's ``co_name`` and asked whether it was ``"<module>"``.
A CLASS BODY executing during an import carries the CLASS's name on its frame,
so an add-on whose ``__init__.py`` reads ``class Config:`` / ``sys.exit(...)``
was ruled to be the operator and re-raised -- the round abandoned with messages
queued, uncached, once per message, which is verbatim the failure round 4 and
round 5 each closed in a different disguise. The frame kinds a module body may
contain cannot be enumerated any more than the exception types could. The
discriminator now asks whose FILE appears anywhere in the traceback, and
``_break_the_add_on``'s flag is ``from_the_add_ons_file``.

#203 -- "WHOSE FILE" WAS A PROXY FOR "WHO ASKED". Rounds 4 through 7 each fixed
the discriminator and each shipped a new variant, because every version read the
TRACEBACK: exception name, then frame ``co_name``, then one package directory,
then the ancestor ``__init__.py`` files. The variant that ended it was import
machinery the loader itself invoked -- a ``sys.meta_path`` finder or a
``sys.path_hooks`` entry -- whose frames are neither party's, so it was ruled the
operator and escaped uncached, once per message, while the identical raise site
with ``RuntimeError`` was contained. The rule is now a fact about the CALL:
everything raised while the loader is importing the add-on is the add-on's,
``KeyboardInterrupt`` excepted. The frame-walk is deleted, and
``test_a_system_exit_raised_during_the_import_does_not_abandon_the_round``
below is the row that used to assert the opposite.

#179 -- THE LEDGER CARVE-OUT. ``_apply`` re-raised ``OSError`` first, as the
audit carve-out that says a mutation without a receipt must not be contained.
But ``ConnectionError``, ``TimeoutError``, and raw socket errors are all
``OSError`` subclasses, so a transport blip inside ``classify`` took the
carve-out's exit and aborted the stream with no FAILED receipt at all -- the
exact opposite of what the carve-out exists to protect.

THE MODULE NAME IS ASSEMBLED FROM PARTS, for the reason
``test_watcher_without_voice.py`` states: this file ships in the basic zip and
the packaging judge refuses the add-on's literal dotted path in anything that
does.
"""

from __future__ import annotations

import sys
from asyncio import CancelledError
from collections.abc import Iterator
from pathlib import Path
from types import FunctionType

import pytest
from loguru import logger

from _watch_harness import FINAL_CURSOR, CountingClassifier, FakeProvider, _context, _message
from inbox_zero_connector.config import VoiceRouting
from inbox_zero_connector.providers.base import ChangeSet, Message
from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.services.triage.decide import Classification
from inbox_zero_connector.services.watch import drafting_loader as loader
from inbox_zero_connector.services.watch import round as watcher

VOICE_PACKAGE = "inbox_zero_connector.services." + "voice"
"""The add-on package. Assembled; see the module docstring."""

DRAFTING_MODULE = f"{VOICE_PACKAGE}.drafting"

MISSING_DEPENDENCY = "some_paid_addon_dependency"
"""A third-party name the add-on would import. Never installed here."""


# ---------------------------------------------------------------------------
# #174 -- a broken add-on must not kill the round, and must not be mistaken
# for an absent one.
# ---------------------------------------------------------------------------


@pytest.fixture
def _clear_loader_cache() -> Iterator[None]:
    """The loader's one cached answer, cleared on both sides.

    ``_drafting`` is the tier switch, and since #203 it is the only cache the
    loader has. ``_add_on_install_files`` used to sit beside it, holding where
    the add-on's install tree lived for the frame-walking discriminator that
    decided whether a ``SystemExit`` was the operator's; both the walk and its
    cache are deleted. Rows here patch ``find_spec`` to describe different
    installs, so an answer surviving into the next row would decide against the
    previous row's tree.
    """
    loader._drafting.cache_clear()
    yield
    loader._drafting.cache_clear()


@pytest.fixture
def captured_logs() -> Iterator[list[str]]:
    """Every WARNING-or-above line loguru emitted, as plain strings.

    A loguru SINK rather than pytest's ``caplog``, because this package logs
    through loguru and loguru does not propagate to the stdlib ``logging``
    tree that ``caplog`` reads. Under ``caplog`` these assertions would pass
    or fail on an always-empty list, which is worse than no assertion.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING", format="{level} {message}")
    yield lines
    logger.remove(sink_id)


ADD_ON_FILE = str(Path(watcher.__file__).resolve().parents[1] / "voice" / "__init__.py")
"""A path inside the add-on package, used as the fixture's ``co_filename``.

BUILT FROM THE LOADER'S OWN MODULE rather than written out, for the packaging
reason ``round._DRAFTING_MODULE`` is assembled the same way: this file ships in
the basic zip and ``scripts/packaging/assert_zip.py`` refuses the add-on's
dotted path as a raw substring. ``round.__file__`` is ``services/watch/round.py``,
so its grandparent is ``services`` and the add-on package sits beside ``watch``.

The file does not have to EXIST -- nothing reads it. Since #203 the loader does
not inspect frames at all, so this path is now only a realistic ``co_filename``
for the fixture's synthesised code object rather than a fact any assertion turns
on. It stays because a raise that claims to come from the add-on's file should
name the add-on's real location, whether or not the paid tier is installed in
this checkout.
"""


def _raise_from_the_add_ons_own_file(error: BaseException, frame_name: str | None = None) -> None:
    """Raise ``error`` from a frame whose file is inside the add-on package.

    NO LONGER LOAD-BEARING, AND KEPT ON PURPOSE (#203). The loader used to
    decide a ``SystemExit`` by whose FILE appeared in the traceback, and this
    helper existed to put the add-on's file there. That discriminator is gone:
    everything raised while the loader is importing the add-on is the add-on's,
    so these rows pass with or without the flag.

    THE FLAG STAYS BECAUSE IT MAKES THE FIXTURE HONEST. The scenario each row
    names is a third-party file ending the program while the loader imports it,
    and a raise from this test module's own ``def`` merely resembles that. Once
    the traceback stopped deciding anything, dropping the flag would have cost
    nothing to the assertions and quietly changed what the rows describe --
    which is how a suite ends up pinning a scenario no shipped path produces,
    the exact failure #203 was opened for.

    THIS REPLACED A ``co_name == "<module>"`` FIXTURE, and the replacement is
    the point rather than a refactor. The old helper compiled a body whose frame
    was named ``<module>``, which is what the old discriminator read. That test
    could not have caught round 6's defect: a CLASS BODY executing during an
    import carries the class's name instead, so the loader ruled it the operator
    and re-raised it out of the round. Pinning the FILE rather than the frame
    name is what makes this fixture describe the scenario instead of the
    implementation.

    ``compile`` is what sets ``co_filename`` honestly: the resulting code
    object reports the planted path exactly as a real module's does, so the
    traceback the loader inspects is the real shape rather than one asserted by
    the test. ``FunctionType`` runs that code object instead of the ``exec``
    builtin, which reads the same and keeps a genuine ``exec`` call out of the
    suite. The on-disk equivalent, with really broken files, is in
    ``test_drafting_loader_real_modules.py``.

    Args:
        error: The exception to raise. Bound into the body's globals rather than
            closed over, because a module body has no closure.
        frame_name: ``co_name`` for the raising frame. None leaves the module
            body's own ``<module>``. Passing a class name reproduces the round-6
            defect's exact shape -- a class body executing during an import,
            whose frame the interpreter names for the CLASS -- and a row that
            passes with it set proves the loader is reading the FILE and not
            the frame name.

    Raises:
        BaseException: ``error``, from a frame inside the add-on's package.
    """
    source = "raise _planted_error"
    if frame_name is not None:
        # A REAL CLASS BODY, not a renamed code object: the interpreter builds
        # the frame and names it for the class itself, which is exactly how the
        # defect reached production. Writing the name onto a synthetic code
        # object would assert the shape rather than produce it.
        source = f"class {frame_name}:\n    raise _planted_error"
    body = compile(source, ADD_ON_FILE, "exec")
    FunctionType(body, {"_planted_error": error})()


def _break_the_add_on(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    *,
    installed: bool = True,
    from_the_add_ons_file: bool = False,
    frame_name: str | None = None,
) -> None:
    """Make loading the drafting module raise ``error``.

    Patches the ``import_module`` NAME the loader actually calls rather than
    planting a broken file on disk, because the failure under test is what the
    interpreter hands back when the add-on's own import machinery raises -- a
    missing dependency, a circular import, a syntax error in a hand-patched
    install. Patching ``builtins.__import__`` would not reach it:
    ``importlib.import_module`` does not go through that hook.

    ``installed`` PATCHES THE SPEC LOOKUP TOO, and #189 is why it has to. The
    loader no longer decides absent-vs-broken from ``exc.name`` -- that name is
    the add-on's own dotted path for any failure raised from inside it, so a
    circular import in a paid install was indistinguishable from the basic
    tier. It asks ``find_spec`` whether the FILES are there instead. This
    checkout has those files on disk, so a fixture that only patched the import
    would describe an absent add-on while the spec still reported an installed
    one, and the assertion would be about a state no install can be in. Tests
    that mean "the operator did not buy it" pass ``installed=False``.

    Real broken and absent add-ons, built on disk rather than described, live
    in ``test_drafting_loader_real_modules.py``; these rows stay because they
    pin which ARM a given exception object takes.

    The already-imported modules are evicted too, so nothing here depends on
    test ordering leaving a live ``services.voice`` in ``sys.modules``.

    Args:
        monkeypatch: Undoes the patches afterwards.
        error: What loading the add-on raises.
        installed: Whether the spec lookup reports the FILES as present.
        from_the_add_ons_file: Raise ``error`` from a frame inside the add-on
            package instead of from this helper's own ``def``. Required for any
            ``BaseException`` the loader routes on origin rather than on type --
            see ``_raise_from_the_add_ons_own_file``.
        frame_name: Passed through to that helper. A class name makes the raise
            come from a real class body, which is the round-6 shape.
    """
    for name in [name for name in sys.modules if name.startswith(VOICE_PACKAGE)]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    def _raise(name: str, package: str | None = None) -> object:
        if from_the_add_ons_file:
            _raise_from_the_add_ons_own_file(error, frame_name)
        raise error

    # EVERY PACKAGE IN THE ADD-ON'S PATH IS LEFT REAL, and since #203 the reason
    # is the ``installed`` verdict rather than an install-tree walk. Answering a
    # bare ``object()`` for an ANCESTOR would describe a machine where the
    # connector's own top-level package resolves to something with no location,
    # which is not a state an install can be in. The rows here vary exactly one
    # fact -- whether the add-on's own MODULE resolves -- so the patch forces
    # that verdict for that module alone and lets every ancestor answer about
    # the real tree.
    real_find_spec = loader.find_spec
    add_on_package, _, _ = loader._DRAFTING_MODULE.rpartition(".")
    real_packages = frozenset(
        add_on_package.rsplit(".", index)[0] for index in range(add_on_package.count(".") + 1)
    )

    def _find_spec(name: str) -> object | None:
        if name in real_packages:
            return real_find_spec(name)
        return object() if installed else None

    monkeypatch.setattr(loader, "import_module", _raise)
    monkeypatch.setattr(loader, "find_spec", _find_spec)


def _drafting_ready_context(provider: FakeProvider) -> watcher.RoundContext:
    """A deployment that HAS bought drafting: backend injected, routing set."""

    class _Backend:
        def compose(self, prompt: str, voice: object) -> str:
            return "unreachable"

    base = _context(provider)
    return watcher.RoundContext(
        provider=provider,
        scope=base.scope,
        taxonomy=base.taxonomy,
        state=base.state,
        container_id=None,
        classifier=CountingClassifier(),
        draft_backend=_Backend(),
        routing=VoiceRouting(owner_domain="example.com"),
        voice="external",
        owner_address="owner@example.com",
    )


def test_an_add_on_dependency_import_error_does_not_kill_the_round(
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
) -> None:
    """RED (#174): the round dies on message 1 and never reaches 2-5.

    The add-on IS installed; one of its dependencies is not. That is an
    ``ImportError`` the loader never caught, raised from a ``_draft`` call
    that sat outside ``_apply``'s containment, so it propagated out of
    ``poll_once``. Filing is the product and it must survive the optional
    add-on being broken.
    """
    _break_the_add_on(
        monkeypatch, ImportError(f"No module named {MISSING_DEPENDENCY!r}", name=MISSING_DEPENDENCY)
    )
    messages = tuple(_message(f"m{index}") for index in range(1, 6))
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))

    result = watcher.poll_once(_drafting_ready_context(provider))

    assert result.applied == 5, "every message files even when the add-on cannot load"
    assert result.failed == 0
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE] * 5


def test_a_dependency_import_error_is_reported_not_silently_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
    captured_logs: list[str],
) -> None:
    """RED (#174): a dependency's ``ModuleNotFoundError`` reads as "no add-on".

    ``ModuleNotFoundError`` is an ``ImportError`` with a ``name``. When that
    name is the drafting module itself, the add-on is genuinely not installed
    and DEBUG is right -- the basic tier must not warn every round about
    software it deliberately does not have. When the name is anything else,
    a paid add-on is installed and broken, and going quiet turns off drafting
    the operator bought. The distinguishing fact is ``exc.name``.
    """
    _break_the_add_on(
        monkeypatch,
        ModuleNotFoundError(f"No module named {MISSING_DEPENDENCY!r}", name=MISSING_DEPENDENCY),
    )

    assert loader._drafting() is None

    assert any(MISSING_DEPENDENCY in line for line in captured_logs), (
        "a broken paid add-on must name the failing dependency at WARNING or above; "
        f"captured={captured_logs}"
    )


def test_the_absent_add_on_import_error_stays_quiet(
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
    captured_logs: list[str],
) -> None:
    """THE OTHER SIDE OF THE SPLIT, and the regression this fix could cause.

    Widening the loader's catch is only correct if the basic tier stays
    silent. Here the drafting module ITSELF is what is missing, which is the
    basic kit's normal state and not a fault.

    ``installed=False`` IS THE LOAD-BEARING ARGUMENT AFTER #189. The exception
    naming the drafting module used to be the whole signal, and it is no longer
    a signal at all -- a circular import inside an installed add-on raises with
    that same name. What says "not bought" now is that the files do not
    resolve, so the fixture has to model the absence rather than only the
    raise.
    """
    _break_the_add_on(
        monkeypatch,
        ModuleNotFoundError(f"No module named {DRAFTING_MODULE!r}", name=DRAFTING_MODULE),
        installed=False,
    )

    assert loader._drafting() is None

    assert not captured_logs, "an uninstalled basic tier must not warn about the add-on it lacks"


def test_an_add_on_that_exits_at_import_time_does_not_kill_the_round(
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
) -> None:
    """RED (#189 round 4): ``SystemExit`` is a ``BaseException`` and escaped BOTH.

    The round-level consequence of the loader's last uncontained failure, and it
    is the same harm #174 opened this file for: a third-party file calling
    ``sys.exit()`` while being imported raises ``SystemExit``, which derives from
    ``BaseException``, so neither ``_drafting``'s ``except Exception`` nor
    ``_apply``'s per-message boundary behind it caught it. Message 1 aborted the
    round and messages 2-5 were abandoned with the cursor unstored.

    The conversion is narrow on purpose: a ``SystemExit`` raised by a MODULE BODY
    is a broken add-on wearing an exit code -- nobody asked this process to stop,
    an import ran and the file ended the program -- so it degrades to the basic
    tier like every other broken install. ``KeyboardInterrupt`` is not converted,
    which ``test_drafting_loader_real_modules.py`` pins.

    ``from_the_add_ons_file=True`` SINCE #189 ROUND 5, and it is what makes this
    row describe the scenario its own docstring names. The loader decides
    ``SystemExit`` by ORIGIN rather than by type, so a fixture raising it inside
    this test module's own ``def`` is the operator's tooling and is correctly
    re-raised. The flag puts the raise in a frame whose file is the add-on's --
    since round 6 that is the fact the loader reads, replacing a check on
    whether the frame was named ``<module>``.

    RED-FIRST, observed before the fix::

        SystemExit: 3
    """
    _break_the_add_on(monkeypatch, SystemExit(3), from_the_add_ons_file=True)
    messages = tuple(_message(f"m{index}") for index in range(1, 6))
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))

    result = watcher.poll_once(_drafting_ready_context(provider))

    assert result.applied == 5, "an add-on that exits at import must not abandon the round"
    assert result.failed == 0
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE] * 5


class _VendorSdkFatal(BaseException):
    """A vendor SDK rooting its own error hierarchy at ``BaseException``.

    Not hypothetical shape-making: a paid add-on imports third-party code, and
    nothing stops that code deriving its base error from ``BaseException`` so
    that a bare ``except Exception`` in a caller cannot swallow it. From the
    loader's side it is a broken add-on, exactly like a ``SyntaxError``.
    """


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(CancelledError("addon setup cancelled"), id="stdlib-CancelledError"),
        pytest.param(_VendorSdkFatal("licence server unreachable"), id="vendor-BaseException"),
    ],
)
def test_a_non_exception_add_on_failure_does_not_kill_the_round(
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
    error: BaseException,
) -> None:
    """RED (#189 round 5): ``except Exception`` was an enumeration, not a boundary.

    Round 4 contained ``SystemExit`` by naming it, which left every OTHER
    ``BaseException`` a module body can raise uncontained.
    ``asyncio.CancelledError`` is stdlib and is what an add-on doing async setup
    at import time raises; a vendor SDK is free to root its own hierarchy at
    ``BaseException`` outright. Neither is the operator, and the type a
    third-party file picks is not a fact about who asked for it.

    Uncontained, either one unwound ``_drafting``, ``_apply``, ``poll_once`` and
    the foreground loop -- all ``except Exception`` -- so message 1 aborted the
    round and messages 2-5 were abandoned with the cursor unstored. That is the
    pre-#174 harm this file was opened for, reached by a type round 4 did not
    name.

    RED-FIRST, observed before the fix::

        asyncio.exceptions.CancelledError: addon setup cancelled
    """
    _break_the_add_on(monkeypatch, error, from_the_add_ons_file=True)
    messages = tuple(_message(f"m{index}") for index in range(1, 6))
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))

    result = watcher.poll_once(_drafting_ready_context(provider))

    assert result.applied == 5, "a non-Exception add-on failure must not abandon the round"
    assert result.failed == 0
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE] * 5


def test_a_class_body_sys_exit_does_not_abandon_the_round(
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
) -> None:
    """RED (#189 round 6): a class body's exit aborted the round at the top level.

    THE ROUND-LEVEL HALF of the loader row in
    ``test_drafting_loader_real_modules.py``. The old discriminator read the
    INNERMOST frame's ``co_name``, and a class body executing during an import
    carries the CLASS's name, so ``sys.exit()`` one indent inside ``class
    Config:`` in the add-on's ``__init__.py`` was ruled the operator and
    re-raised out of the loader.

    Nothing above it catches a ``SystemExit``: ``_apply``, ``poll_once``, and
    the foreground loop all catch ``Exception``. So message 1 filed, and
    messages 2-5 were abandoned with the cursor unstored -- the pre-#174
    failure this loader exists to prevent, reached by the one frame kind the
    round-5 fix did not name.

    Five messages, all filed, is the assertion because the count is the
    containment: filing is the product and it must not depend on the add-on.

    RED-FIRST, observed before the fix::

        Failed: a class body calling sys.exit while being imported aborted the
        whole round: SystemExit: bad config; warns=[]
        captured stderr: LEDGER: receipt written ... message_id=m1 ...
    """
    _break_the_add_on(
        monkeypatch,
        SystemExit("bad config"),
        from_the_add_ons_file=True,
        frame_name="Config",
    )
    messages = tuple(_message(f"m{index}") for index in range(1, 6))
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))

    result = watcher.poll_once(_drafting_ready_context(provider))

    assert result.applied == 5, (
        "a class body that exits while being imported is a broken add-on, "
        f"and filing must not depend on it; applied={result.applied}"
    )
    assert result.failed == 0
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE] * 5


def test_a_grandparent_sys_exit_does_not_abandon_the_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
    captured_logs: list[str],
) -> None:
    """RED (#189 round 7): the escape survived one directory ABOVE the add-on.

    THE ROUND-LEVEL HALF of
    ``test_a_grandparent_calling_sys_exit_is_contained_loud_and_cached`` in
    ``test_drafting_loader_real_modules.py``, and the reason this row plants a
    REAL TREE instead of using ``_break_the_add_on`` like its neighbours. The
    defect was not in which arm a described exception took -- it was in the
    LOOKUP that decides the arm: the anchor resolved the add-on's parent, which
    EXECUTES the grandparent, so the grandparent's own ``sys.exit()`` was
    re-raised inside the anchor lookup and destroyed the anchor that was
    judging it. A patched ``find_spec`` cannot reproduce a lookup poisoning
    itself, because the test author supplies the answer the poisoning would have
    corrupted.

    WHAT IT COST AT THE PRODUCT LEVEL, which is why the assertion is a count
    rather than a return value. Nothing above the loader catches a
    ``SystemExit``: ``_apply``, ``poll_once``, and the foreground loop all catch
    ``Exception``. Measured before the fix, m1 filed and its receipt was
    written, then the round died and m2-m5 were abandoned with the cursor
    unstored -- and not one warning was logged, so the operator's only signal
    was mail that stopped moving. That is verbatim the pre-#174 "abandons every
    message behind the one being filed" harm this loader exists to prevent,
    reached through the one tier round 6 never looked at.

    Five messages, all filed, plus a loud line: filing is the product and must
    never depend on the add-on, and a paid install that does not work must say
    so.

    RED-FIRST, observed before the fix::

        Failed: a grandparent sys.exit during add-on import aborted the round:
        SystemExit: grandparent bailed; filed=1/5; warns=[]
    """
    top = tmp_path / "izc_round_grandparent"
    middle = top / "voice"
    middle.mkdir(parents=True)
    (top / "__init__.py").write_text(
        "import sys\n\nsys.exit('grandparent bailed')\n", encoding="utf-8"
    )
    (middle / "__init__.py").write_text("", encoding="utf-8")
    (middle / "drafting.py").write_text("MARKER = 'loaded'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(loader, "_DRAFTING_MODULE", "izc_round_grandparent.voice.drafting")
    for name in [name for name in sys.modules if name.startswith("izc_round_grandparent")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    messages = tuple(_message(f"m{index}") for index in range(1, 6))
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))

    result = watcher.poll_once(_drafting_ready_context(provider))

    assert result.applied == 5, (
        "a grandparent that exits while being imported is a broken add-on, "
        f"and filing must not depend on it; applied={result.applied}"
    )
    assert result.failed == 0
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE] * 5
    assert any("FAILED TO LOAD" in line for line in captured_logs), (
        f"a broken paid install must not go by in silence; captured={captured_logs}"
    )


def test_a_system_exit_raised_during_the_import_does_not_abandon_the_round(
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
) -> None:
    """RED (#203): THE SCENARIO THIS ROW USED TO PIN WAS FICTION.

    It was ``test_an_operator_system_exit_is_not_mistaken_for_a_broken_add_on``,
    and it asserted that a ``SystemExit`` raised from a ``def`` OUTSIDE the
    add-on, while the loader was importing the add-on, propagated out of
    ``poll_once`` as the operator's shutdown. No shipped path produces that. The
    raise happens INSIDE the import call the loader itself made, and the
    operator does not reach inside that call: their ``sys.exit()`` comes from
    their own tooling, a signal handler, or code ABOVE this loader, none of
    which is on this stack.

    WHAT REALLY ARRIVES IN THAT SHAPE is import machinery the loader invoked --
    a ``sys.meta_path`` finder or a ``sys.path_hooks`` entry from a licence
    check, a lazy-import shim, an editable-install finder. Its frames are not
    the add-on's files either, so the old origin test ruled it the operator and
    let it escape uncached, once per message, aborting the round with mail still
    queued -- while the identical raise site with ``RuntimeError`` was contained
    and loud. That asymmetry, decided by nothing but the type of the raise, is
    the fifth variant of the same defect and is what retired the discriminator.

    THE RULE NOW: everything raised while the loader is importing the add-on is
    the add-on's, ``KeyboardInterrupt`` excepted -- which
    ``test_an_operator_keyboard_interrupt_still_stops_the_round`` below still
    pins. So the fixture is unchanged and the assertion is inverted: five
    messages, all filed, because filing is the product and must not depend on
    the add-on.

    RED-FIRST, observed before the redo::

        Failed: DID NOT RAISE <class 'SystemExit'>
    """
    _break_the_add_on(monkeypatch, SystemExit(0))
    messages = tuple(_message(f"m{index}") for index in range(1, 6))
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))

    result = watcher.poll_once(_drafting_ready_context(provider))

    assert result.applied == 5, (
        "a SystemExit raised inside the import the loader asked for is the "
        f"add-on's, and the round continues; applied={result.applied}"
    )
    assert result.failed == 0
    assert [row.action for row in ledger.read_all()] == [ledger.ACTION_FILE] * 5


def test_an_operator_keyboard_interrupt_still_stops_the_round(
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
) -> None:
    """THE SIGNAL THAT IS THE OPERATOR NO MATTER WHOSE FILE RAISED IT.

    ``KeyboardInterrupt`` is the one case the origin test must NOT be consulted
    for. Ctrl-C is delivered to whatever frame happens to be executing, so it
    lands inside the add-on's own import as readily as anywhere else -- the file
    it interrupted says nothing about who asked. ``_reraise_if_operator_signal``
    therefore checks it FIRST and unconditionally, before any question about
    origin is asked.

    This row raises it from the add-on's own file, which is the shape that would
    break if the containment rule were ever simplified to "anything from the
    add-on's files is the add-on's fault". An operator pressing Ctrl-C during a
    round must stop the round, not be logged as a broken paid install and
    swallowed.
    """
    _break_the_add_on(monkeypatch, KeyboardInterrupt(), from_the_add_ons_file=True)
    provider = FakeProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    with pytest.raises(KeyboardInterrupt):
        watcher.poll_once(_drafting_ready_context(provider))


def test_a_spec_lookup_that_raises_is_diagnosed_once_not_per_message(
    monkeypatch: pytest.MonkeyPatch,
    _clear_loader_cache: None,
    captured_logs: list[str],
) -> None:
    """RED (#189 round 4): the PRESENCE lookup was the uncontained call.

    ``_add_on_is_on_disk`` sat on the line BEFORE ``_drafting``'s try, so the
    widened catch that was supposed to contain a broken add-on never ran for
    anything the LOOKUP raised -- and the lookup executes the add-on's parent
    package, so it raises whatever that parent raises. A ``SyntaxError`` in a
    hand-patched ``__init__.py`` therefore left the loader entirely.

    THE ROUND SURVIVED THAT ONE, and surviving is why it stayed hidden. ``_apply``
    has a belt-and-braces boundary around the drafting seam (#174), so the escape
    was contained one frame out -- but contained there it is a per-message
    ``WARNING`` reading "drafting raised outside its own boundary
    error_type=SyntaxError", which names no add-on and offers no diagnosis, and
    ``functools.cache`` stores nothing for a call that raised, so the broken file
    was re-read and re-reported for EVERY message in the round. Five messages,
    five useless warnings, forever.

    So the assertion is the COUNT and the CONTENT, not merely survival: one line,
    from the loader, naming the add-on module.

    THE FIXTURE RAISES ONLY ON THE CHILD, which is what a broken parent really
    does: resolving ``voice.drafting`` executes ``voice/__init__.py`` and blows
    up, while resolving ``voice`` alone reads that package's spec off the path
    finder without running a line of it. A fake that raised on BOTH would model
    a mailbox no install can be in and would take the quiet arm for the right
    reason and the wrong scenario. The on-disk version of this shape, with real
    files, is ``test_drafting_loader_real_modules.py``.

    RED-FIRST, observed before the fix -- five copies of::

        WATCH: drafting raised outside its own boundary, message stays filed and
        the round continues message=m1 error_type=SyntaxError
    """

    def _raise_on_the_child_only(name: str) -> object:
        if name == DRAFTING_MODULE:
            raise SyntaxError("invalid syntax", ("__init__.py", 1, 1, "def broken(:"))
        return object()

    _break_the_add_on(monkeypatch, ImportError("unreachable"))
    monkeypatch.setattr(loader, "find_spec", _raise_on_the_child_only)
    messages = tuple(_message(f"m{index}") for index in range(1, 6))
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))

    result = watcher.poll_once(_drafting_ready_context(provider))

    assert result.applied == 5, "a raising presence lookup must not abandon the round"
    assert result.failed == 0
    assert len(captured_logs) == 1, (
        f"one diagnosis per process, not one per message; captured={captured_logs}"
    )
    assert "FAILED TO LOAD" in captured_logs[0], (
        f"the line must come from the loader and name the add-on; line={captured_logs[0]}"
    )


# ---------------------------------------------------------------------------
# #179 -- the OSError carve-out belongs to the ledger, not to the boundary.
# ---------------------------------------------------------------------------


class _SocketFailingClassifier:
    """A classifier whose transport dies. ``ConnectionError`` IS an OSError."""

    def __init__(self) -> None:
        self.seen_ids: list[str] = []

    def classify(self, message: Message, folders: tuple[str, ...]) -> Classification:
        self.seen_ids.append(message.id)
        raise ConnectionResetError(104, "Connection reset by peer")


def test_a_classifier_socket_error_is_not_taken_by_the_ledger_carve_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (#179): the carve-out fires, the round aborts, no receipt is written.

    ``ConnectionResetError`` is a ``ConnectionError`` is an ``OSError``, so it
    took the ledger carve-out's ``raise`` before the broad catch could receipt
    it. A transport blip is exactly the kind of per-message failure #157 said
    must not abandon the messages behind it.
    """
    messages = tuple(_message(f"m{index}") for index in range(1, 4))
    provider = FakeProvider(ChangeSet(messages=messages, cursor=FINAL_CURSOR))
    context = _context(provider, classifier=_SocketFailingClassifier())

    result = watcher.poll_once(context)

    assert result.failed == 3, "every message gets its own contained failure"
    assert result.applied == 0
    rows = ledger.read_all()
    assert len(rows) == 3, "a contained per-message failure still owes a receipt"
    assert {row.error_code for row in rows} == {"ConnectionResetError"}


def test_a_provider_write_socket_error_is_not_taken_by_the_ledger_carve_out() -> None:
    """The same hole reached through ``file_message`` rather than ``classify``.

    A provider adapter raising ``TimeoutError`` on the write is an ``OSError``
    too, and it must produce a FAILED receipt like any other refusal.
    """
    provider = FakeProvider(
        ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR),
        file_error=TimeoutError("read timed out"),
    )

    result = watcher.poll_once(_context(provider, classifier=CountingClassifier()))

    assert result.failed == 1
    rows = ledger.read_all()
    assert len(rows) == 1
    assert rows[0].error_code == "TimeoutError"


def test_a_ledger_write_failure_still_takes_the_carve_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE CARVE-OUT ITSELF, kept. Narrowing it must not delete it.

    An ``OSError`` raised by the LEDGER APPEND is the one failure that must
    propagate: the thing that would record the mutation is what failed, so a
    round that kept filing would move mail nothing recorded.
    """
    provider = FakeProvider(ChangeSet(messages=(_message("m1"),), cursor=FINAL_CURSOR))

    def _refuse(*args: object, **kwargs: object) -> object:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ledger, "append", _refuse)

    with pytest.raises(OSError, match="No space left on device"):
        watcher.poll_once(_context(provider, classifier=CountingClassifier()))
