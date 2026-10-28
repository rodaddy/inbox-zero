"""The flow decorator's contract: enter/exit at DEBUG, failure at ERROR with traceback, re-raise unchanged."""

from __future__ import annotations

from typing import Any

import pytest
from loguru import logger

from inbox_zero_connector.utils import logged
from inbox_zero_connector.utils.logging_config import _redact


def _capture(fn: Any) -> list[Any]:
    """Run ``fn`` with a DEBUG-level loguru sink attached and return the records."""
    records: list[Any] = []
    handle = logger.add(records.append, level="DEBUG", format="{message}")
    try:
        fn()
    finally:
        logger.remove(handle)
    return records


def test_logged_emits_enter_and_exit_at_debug() -> None:
    """A successful call returns its value and leaves an enter and an exit line."""

    @logged
    def work() -> int:
        return 7

    results: list[int] = []
    records = _capture(lambda: results.append(work()))

    assert results == [7]
    enters = [
        r
        for r in records
        if "FLOW: enter" in r.record["message"] and r.record["level"].name == "DEBUG"
    ]
    exits = [
        r
        for r in records
        if "FLOW: exit" in r.record["message"] and r.record["level"].name == "DEBUG"
    ]
    assert enters
    assert exits


def test_logged_reraises_and_logs_error_with_traceback() -> None:
    """A raising call logs FLOW: fail at ERROR with a traceback, then re-raises."""

    @logged
    def boom() -> None:
        raise ValueError("boom")

    records: list[Any] = []
    handle = logger.add(records.append, level="DEBUG", format="{message}")
    try:
        with pytest.raises(ValueError):
            boom()
    finally:
        logger.remove(handle)

    failures = [
        r
        for r in records
        if "FLOW: fail" in r.record["message"] and r.record["level"].name == "ERROR"
    ]
    assert failures
    assert failures[0].record["exception"] is not None


def test_logged_preserves_metadata() -> None:
    """The wrapper keeps the wrapped function's name and docstring."""

    @logged
    def named() -> None:
        """Original doc."""

    assert named.__name__ == "named"
    assert named.__doc__ == "Original doc."


def _capture_redacted(fn: Any) -> tuple[list[Any], list[str]]:
    """Run fn under the redaction patcher, returning its records and rendered text."""
    records: list[Any] = []
    rendered: list[str] = []
    logger.configure(patcher=_redact)
    handle = logger.add(records.append, level="ERROR", format="{message}", diagnose=False)
    handle2 = logger.add(
        rendered.append,
        level="ERROR",
        format="{message}",
        backtrace=False,
        diagnose=False,
    )
    try:
        with pytest.raises(ValueError):
            fn()
    finally:
        logger.remove(handle2)
        logger.remove(handle)
        logger.configure(patcher=None)
    return records, rendered


def test_redaction_patcher_strips_exception_message_but_keeps_frames() -> None:
    """The patcher keeps the traceback and type but drops the exception's message."""

    # Assembled at runtime, never written as a literal: a traceback echoes the
    # SOURCE LINE of the raising frame, so a hardcoded canary would appear in
    # the rendered text no matter how well the patcher worked, and the test
    # would be measuring its own source rather than the redaction.
    canary = "CANARY-" + "SECRET-" + "BODY"

    @logged
    def boom() -> None:
        raise ValueError(canary)

    records, rendered = _capture_redacted(boom)

    assert len(records) == 1
    rec = records[0].record["exception"]
    assert rec is not None
    assert canary not in str(rec.value)
    assert "ValueError" in str(rec.value)
    assert rec.traceback is not None
    assert rec.type is ValueError

    text = "".join(rendered)
    assert canary not in text
    assert "ValueError" in text
