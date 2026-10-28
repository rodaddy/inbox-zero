"""The shared date-bound parse: what it accepts, and what its refusal says.

Both adapters now refuse through ``parse_iso_date``, so the parse-not-
pattern-match property and the no-echo property are proven once, here,
rather than re-argued in each adapter's suite.
"""

from __future__ import annotations

from datetime import date

import pytest

from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.dates import parse_iso_date

_HINT = "pass the date as YYYY-MM-DD."


@pytest.mark.parametrize("value", ["2026-01-31", " 2026-01-31 ", "20260131"])
def test_a_real_calendar_date_parses_to_the_date_itself(value: str) -> None:
    """Surrounding whitespace and the compact ISO form are still dates."""
    assert parse_iso_date(value, field="A date bound", hint=_HINT) == date(2026, 1, 31)


def test_a_hostile_bound_is_refused_without_echoing_what_arrived() -> None:
    """The message reaches a log line and the host AI's context, so the
    attacker-controlled value must not survive into it -- while the refusal
    still tells the caller what to do instead."""
    with pytest.raises(ProviderError) as excinfo:
        parse_iso_date("2026-01-31 OR in:anywhere ZZQX", field="A date bound", hint=_HINT)
    message = str(excinfo.value)
    assert "ZZQX" not in message, "the rejected value was echoed back"
    assert "ACTION REQUIRED" in message


def test_an_impossible_calendar_date_is_refused() -> None:
    """February 30th matches every date-shaped regex and is not a date."""
    with pytest.raises(ProviderError):
        parse_iso_date("2026-02-30", field="A date bound", hint=_HINT)
