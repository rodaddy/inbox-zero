"""Parse-not-pattern-match for caller-supplied date bounds, once.

Both adapters accept ``after``/``before`` dates from the host AI and both
must refuse anything that is not a real calendar date, because a value
that terminates its own term turns the rest of a query into syntax. Both
already parsed with ``date.fromisoformat`` and re-emitted from the PARSED
object so nothing of the input survives. The parse and the refusal live
here; each adapter formats the resulting ``date`` for its own syntax
(Gmail wants ``YYYY/MM/DD``, Graph wants ISO). The rejected value is
never echoed: it is attacker-controlled and the message reaches a log
line and the host AI's context.
"""

from __future__ import annotations

from datetime import date

from inbox_zero_connector.providers.base import ProviderError


def parse_iso_date(value: str, *, field: str, hint: str) -> date:
    """Return the parsed calendar date or raise ``ProviderError`` without echoing ``value``."""
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ProviderError(
            f"{field} is not a valid ISO date and was refused before any request was made. "
            f"A date bound is a DATE (YYYY-MM-DD). ACTION REQUIRED: {hint}"
        ) from exc
