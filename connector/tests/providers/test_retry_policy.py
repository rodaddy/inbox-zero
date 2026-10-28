"""The shared retry policy, exercised through both SDKs' real error types.

WHY THE REAL ERROR TYPES AND NOT A STUB
    The policy's whole job is reading a status and a reason off objects it did
    not construct. A hand-rolled stub with the attributes the policy happens to
    look for would prove only that the test and the policy agree with each
    other -- it would keep passing if ``HttpError`` moved the body off
    ``.content`` tomorrow. ``googleapiclient.errors.HttpError`` and
    ``httpx.HTTPStatusError`` are cheap to build offline, so there is no reason
    to assert against an imitation. No network is touched here.

THE DISTINCTION UNDER TEST
    Gmail answers throttling with 403 as well as 429
    (https://developers.google.com/workspace/gmail/api/guides/handle-errors),
    while a Graph 403 is a genuine permission denial. The policy must separate
    those on the error body's machine-readable reason, never on which adapter
    is calling -- so these tests deliberately include a Graph 403 and a Gmail
    permission 403 alongside the throttled one.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from googleapiclient.errors import HttpError

from inbox_zero_connector.providers.retry import RETRYABLE_STATUS, is_retryable


class _Resp:
    """The minimal httplib2-style response ``HttpError`` reads its status from."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Forbidden" if status == 403 else "Error"


def _google_error(status: int, *, reasons: list[str] | None = None) -> HttpError:
    """Build a real ``HttpError`` carrying a Google-shaped JSON error body."""
    body: dict[str, Any] = {"error": {"code": status, "message": "boom"}}
    if reasons is not None:
        body["error"]["errors"] = [
            {"domain": "usageLimits", "reason": reason, "message": "boom"} for reason in reasons
        ]
    return HttpError(_Resp(status), json.dumps(body).encode("utf-8"), uri="https://example.invalid")


def _graph_error(
    status: int,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    """Build a real ``httpx.HTTPStatusError`` with a Graph-shaped error body."""
    payload = body if body is not None else {"error": {"code": "ErrorAccessDenied"}}
    request = httpx.Request("GET", "https://graph.microsoft.invalid/v1.0/me/messages")
    response = httpx.Response(status, json=payload, request=request, headers=headers)
    return httpx.HTTPStatusError("graph failed", request=request, response=response)


# -- the defect this module exists to prevent -----------------------------


@pytest.mark.parametrize(
    "reason",
    ["rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "quotaExceeded"],
)
def test_a_gmail_403_carrying_a_rate_limit_reason_is_retried(reason: str) -> None:
    """403 + a throttling reason means "slow down", so it must earn another attempt.

    This is the regression the module was audited for. Gmail signals rate
    limiting with 403 as well as 429, so a policy that read only the status
    would kill a healthy watcher the first time Google throttled it -- and a
    poller is exactly the workload that gets throttled.
    """
    assert is_retryable(_google_error(403, reasons=[reason])) is True


def test_the_reason_match_is_case_insensitive() -> None:
    """Google's enum is camelCase; matching must not depend on that staying so."""
    assert is_retryable(_google_error(403, reasons=["RATELIMITEXCEEDED"])) is True


def test_a_rate_limit_reason_is_found_alongside_unrelated_ones() -> None:
    """``error.errors`` is a list, and the throttling entry need not be first."""
    assert is_retryable(_google_error(403, reasons=["somethingElse", "rateLimitExceeded"])) is True


# -- and the refusals it must NOT weaken ----------------------------------


def test_a_gmail_403_without_a_rate_limit_reason_is_not_retried() -> None:
    """A real permission denial stays fatal. The carve-out is reason-gated, not status-gated."""
    assert is_retryable(_google_error(403, reasons=["insufficientPermissions"])) is False


def test_a_gmail_403_with_no_errors_array_at_all_is_not_retried() -> None:
    """Absent evidence is not evidence of throttling -- the default stays fatal."""
    assert is_retryable(_google_error(403)) is False


def test_a_microsoft_403_is_not_retried() -> None:
    """Graph's 403 IS a permission denial, and no provider-name check rescues it.

    Graph error bodies carry no ``error.errors[].reason``, so the reason lookup
    finds nothing and the status verdict stands -- which is the point of
    deciding from the body instead of from who is calling.
    """
    assert is_retryable(_graph_error(403)) is False


def test_a_microsoft_403_is_not_retried_even_quoting_a_rate_limit_word() -> None:
    """Prose that merely CONTAINS the word must not trip the policy.

    Only a structured ``error.errors[].reason`` counts. A substring search over
    the body would have retried this, which is why the parser walks the shape.
    """
    denial = _graph_error(403, body={"error": {"message": "rateLimitExceeded is not the issue"}})
    assert is_retryable(denial) is False


def test_a_401_is_not_retried_even_with_a_rate_limit_reason() -> None:
    """The carve-out is scoped to 403. An expired token is not fixed by waiting."""
    assert is_retryable(_google_error(401, reasons=["rateLimitExceeded"])) is False


# -- the pre-existing status behaviour, unchanged -------------------------


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
def test_the_transient_statuses_are_retried_on_status_alone(status: int) -> None:
    """A 429 or a 5xx needs no reason in its body to qualify."""
    assert is_retryable(_google_error(status)) is True
    assert is_retryable(_graph_error(status)) is True


def test_an_exception_with_no_status_is_not_retried() -> None:
    """A plain error carries no verdict, and the policy must not invent one."""
    assert is_retryable(ValueError("no status here")) is False


@pytest.mark.parametrize(
    "content",
    [b"not json at all", b"", b"[]", b'{"error": "a string, not an object"}', b'{"error": {}}'],
)
def test_an_unparseable_error_body_does_not_crash_the_policy(content: bytes) -> None:
    """A malformed body must fall back to the status verdict, never raise.

    ``is_retryable`` runs inside tenacity's predicate during a failure. An
    exception thrown HERE would replace the provider's real error with a
    parser bug at exactly the moment the diagnostics matter.
    """
    assert is_retryable(HttpError(_Resp(403), content, uri="https://example.invalid")) is False


# -- the server's own wait hint -------------------------------------------


def _wait_for(exc: BaseException) -> float:
    """Ask the policy's own ``Retrying`` how long it would sleep after ``exc``.

    The wait is read from the real policy object rather than recomputed here,
    for the same reason the errors above are real: a local reimplementation
    would only prove the test agrees with itself. tenacity 9.1.4 takes
    ``RetryCallState(retry_object, fn, args, kwargs)`` and an exc_info triple
    on ``set_exception``, which is the whole state its wait callables read.
    """
    from tenacity import RetryCallState

    from inbox_zero_connector.providers.retry import retrying
    from inbox_zero_connector.utils.logging_config import ProviderCall

    state = RetryCallState(retrying(ProviderCall("test", "wait_probe")), None, (), {})
    state.set_exception((type(exc), exc, None))
    return retrying(ProviderCall("test", "wait_probe")).wait(state)


def test_a_retry_after_hint_is_waited_out() -> None:
    """When the server names its own wait, blind jitter is the wrong answer.

    A 429 carrying ``Retry-After: 30`` is the provider stating the fact the
    backoff curve is guessing at, and honouring it is what stops a retry
    landing inside the same throttling window that produced it.
    """
    assert _wait_for(_graph_error(429, headers={"Retry-After": "30"})) == pytest.approx(30.0)


def test_a_retry_after_hint_is_capped_at_120_seconds() -> None:
    """A server may legitimately name an hour, and the kit must not sleep it.

    The hint is trusted as a floor on the wait, never as an unbounded one: a
    connector parked for 3600s is indistinguishable from a hung one.
    """
    assert _wait_for(_graph_error(429, headers={"Retry-After": "600"})) == pytest.approx(120.0)


def test_no_retry_after_header_falls_back_to_jitter() -> None:
    """No hint means the existing exponential-jitter curve stands, unchanged."""
    from inbox_zero_connector.providers.retry import MAX_WAIT_SECONDS

    assert 0 <= _wait_for(_graph_error(429)) <= MAX_WAIT_SECONDS


def test_an_http_date_retry_after_falls_back_to_jitter() -> None:
    """``Retry-After`` may carry an HTTP-date, which this policy does not read.

    An unparseable hint is not a hint, and guessing at one is worse than the
    curve that is already bounded and known-good.
    """
    from inbox_zero_connector.providers.retry import MAX_WAIT_SECONDS

    hinted = _graph_error(429, headers={"Retry-After": "Fri, 21 Aug 2026 10:00:00 GMT"})
    assert 0 <= _wait_for(hinted) <= MAX_WAIT_SECONDS


def test_a_negative_retry_after_falls_back_to_jitter() -> None:
    """A negative delay is nonsense, so it is discarded rather than clamped silently."""
    from inbox_zero_connector.providers.retry import MAX_WAIT_SECONDS

    assert 0 <= _wait_for(_graph_error(429, headers={"Retry-After": "-5"})) <= MAX_WAIT_SECONDS
