"""The one guard every caller-supplied id passes before a request is built.

Gmail and Graph each carried the same three-arm refusal -- empty or
whitespace, longer than 512 characters, outside the id grammar -- with
the same no-echo rule and the same ACTION REQUIRED voice. Held twice the
two had already begun to differ in wording. Held here, the arms are one
function and each adapter keeps only what is genuinely its own: the
grammar (Gmail's hex-ish ids, Graph's base64url) and the tail (Gmail
returns the id unchanged for a query parameter; Graph percent-encodes it
for a URL path).

The rejected value is NEVER echoed: it is attacker-controlled text that
would land in a log line and in the host AI's context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from inbox_zero_connector.providers.base import ProviderError

if TYPE_CHECKING:
    import re

MAX_ID_CHARS: Final = 512
"""Longest id either adapter accepts. One number, deliberately."""


def validate_id(
    value: str,
    *,
    field: str,
    pattern: re.Pattern[str],
    kind: str,
    hint: str,
) -> str:
    """Return ``value`` unchanged or raise ``ProviderError`` without echoing it.

    ``field`` names the parameter in the message, ``kind`` names the
    provider's id family ("Gmail", "Microsoft Graph conversation"), and
    ``hint`` is the ACTION REQUIRED tail each adapter already used.
    """
    if not value or not value.strip():
        raise ProviderError(f"{field} is required. ACTION REQUIRED: {hint}")
    if len(value) > MAX_ID_CHARS:
        raise ProviderError(
            f"{field} is longer than {MAX_ID_CHARS} characters and was refused before "
            f"any request was made. ACTION REQUIRED: {hint}"
        )
    if not pattern.fullmatch(value):
        raise ProviderError(
            f"{field} contains characters that are not valid in a {kind} id and was "
            f"refused before any request was made. ACTION REQUIRED: {hint}"
        )
    return value
