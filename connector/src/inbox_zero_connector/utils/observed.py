"""The ``logged`` flow decorator - one wrapper instead of five hand-written calls.

STANDARDS-python.md "LAW: flow instrumentation is ONE decorator" (Rico,
2026-08-19): write the logging utility once - entry at DEBUG, exit with
duration at DEBUG, failure at ERROR with ``opt(exception=True)`` then re-raise - and
apply it to the functions that do meaningful work, instead of hand-coding ins
and outs at every seam. Manual five-point coverage always drifts, because
every call site is a chance to forget one; a decorated path cannot die
without a trace, and DEBUG is silent in production so the flow detail is free
to leave on.

Re-homed here from ``scripts/voice_corpus/flow.py`` on 2026-08-20: the
decorator is domain-neutral and the corpus scripts import it from the
installed package, so one copy serves both trees (promotion ladder, rung 3).

The decorator owns FLOW. Call sites still own MEANING: guard and limit
triggers (WARNING), domain results (INFO), and any line needing identifiers
beyond the function's name remain explicit ``logger`` calls in the function
body.

Key Components:
    - logged: the decorator. Emits entry (DEBUG), exit with elapsed ms
      (DEBUG), and failure (ERROR, ``opt(exception=True)``), then re-raises. The
      exception is never swallowed - this wrapper observes, it does not
      handle.

Pattern/Convention:
    Decorate every function that crosses an I/O boundary (file reads and
    writes, corpus loading) or does one whole unit of meaningful work. Do not
    decorate per-sentence helpers - a DEBUG line per statement is noise that
    buries the signal (STANDARDS-observability "log at the boundary").

Example:
    >>> from inbox_zero_connector.utils import logged
    >>> @logged
    ... def parse(path: str) -> int:
    ...     return len(path)
    >>> parse("corpus")
    6

See Also:
    - inbox_zero_connector.utils.logging_config: owns the sinks this decorator's lines land in.
    - _DOCS/STANDARDS-observability.md: the five-point contract.
"""

from __future__ import annotations

import functools
import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable


def logged[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap ``fn`` with entry/exit/duration/failure logging.

    Args:
        fn: The function doing meaningful work at an I/O or unit boundary.

    Returns:
        ``fn`` wrapped so that entry and exit emit at DEBUG (exit carries the
        elapsed milliseconds) and any exception emits at ERROR with the full
        traceback before being re-raised unchanged.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        """Time one call of ``fn``, logging entry, exit, and failure around it.

        Arguments are never logged - only ``fn.__qualname__`` and the elapsed
        milliseconds - because a decorator cannot know which parameter holds a
        mail body.
        """
        name = fn.__qualname__
        logger.debug("FLOW: enter {}", name)
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            # Named on purpose as the broad base: the decorator's whole job is
            # to guarantee no decorated path dies silently, so it must see
            # every exception type - and it re-raises unchanged, so nothing is
            # swallowed. The specific handling lives at the CLI boundary.
            # ``opt(exception=True)`` is loguru's spelling; stdlib's
            # ``exc_info=True`` is silently absorbed into ``extra`` and
            # attaches nothing, which the test in tests/utils/test_observed.py
            # pins.
            elapsed_ms = (time.monotonic() - started) * 1000.0
            # The exception TYPE only. ``str(exc)`` is where content rides -- a
            # provider error routinely interpolates the upstream response, and
            # that response can hold a mail body -- and free prose is not a
            # shape the redacting patcher can sweep, so the message never
            # enters the string. The attached traceback still names the type
            # and the frames, which is what actually diagnoses the failure.
            logger.opt(exception=True).error(
                "FLOW: fail {} after {:.1f}ms: {}",
                name,
                elapsed_ms,
                type(exc).__name__,
            )
            raise
        elapsed_ms = (time.monotonic() - started) * 1000.0
        logger.debug("FLOW: exit {} in {:.1f}ms", name, elapsed_ms)
        return result

    return wrapper
