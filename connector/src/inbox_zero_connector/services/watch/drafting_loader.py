"""The lazy voice add-on loader: is drafting available, and whose fault is it not.

WHAT THIS MODULE IS
    One question, asked once per process: can this deployment draft? The answer
    is a module or None, and None is a PRODUCT STATE rather than an error --
    the basic kit is a folder-triage kit that ships without the voice add-on,
    so "no drafting module" is its normal condition. ``round`` files exactly as
    it always did when the answer is None.

WHY IT IS ITS OWN MODULE AND NOT PART OF ``round``
    It answers a packaging question, not a mail question. Nothing here reads a
    message, a cursor, a taxonomy, or a provider; every function here talks to
    the import system and to ``logger``. ``round`` was also at the 500-code-line
    ceiling with this block inside it, so the containment work #189 needed had
    nowhere to go -- but the seam was already there, and moving the block is
    what makes it visible rather than what creates it. ``round`` re-exports
    exactly two names -- ``_DRAFTING_MODULE`` and ``_drafting`` -- because those
    are the two its own callers and the shipped tests reach through it. Every
    other function here is private to this module and is driven directly, which
    is what ``test_drafting_loader_real_modules.py`` does: it re-aims this
    module's globals, since ``round``'s aliases are bound at import and re-aiming
    one would leave the loader still reading its own.

THE THREE FACTS IT SEPARATES, WHICH ONE EXCEPTION CONFLATES
    NOT BOUGHT
        The add-on's files are not on disk. The basic tier's every process.
        Quiet DEBUG, drafting off, filing continues. Anything louder puts a
        warning in front of every basic install about software the operator
        deliberately did not buy.

    BOUGHT AND BROKEN
        The files ARE on disk and importing them failed -- a missing
        dependency, a circular import, a syntax error in a hand-patched
        install, a module body that raised. ERROR with the traceback, once per
        process, drafting off, filing continues. This is #174: paid drafting
        must never be disabled silently.

    THE OPERATOR ASKED THIS PROCESS TO STOP
        ``KeyboardInterrupt``, and nothing else. Re-raised untouched. Ctrl-C is
        delivered to whatever frame happens to be executing, so it lands inside
        the add-on's import as readily as anywhere else and the file it
        interrupted says nothing about who asked.

WHY THE CATCHES ARE ``BaseException``
    Because they are a BOUNDARY, not an enumeration. Resolving or importing the
    add-on executes third-party module bodies, and a module body is free to
    raise anything: ``asyncio.CancelledError`` is stdlib and is what an add-on
    doing async setup at import time raises, and a vendor SDK may root its own
    hierarchy at ``BaseException`` outright so a caller's ``except Exception``
    cannot swallow it. An uncontained raise here unwinds ``_apply``,
    ``poll_once`` and the foreground loop -- all ``except Exception`` -- and
    abandons every message queued behind the one being filed, which is the
    pre-#174 failure this loader exists to prevent. Worse, ``functools.cache``
    stores nothing for a call that RAISED, so the same broken file was
    re-imported and re-reported once per message.

    So every catch here is broad, ``_reraise_if_operator_signal`` is called
    first in every one of them, and every arm logs. ``KeyboardInterrupt`` is the
    only line, and it is drawn on the one fact that does not need discovering:
    the loader asked for this import, so what the import raised is the add-on's.

WHY THERE IS NO OPERATOR-VERSUS-ADD-ON DISCRIMINATOR (#203)
    There was one, for four rounds, and each round shipped a new variant of the
    same defect: it keyed on a PROXY for the answer instead of the answer.
    ``SystemExit`` was treated as genuinely ambiguous -- the operator's tooling
    raises it and so does a broken add-on -- and the loader tried to tell them
    apart by looking at the exception name, then the innermost frame's
    ``co_name``, then one package directory, then the ancestor ``__init__.py``
    files, then whether the install tree could be located at all. Every one of
    those reads WHICH FILE RAN. The question is WHO ASKED, and the two coincide
    only by luck: a class body, an ancestor's module body, a ``sys.meta_path``
    finder, and a ``sys.path_hooks`` entry each carried a broken add-on's
    failure in with somebody else's frames on it, and each escaped uncached,
    once per message, aborting the round with mail still queued.

    THE AMBIGUITY WAS NEVER REAL, which is why no amount of frame walking could
    resolve it. This loader is the only thing that asked for this import. The
    operator has no legitimate ``sys.exit()`` inside an import the loader
    initiated -- their shutdown comes from their own tooling, a signal handler,
    or code ABOVE this loader, and none of those is inside this call. So the
    rule is a fact about the call, not a judgement about a traceback:
    everything raised while the loader is importing the add-on is the add-on's,
    ``KeyboardInterrupt`` excepted. Contained, reported once per process,
    cached, and the round continues.

    THE FINDER CASE FALLS OUT FOR FREE, and so does whichever limb of the
    import system runs third-party code next. The places the interpreter can
    execute somebody else's code on the loader's behalf are no more enumerable
    than the exception types or the frame kinds were, and this rule does not
    enumerate them.

THE PACKAGING CONSTRAINT THIS MODULE OBEYS
    This file ships in the BASIC zip, where the add-on's own directory is cut.
    ``scripts/packaging/assert_zip.py`` scans shipped bytes and refuses the
    add-on's dotted path and directory path as raw substrings -- so a comment
    or a docstring trips it exactly as an ``import`` statement does.
    ``_DRAFTING_MODULE`` is therefore ASSEMBLED from ``__package__`` rather than
    written out, and no prose in this file spells the add-on's location.
"""

from __future__ import annotations

from functools import cache
from importlib import import_module
from importlib.util import find_spec
from types import ModuleType
from typing import Final

from loguru import logger

_DRAFTING_MODULE: Final[str] = f"{__package__.rsplit('.', 1)[0]}.voice.drafting"
"""Dotted path of the add-on module ``_drafting()`` loads.

ASSEMBLED FROM ``__package__`` RATHER THAN WRITTEN OUT, and that is a packaging
requirement rather than a style choice. This file ships in the basic zip, and
``scripts/packaging/assert_zip.py`` refuses the add-on's dotted path as a raw
substring anywhere inside it -- a scan over bytes, so a comment or a deferred
``import`` statement trips it exactly as a module-scope one does. Deriving the
name keeps the literal out of the shipped bytes while naming the same module.
"""


def _reraise_if_operator_signal(error: BaseException) -> None:
    """Let ``KeyboardInterrupt`` through the add-on loader's catches, and nothing else.

    THE BOUNDARY THE BROAD CATCHES STOP AT, in one place because all of them
    stop at the same line and a second copy would drift.

    ONE SIGNAL, NOT TWO (#203). ``KeyboardInterrupt`` is Ctrl-C, always: it is
    delivered to whatever frame happens to be executing, so it lands inside the
    add-on's import as readily as anywhere else and the file it interrupted says
    nothing about who asked. It is therefore the only raise this loader hands
    back, and it is checked on the type alone.

    ``SystemExit`` IS NOT THE OPERATOR HERE, and four rounds of trying to decide
    when it was are why. It was treated as ambiguous -- the operator's tooling
    calls ``sys.exit()`` and so does a broken add-on being imported, with the
    identical type and an equally arbitrary code -- and every discriminator
    written for it read WHICH FILE RAN as a stand-in for WHO ASKED: the
    exception name, the innermost frame's ``co_name``, one package directory,
    the ancestor ``__init__.py`` files, then whether the install tree could be
    located at all. Each shipped, and each was refuted by another way for the
    add-on's own failure to arrive wearing somebody else's frames -- a class
    body, an ancestor module body, a ``sys.meta_path`` finder, a
    ``sys.path_hooks`` entry.

    The ambiguity was an artefact of asking the traceback. This loader is what
    asked for the import, so a ``SystemExit`` raised inside it is the add-on's
    by construction; the operator's real shutdown comes from their own tooling,
    a signal handler, or code above this loader, none of which is inside this
    call. Deciding it by the CALL rather than by the frames is what makes the
    finder case, the path-hook case, and whatever the import system runs next
    correct without naming any of them.

    Args:
        error: The exception a loader catch just bound.

    Raises:
        KeyboardInterrupt: ``error`` itself, when it is one. Returns None
            otherwise, leaving the caller to report and cache it as the
            add-on's.
    """
    if isinstance(error, KeyboardInterrupt):
        raise error


def _add_on_is_on_disk() -> bool:
    """Whether the add-on's FILES are present, asked without executing them.

    THE DISCRIMINATOR BETWEEN "not bought" AND "bought and broken" (#189), and
    the reason ``exc.name`` could not be it. ``exc.name`` answers which module
    the interpreter was working on, which is the add-on's OWN dotted path for
    every failure raised from INSIDE the add-on -- a circular import is the
    plain case, and it is indistinguishable by name from the add-on simply not
    being there. Both arms then read "not installed" and the basic tier's quiet
    DEBUG line swallowed a paid install that was physically present and broken.

    ``find_spec`` asks the import system where the module WOULD come from
    without running a line of it, so it separates the two facts the exception
    conflates: files present is an install, files absent is a tier.

    IT ANSWERS THE QUESTION TWO DIFFERENT WAYS AND BOTH MEAN ABSENT. A missing
    submodule inside a package that exists returns ``None``. A missing PARENT
    package -- which is the shipped basic kit, where the whole directory is cut
    by ``scripts/packaging/tiers.json`` -- RAISES ``ModuleNotFoundError``,
    because resolving the child requires importing the parent first. Treating
    only the ``None`` case as absent would make every basic install take the
    loud arm, so the raise is caught here and reported as the same answer.

    AND A RAISE IS NOT ITSELF EVIDENCE OF ABSENCE (#189 round 4). Resolving a
    child forces the interpreter to EXECUTE the parent package, so every way a
    parent ``__init__.py`` can fail -- a syntax error, a ``RuntimeError`` at
    module scope, a dependency it imports that is not installed -- arrives here
    as a raise from a lookup that promised not to run any of the add-on's code.
    A narrow ``(ImportError, ValueError)`` catch let ``SyntaxError`` and every
    non-import exception escape the loader entirely, which is #189's own second
    half relocated one directory up: uncached, re-raised for every message in
    the round. ``ModuleNotFoundError`` from a parent that IS on disk and imports
    a missing dependency was worse -- caught, and answered "not installed",
    which is #189's FIRST half and the silently-disabled paid drafting of #174.

    SO THE PARENT'S OWN SPEC IS THE DISCRIMINATOR, asked by
    ``_parent_files_resolve`` below: it resolves the parent package's spec
    WITHOUT executing it, so "the directory is not there" and "the directory is
    there and its ``__init__`` blew up" stop wearing the same answer. Absent
    parent means the basic tier, quiet and False. Present parent means a broken
    install, and this function answers True so ``_drafting`` takes its loud arm
    and caches the failure.

    THE CATCH IS ``BaseException``, MINUS ``KeyboardInterrupt`` (#189 round 5,
    narrowed to one signal in #203). ``Exception`` was an enumeration wearing
    the shape of a boundary:
    it contained a module body that raised ``RuntimeError`` and let one that
    raised ``asyncio.CancelledError`` -- stdlib, and what an add-on doing async
    setup while being imported raises -- straight past, and any vendor SDK that
    roots its own error hierarchy at ``BaseException``. Neither is the operator,
    both are a broken add-on wearing a non-``Exception`` type, and the type a
    third-party file chooses to raise is not a fact about who asked for it.
    ``_reraise_if_operator_signal`` draws the only line that is a fact.

    NEVER RAISES EXCEPT FOR THAT ONE, AND NEVER WARNS. Every answer here is
    logged at DEBUG, including the broken-install one, because ``_drafting`` is
    what owes the operator the ERROR line and a second complaint from the lookup
    would double it.

    Returns:
        True when the add-on's files are present -- either the module's own
        spec resolved, or the lookup blew up executing a parent that is on
        disk. Both make a later import failure a fault rather than an absence.
    """
    try:
        return find_spec(_DRAFTING_MODULE) is not None
    # A BROAD CATCH IS THE CONTRACT, not defensive coding: this function's
    # docstring promises it always answers, and resolving a child executes
    # third-party parent code that can fail in any way at all -- including in
    # types that are not ``Exception``. Every arm below logs,
    # ``KeyboardInterrupt`` is re-raised first, and the answer -- not the
    # exception -- is what the caller reads.
    except BaseException as error:  # ruff: ignore[blind-except]
        _reraise_if_operator_signal(error)
        present = _parent_files_resolve()
        # DOCUMENTED FAIL-CLOSED, at DEBUG in both arms. Absent is the basic
        # tier's normal path every process and not a fault. Present-and-broken
        # is a fault, but it is ``_drafting``'s to report at ERROR with the
        # traceback; saying it twice at two levels would make the operator
        # reconcile two lines about one broken file.
        logger.debug(
            f"WATCH: voice add-on lookup failed, parent_files_present={present} "
            f"error_type={type(error).__name__}"
        )
        return present


def _files_resolve(module: str) -> bool:
    """Whether ``module``'s files are on disk, asked without executing ``module``.

    THE HALF OF ``_add_on_is_on_disk`` THAT STILL ANSWERS when resolving the
    level below has already raised. ``find_spec`` on a PACKAGE resolves that
    package's own spec from the path finders and does not execute its
    ``__init__.py`` -- executing it is what resolving a CHILD requires. So
    asking about the parent alone separates the two facts the child's raise
    conflates: no spec means the directory was cut by the basic tier's
    packaging, a spec means the operator has the add-on and it is broken.

    RECURSIVE, BECAUSE THE FAIL-OPEN WAS RECURSIVE (#189 round 5). Resolving a
    parent still EXECUTES ITS OWN parent, so a grandparent whose module body
    raises brought the same conflation back one directory up: the old code
    answered a flat False there, which routes a fully-present, paid, broken
    add-on to the quiet not-installed arm -- #189's first clause, at the next
    level. The previous docstring defended that on the grounds that the real
    grandparent is this connector's own shipped package and "in practice nothing
    here raises", which is a statement about today's tree rather than about the
    function, and it is the same assumption the round-4 residual already
    falsified one level down. So the discriminator asks itself: when resolving
    ``module`` raises, ask about ``module``'s own parent, until a level answers
    without executing anything or the dotted path runs out.

    A path that runs out means no package above it resolved at all, which is
    the basic tier's shape and answers False -- a lookup that cannot establish
    presence must not claim it, because claiming it turns a basic install's
    normal state into an ERROR line every process.

    Args:
        module: The dotted path to ask about. Never executed by this call; its
            ancestors are, which is why the recursion exists.

    Returns:
        True when ``module``'s spec resolves, or when a raise while resolving it
        is explained by an ancestor whose own files ARE present.
    """
    if not module:
        return False
    try:
        return find_spec(module) is not None
    # Same broad catch and same reason as the caller's: this is the fallback
    # arm of a lookup that must always answer, it is reached only when something
    # already went wrong, and third-party module bodies do not restrict
    # themselves to ``Exception``. ``KeyboardInterrupt`` is re-raised before
    # anything else is read.
    except BaseException as error:  # ruff: ignore[blind-except]
        _reraise_if_operator_signal(error)
        parent, _, _ = module.rpartition(".")
        present = _files_resolve(parent)
        logger.debug(
            f"WATCH: voice add-on package {module} did not resolve, "
            f"ancestor_files_present={present} error_type={type(error).__name__}"
        )
        return present


def _parent_files_resolve() -> bool:
    """Whether the add-on package's DIRECTORY is on disk, asked without running it.

    The named entry point into ``_files_resolve`` for the one module this
    loader cares about, kept as its own function because the caller's question
    is "is the add-on installed", not "does an arbitrary dotted path resolve".

    Returns:
        True when the add-on package's spec resolves without executing it, or
        when the raise while resolving it came from an ancestor that is itself
        present on disk.
    """
    parent, _, _ = _DRAFTING_MODULE.rpartition(".")
    return _files_resolve(parent)


@cache
def _drafting() -> ModuleType | None:
    """The drafting module, or None when the voice add-on is absent or broken.

    THE TIER SWITCH, resolved once per process. ``functools.cache`` because the
    answer cannot change while the process runs -- an add-on does not appear
    mid-round -- and a per-message import lookup on the hot filing path would
    be paid for nothing.

    Returns None rather than raising, and that is the product decision rather
    than defensive coding: the basic kit is a folder-triage kit, so "no
    drafting module" is its NORMAL state and not a fault to report. Callers
    treat None exactly as they treat an uninjected composer.

    TWO FAILURES WEAR ONE EXCEPTION TYPE, AND #174 IS WHY THEY ARE SPLIT HERE.
    An import failure says only that SOME module did not load, so the catch that
    read every one of them as "the add-on is not installed" also swallowed a
    DEPENDENCY of an add-on that IS installed -- and cached the swallow as None
    for the process lifetime, silently turning off drafting the operator paid
    for. The split sends an absent add-on to a quiet DEBUG line, which is the
    basic tier's NORMAL state, and a broken one to ERROR.

    ``_add_on_is_on_disk`` IS WHAT SPLITS THEM, NOT ``exc.name`` (#189). The
    name on the exception is the add-on's own dotted path for every failure
    raised from inside it, so a circular import in an installed add-on matched
    the absent-add-on test exactly and took the quiet arm -- the silently
    disabled paid drafting #174 was about, reached by a different route. The
    spec is the fact that cannot be confused: files present means an install,
    and an install that fails to import is a fault.

    THE CATCH IS ``BaseException``, NOT ``ImportError``, for the other half of
    #189. A syntax error in a hand-patched install raises ``SyntaxError``,
    which is not an ``ImportError`` at all: it escaped this function, was
    contained by ``_apply``'s per-message boundary instead, and -- because
    ``functools.cache`` stores nothing for a call that RAISED -- re-imported
    the same broken file and re-logged the same complaint for every message in
    the round. Answering None for any failure is what puts the answer in the
    cache, so the complaint is made once per process and the hot path never
    pays for the broken file again.

    Logged at DEBUG for the absent add-on and only on the first call, because
    the cache makes every later call silent. An operator who did not buy the
    add-on should not read a warning every round about software they
    deliberately do not have. The broken add-on is logged at ERROR for the
    inverse reason: it is paid software that is not working, and one line per
    process is the least that can be said about it.

    THE ``installed`` LOOKUP IS INSIDE THE TRY (#189 round 4), and the position
    is the fix rather than tidiness. ``_add_on_is_on_disk`` resolves the child,
    which EXECUTES the add-on's parent package, so it is a place third-party
    code runs and therefore a place third-party code can raise. Called on the
    line above the try, a parent ``__init__.py`` with a syntax error propagated
    straight out of this function, past the widened catch that was supposed to
    contain it -- uncached, once per message, which is exactly the defect the
    widened catch was added to close. Inside the try, any raise from either call
    lands on the same arm and is cached as None like every other failure.

    THE BOUNDARY IS THE CALL, NOT THE TRACEBACK (#203). This arm no longer asks
    where a ``SystemExit`` came from, because every version of that question
    read which FILE ran as a stand-in for who ASKED, and each one shipped a
    fresh way for a broken add-on's exit to arrive with somebody else's frames
    on it -- a class body, an ancestor module body, a ``sys.meta_path`` finder,
    a ``sys.path_hooks`` entry -- and escape uncontained and uncached. The
    loader is what asked for this import, so everything it raises is the
    add-on's; ``_reraise_if_operator_signal`` keeps ``KeyboardInterrupt`` alone.

    Returns None in BOTH arms. Reporting loudly and refusing to draft are not
    in tension -- the caller's contract is that filing never depends on the
    add-on, so a broken add-on degrades to the basic tier's behaviour plus a
    complaint, never to a stalled round.
    """
    installed = False
    try:
        installed = _add_on_is_on_disk()
        return import_module(_DRAFTING_MODULE)
    # A BROAD CATCH IS THE RULE HERE, for the reason the docstring gives: a
    # loader whose callers treat None as "this deployment does not draft" must
    # answer for every way a third-party file can fail to execute, not for the
    # subset that wears an import-flavoured type. The suppression is scoped to
    # this call and every arm below logs.
    #
    # ``installed`` IS SEEDED FALSE ABOVE so this arm reads a bound name even
    # on the path where ``_add_on_is_on_disk`` itself raised. That path is
    # unreachable today -- that function's own broad catch makes it total -- and
    # the seed is what keeps it unreachable rather than a fresh ``NameError``
    # escaping the loader if it ever stops being total. False is the safe seed:
    # it takes the quiet arm, and a basic tier reading nothing is a smaller
    # wrong than every basic install reading an ERROR.
    except BaseException as error:  # ruff: ignore[blind-except]
        _reraise_if_operator_signal(error)
        if not installed:
            logger.debug("WATCH: voice add-on not installed, drafting is off and filing continues")
            return None
        logger.opt(exception=True).error(_failed_add_on_line(error))
        return None


def _failed_add_on_line(error: BaseException) -> str:
    """The ERROR text for an add-on that is on disk and did not load.

    A MESSAGE BUILDER RATHER THAN A LOGGING HELPER, and the shape is deliberate:
    ``scripts/check_except_logging.py`` reads handler bodies syntactically and
    cannot see through a call, so a helper that logs on a handler's behalf is
    exactly the bound-but-unlogged shape that check exists to refuse. Both of
    ``_drafting``'s arms therefore keep their own ``logger`` call and share only
    the words.

    ERROR, with the traceback at the call site, because this is bought software
    that did not load. The failing name is the operator's whole diagnosis -- an
    add-on dependency that is not installed, or the add-on module itself for a
    circular import -- and it is a module name, never mail content, so it is
    safe to log in full. It falls back to the module asked for because a
    ``SyntaxError`` carries no ``name`` at all.

    Args:
        error: What the lookup or the import raised.

    Returns:
        One line naming the failing module and the exception type.
    """
    missing = getattr(error, "name", None) or ""
    return (
        f"WATCH: voice add-on installed but FAILED TO LOAD, drafting is off for this "
        f"process and filing continues missing={missing or _DRAFTING_MODULE} "
        f"error_type={type(error).__name__}"
    )
