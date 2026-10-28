"""Bounded retry for outbound provider calls. tenacity owns the mechanism.

The observability standard requires that outbound calls have bounded attempts,
that each failure is logged with its attempt count, and that exhaustion
re-raises rather than returning a sentinel. This module supplies that once, so
no adapter writes a retry loop.

WHY tenacity AND NOT A LOOP
    ``_DOCS/STANDARDS-python.md`` ## LAW: do not hand-roll a solved problem
    names retry-with-backoff as the canonical example, and names the decisions
    a hand-rolled version makes invisibly: whether attempt N waits N or N-1,
    and whether jitter is additive (which breaks your own max-delay bound) or
    subtractive. Those are exactly the bugs that surface during the incident
    the retry existed to survive. ``wait_exponential_jitter`` is somebody
    else's maintained problem.

    What stays local is the part that IS domain knowledge: WHICH failures
    deserve another attempt. A 429 or a 503 is the network having a bad
    second; a 403 is USUALLY the token lacking a scope, and retrying that just
    asks Google to refuse four more times while the host AI waits. The
    exception is a 403 whose body names a rate-limit reason -- Google
    overloads that status to mean "slow down" too -- and telling the two apart
    is ``RATE_LIMIT_REASONS`` below.

WHAT IS DELIBERATELY NOT RETRIED
    Mutations are not retried on an ambiguous failure. A ``file_message`` that
    timed out may well have applied the label, and a blind second attempt is
    how one action becomes two ledger rows -- or worse, two rows for one real
    change, which breaks the "action count == ledger row count" invariant the
    kit is audited on. Retry covers READS, where a repeat is free, and covers
    the transport-level refusals where the request provably never landed.

    THE ONE EXCEPTION, AND IT IS THE SECOND HALF OF THAT LAST SENTENCE.
    ``providers/outlook_reauth.py`` retries a POST or a PATCH after Graph
    answers 401, and that is this rule honoured rather than broken: what this
    rule refuses is repeating a write whose outcome is UNKNOWN, and a 401 is
    the provider refusing the request BEFORE acting on it. Nothing was
    created, so there is no duplicate to make and no second ledger row. The
    exception is named here, in the absolute, because the reader who would
    delete it is the one reading this paragraph -- not the one reading the
    module that implements it. 401 is deliberately absent from
    ``RETRYABLE_STATUS`` below: refresh-then-retry-once is a different shape
    from repeat-and-hope, and folding it in here would retry an expired token
    against itself four times.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from loguru import logger
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

if TYPE_CHECKING:
    from inbox_zero_connector.utils.logging_config import ProviderCall

MAX_ATTEMPTS: Final = 3
"""Total attempts, not retries. Bounded, per the observability standard."""

INITIAL_WAIT_SECONDS: Final = 1.0
MAX_WAIT_SECONDS: Final = 8.0

RETRY_AFTER_CAP_SECONDS: Final = 120.0
"""Upper bound on a honoured ``Retry-After``: the server's hint is trusted, not obeyed.

A throttling server may legitimately name minutes -- Gmail's daily-limit and
Graph's tenant-level throttles both do -- and a watcher asleep for longer than
two minutes is indistinguishable from a dead one to anything looking at it from
the outside. Past the cap the hint is still HONOURED, at the cap: the wait is
clamped rather than discarded, because a server that said "not yet" is still
better evidence than the backoff curve's guess.
"""

RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})
"""HTTP statuses worth another attempt REGARDLESS of the error body.

Rate limiting and server-side transients only. 401/403 are excluded on
purpose: an auth or scope refusal is a configuration fact, and retrying it
turns a clear error into a slow one. That exclusion is also a security
property here -- a connector that hammered a send-refusal would look exactly
like one trying to get around it.

403 is excluded from this table and handled by ``is_retryable`` as a
body-conditional case, so the security property above survives: a 403 earns a
retry only by carrying a ``RATE_LIMIT_REASONS`` reason, and a send-refusal
carries none.
"""


RATE_LIMIT_REASONS: Final[frozenset[str]] = frozenset({
    "ratelimitexceeded",
    "userratelimitexceeded",
    "dailylimitexceeded",
    "quotaexceeded",
})
"""Machine-readable reasons that mean "slow down", not "you may not".

Google returns these under ``error.errors[].reason`` and answers them with a
403, not a 429 -- documented at
https://developers.google.com/workspace/gmail/api/guides/handle-errors, which
states plainly that a project over quota gets ``403 dailyLimitExceeded`` and a
user over rate gets ``403 rateLimitExceeded`` / ``userRateLimitExceeded``.

WHY THIS IS A REASON TABLE AND NOT A PROVIDER CHECK
    A 403 means different things to the two providers this kit speaks to, and
    the difference is real rather than cosmetic: for Graph a 403 IS a
    permission denial, and for Gmail it is overloaded to carry throttling as
    well. The tempting shortcut -- ``if provider == "gmail"`` in shared code --
    would be wrong even where it works, because it decides from WHO the caller
    is instead of WHAT the response says. The moment Graph adds a throttling
    403, or the Gmail adapter is reused behind a proxy, a name comparison is
    silently answering the wrong question.

    Matching on the body keeps the decision where the evidence is. Graph's
    error bodies carry no ``error.errors[].reason``, so a Graph 403 finds
    nothing here and stays fatal WITHOUT this module ever knowing which
    adapter called it.

IN PRACTICE THIS CARVE-OUT IS GMAIL-ONLY, AND THE GRAPH PATH NEVER TESTS IT
    The paragraph above describes what WOULD happen if a Graph 403 reached
    ``is_retryable``. It does not reach it. On the Graph path a 403 never
    becomes an exception at all, so the body question is never asked: the only
    thing that converts an httpx response into a raisable error is
    ``providers/outlook/errors.py:167`` ``_raise_if_retryable``, and its
    single condition is ``response.status_code in RETRYABLE_STATUS`` --
    the frozenset at ``providers/retry.py:82``, from which 403 is excluded by
    design. A Graph 403 therefore stays a plain ``httpx.Response``, falls
    through to ``_unwrap``/``_provider_error``, and is attempted exactly once.
    Both Graph read paths go through that same helper:
    ``providers/outlook/provider.py:710`` (``OutlookProvider._get``) and
    ``providers/outlook/delta.py:248`` (``_delta_page``); neither has any other
    route to a 403 exception, and the writes are not retried at all.

    So the ``status == 403`` branch of ``is_retryable`` is live code that only
    Gmail can ever exercise -- which is the documented intent, not a defect.
    It is kept provider-agnostic anyway for the reason stated above: the
    decision reads WHAT the response says, so it stays correct if Graph ever
    grows a throttling 403 or the Gmail adapter is reused behind a proxy.
    Pinned by ``tests/providers/outlook/test_retry_403_graph_path.py``, which
    fails if 403 is ever added to ``RETRYABLE_STATUS`` without that scope
    being re-argued.
"""


def _rate_limit_reasons(exc: BaseException) -> frozenset[str]:
    """Extract lowercased ``error.errors[].reason`` values from a Google error body.

    ``googleapiclient.errors.HttpError`` keeps the raw response body on
    ``.content`` as bytes. The SDK's own ``.reason`` attribute is deliberately
    NOT used: it holds the human-readable ``error.message`` ("Daily Limit
    Exceeded"), which is prose subject to wording and localisation changes,
    while ``error.errors[].reason`` is the stable machine-readable enum.
    Matching prose is how a policy quietly stops matching.

    Anything unparseable yields an empty set, so a body that is not
    Google-shaped -- Graph's, a proxy's HTML error page, an empty payload --
    simply finds no rate-limit reason and leaves the status verdict alone.

    Args:
        exc: The exception to inspect.

    Returns:
        The lowercased reason strings found, or an empty frozenset.
    """
    content = getattr(exc, "content", None)
    if not isinstance(content, bytes | str):
        return frozenset()
    try:
        payload: Any = json.loads(content)
    except ValueError, UnicodeDecodeError:
        # DOCUMENTED FAIL-OPEN, and it is the one in this module whose
        # consequence is a POLICY DECISION rather than a lost diagnostic.
        #
        # An empty set here means `_is_rate_limited` is False, which means a
        # 403 is judged FATAL and the call is not retried. That is the correct
        # default and it is deliberately fail-CLOSED with respect to retrying:
        # a body we cannot read is not evidence of throttling, and inventing a
        # retry from an unparseable body is how a send-refusal gets hammered.
        #
        # What the log buys is telling the two indistinguishable cases apart.
        # A Graph 403, a proxy's HTML error page, and an empty payload all
        # legitimately reach here and find nothing -- but so does a Gmail
        # throttling 403 whose body arrived truncated, and that one SHOULD have
        # been retried. Without a line, a connector that has silently stopped
        # retrying real rate limits looks exactly like one that never hit any.
        #
        # DEBUG because the overwhelmingly common case is the benign one (every
        # Graph 403 lands here by design, and this module is deliberately
        # provider-agnostic so it cannot tell which caller it is serving). The
        # failing call itself is already logged at ERROR by `ProviderCall`, and
        # each retried attempt at WARNING by `retrying`; this line explains the
        # ABSENCE of those retry lines, which is the signal that is otherwise
        # missing entirely. NO BODY CONTENT is logged -- an error body may echo
        # the request, and this kit's requests carry the owner's mail.
        logger.opt(exception=True).debug(
            "RETRY: error body not parseable as a Google error envelope, "
            "no rate-limit reason found -- a 403 will be treated as fatal"
        )
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()
    error = payload.get("error")
    if not isinstance(error, dict):
        return frozenset()
    errors = error.get("errors")
    if not isinstance(errors, list):
        return frozenset()
    return frozenset(
        item["reason"].lower()
        for item in errors
        if isinstance(item, dict) and isinstance(item.get("reason"), str)
    )


def _is_rate_limited(exc: BaseException) -> bool:
    """True when the error body names a throttling reason rather than a refusal."""
    return bool(_rate_limit_reasons(exc) & RATE_LIMIT_REASONS)


def _status_of(exc: BaseException) -> int | None:
    """Extract an HTTP status from either provider's error type, or None.

    Both SDKs are handled here rather than in each adapter so the retry policy
    is one table. ``googleapiclient.errors.HttpError`` exposes ``resp.status``;
    ``httpx.HTTPStatusError`` exposes ``response.status_code``.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return status if isinstance(status, int) else None


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Extract a ``Retry-After`` delta-seconds hint from either SDK's error, or None.

    Mirrors ``_status_of``'s two-branch shape for the same reason: the hint is
    one policy, and each adapter would otherwise reimplement it against its own
    SDK. ``httpx``'s ``Headers`` mapping is case-insensitive, so the canonical
    ``Retry-After`` spelling matches whatever the server sent; httplib2
    lowercases every header key before handing ``HttpError`` its ``resp``, so
    the Google branch asks for ``retry-after`` instead.

    ONLY the delta-seconds form is read. ``Retry-After`` may also carry an
    HTTP-date, and parsing one needs a wall clock plus an assumption that the
    local clock agrees with the server's -- a skewed clock would turn a hint
    into an arbitrary sleep. An unreadable hint is simply no hint: the caller
    falls back to the exponential-jitter curve, which is already bounded and
    known-good, so the failure mode is the pre-existing behaviour rather than a
    guess.

    Args:
        exc: The exception to inspect.

    Returns:
        The non-negative delay in seconds the server asked for, or None when
        there is no header, no parseable number, or a nonsensical negative one.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        resp = getattr(exc, "resp", None)
        get = getattr(resp, "get", None)
        raw = get("retry-after") if callable(get) else None
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError, TypeError, AttributeError:
        # DOCUMENTED FAIL-OPEN: an unreadable hint is no hint. Returning None
        # sends the caller to the bounded jitter curve, so the consequence is
        # the pre-existing wait, never a guessed sleep. DEBUG because the
        # HTTP-date form of Retry-After lands here by design (see the
        # docstring), and the retried attempt itself is already logged at
        # WARNING. The raw value is server-controlled and is NOT logged.
        logger.debug(
            "RETRY: Retry-After header not readable as delta-seconds, "
            "falling back to the jitter curve"
        )
        return None
    return None if seconds < 0 else seconds


_JITTER: Final = wait_exponential_jitter(initial=INITIAL_WAIT_SECONDS, max=MAX_WAIT_SECONDS)


def _wait_server_hint_or_jitter(state: RetryCallState) -> float:
    """Wait what the server asked for, capped; otherwise the existing jitter curve.

    WHY THE HINT WINS WHEN IT IS PRESENT
        The backoff curve is guessing at a number the server already stated.
        Expiring earlier than the window a 429 named does not recover sooner --
        it burns one of three attempts on a service that has just said it is
        not ready, and for a watcher that polls continuously that is how the
        whole attempt budget is spent inside a single throttling window.

    WHY ABSENCE CHANGES NOTHING
        With no readable hint this delegates to the same
        ``wait_exponential_jitter`` the policy has always used, with the same
        bounds. The hint is an addition to the curve, not a replacement for it,
        so every failure that carries no header retries exactly as before.

    Args:
        state: tenacity's call state for the attempt that just failed.

    Returns:
        Seconds to sleep before the next attempt.
    """
    outcome = getattr(state, "outcome", None)
    exc = outcome.exception() if outcome is not None else None
    hint = _retry_after_seconds(exc) if exc is not None else None
    if hint is not None:
        return min(hint, RETRY_AFTER_CAP_SECONDS)
    return _JITTER(state)


def is_retryable(exc: BaseException) -> bool:
    """True when ``exc`` is a transient transport failure worth another attempt.

    Two independent grounds, in order of cost. A status in
    ``RETRYABLE_STATUS`` settles it outright. Otherwise a 403 gets ONE more
    question -- does its own body name a throttling reason? -- because Google
    answers "slow down" with the same status it uses for "you may not", and a
    watcher that polls continuously is precisely the workload that finds out.

    The 403 carve-out is narrow on purpose. A 403 with no rate-limit reason in
    its body is still fatal, which is what keeps a Graph permission denial and
    a genuine Gmail scope refusal from being hammered three times over.
    """
    status = _status_of(exc)
    if status is None:
        return False
    if status in RETRYABLE_STATUS:
        return True
    return status == 403 and _is_rate_limited(exc)


def retrying(call: ProviderCall) -> Retrying:
    """Return a tenacity ``Retrying`` bound to ``call``'s log context.

    The retry point of the five is emitted here, once per attempt after the
    first, so no adapter has to remember it.

    Args:
        call: The ``ProviderCall`` whose correlation id and identifying fields
            the retry lines should carry.

    Returns:
        A ``Retrying`` to iterate. Exhaustion re-raises the last exception --
        tenacity's ``reraise=True`` -- rather than raising a wrapper that hides
        which provider error actually occurred.

    Example:
        >>> for attempt in retrying(call):        # doctest: +SKIP
        ...     with attempt:
        ...         return api.execute()
    """

    def _before_sleep(state: object) -> None:
        """Emit the retry log line for the attempt that just failed.

        Typed ``object`` and read with ``getattr`` because tenacity's
        ``RetryCallState`` is a runtime detail this module does not import into
        its signatures; a missing attribute degrades to a logged "unknown"
        rather than raising inside a retry callback, where an exception would
        replace the provider's real error with this module's.
        """
        number = getattr(state, "attempt_number", 0)
        outcome = getattr(state, "outcome", None)
        exc = outcome.exception() if outcome is not None else None
        call.retry(
            attempt=number,
            error_type=type(exc).__name__ if exc is not None else "unknown",
            status=_status_of(exc) if exc is not None else None,
        )

    return Retrying(
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=_wait_server_hint_or_jitter,
        retry=retry_if_exception(is_retryable),
        before_sleep=_before_sleep,
        reraise=True,
    )


def with_retry[T](call: ProviderCall, operation: Callable[[], T]) -> T:
    """Run ``operation`` under the bounded retry policy, logging each attempt.

    Args:
        call: The ``ProviderCall`` supplying log context.
        operation: A zero-argument callable performing ONE provider request.

    Returns:
        Whatever ``operation`` returns.

    Raises:
        Exception: The last exception, re-raised after the attempt budget is
            spent. Never a sentinel and never a swallow.
    """
    for attempt in retrying(call):
        with attempt:
            return operation()
    # Unreachable: reraise=True means the loop either returns or raises.
    raise AssertionError("retry loop completed without a result")  # pragma: no cover
