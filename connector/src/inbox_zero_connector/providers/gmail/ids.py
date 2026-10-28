"""Gmail boundary validation for caller-supplied ids, and address parsing.

Split out of ``gmail.py`` because that module sat at the 500-code-line ceiling
and the last attempt to make room by COMPRESSING working code shipped a real
regression -- the address-parsing one this module now owns the fix for. Moving
whole functions out is the change that costs nothing: the same code runs, and
the budget comes from the file boundary rather than from making a working
function terser.

Two concerns live here because both are about untrusted text arriving at the
Gmail adapter, and both were previously handled inconsistently:

1. **Caller-supplied ids** (``_safe_id``). ``thread_id`` and ``message_id``
   come from MCP tool parameters, which are attacker-controlled whenever the
   host AI is reading attacker-controlled mail. ``server.py`` strips whitespace
   and nothing else. Graph validated these at its boundary and refused to echo
   a rejected value back; Gmail did neither. This module gives Gmail the same
   grammar, the same length bound, and the same no-echo policy, so a reviewer
   comparing the two adapters finds one answer instead of two.

2. **Header addresses** (``split_addresses``). ``email.utils.getaddresses``
   fails for the WHOLE input it is handed, never per token -- see that
   function's own docstring for why that makes the obvious "parse it all at
   once" shape a blast-radius bug rather than an optimisation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from email.utils import getaddresses

from inbox_zero_connector.providers.ids import validate_id

_ID_RE = re.compile(r"[A-Za-z0-9_\-=.]+")
"""The grammar a Gmail id is allowed to use.

An ALLOWLIST, not a blocklist, and the same one ``graph._MESSAGE_ID_RE`` uses.
A blocklist has to anticipate every dangerous character -- quote, space,
newline, the Gmail search operators, whatever the next parser disagreement
turns out to be -- so anything unforeseen fails OPEN. An allowlist admits only
what a real id contains, so an unanticipated metacharacter fails CLOSED by
default without having been named.

Gmail's own ids are hex, which this comfortably covers. The extra characters
are carried so the two adapters share one grammar rather than maintaining a
tighter Gmail one that drifts.
"""


def safe_id(value: str, field: str) -> str:
    """Return ``value`` unchanged, or refuse it before any request goes out.

    ABSENT EVIDENCE REFUSES. Every arm below raises rather than falling
    through: an empty id, a whitespace-only id, an over-long id, and an id
    outside the grammar are each declined, and the function has no path that
    returns a value it did not check. There is no "unknown, so allow" branch,
    because the input is an MCP tool parameter and the caller cannot prove
    anything about it.

    THE REJECTED VALUE IS NEVER ECHOED. This is the policy Graph already
    followed and Gmail did not, and it is settled HERE toward the stricter of
    the two. The refusal text reaches two places that matter: a log line (via
    ``ProviderCall``'s exception handling, which logs with ``exc_info``) and
    the host AI's own context, because a ``ProviderError`` message is what the
    tool layer hands back. Repeating attacker-authored text into either turns a
    refusal into a delivery mechanism for the injection it just blocked -- the
    guard fires, the payload arrives anyway, and it arrives wearing the
    connector's own voice. The caller already knows what it sent; naming the
    FIELD is what makes the refusal actionable, and it is enough.

    The one deliberate exception to no-echo lives in ``gmail._require_sent``,
    which names an id that PASSED this validation. That value is grammar-bound
    and length-bound by the time it is quoted, so it can carry no payload, and
    a refusal spanning several ids is unactionable without saying which one.
    Echoing a VALIDATED id and echoing a REJECTED one are different acts.

    Args:
        value: The caller-supplied id, straight off a tool parameter.
        field: The parameter's name, used in the refusal. Never the value.

    Returns:
        The id, unchanged. Gmail's client library sends it as a query
        parameter rather than a path segment, so nothing is encoded here --
        unlike Graph, which builds a URL path and must percent-encode.

    Raises:
        ProviderError: The id is empty, over-long, or outside the id grammar.
    """
    return validate_id(
        value,
        field=field,
        pattern=_ID_RE,
        kind="Gmail",
        hint="pass an id exactly as list_inbox returned it, unmodified.",
    )


_SEMICOLON_FALLBACK = re.compile(r";\s*")
"""Splits a semicolon-separated recipient list into per-address chunks.

Applied ONLY after the RFC-conformant parse of the whole header has already
come back empty. RFC 5322 separates addresses with commas, but Outlook-origin
mail routinely uses semicolons and the strict parser answers the entire header
with one empty pair rather than declining the token it disliked -- so ordinary
mail read as a thread with no participants at all.
"""


def split_addresses(value: str) -> list[str]:
    """Split ONE header value into addresses, keeping display names intact.

    ONE HEADER PER CALL, AND THAT IS THE WHOLE CONTRACT. ``getaddresses`` is
    all-or-nothing: given ``'"Unterminated <a@x>, b@y'`` it returns a single
    empty pair, discarding the perfectly good ``b@y`` along with the token it
    could not parse. It offers no per-token failure and no partial result.

    That property makes "parse everything at once" a blast-radius bug, not an
    optimisation. Joining a thread's headers into one string before parsing --
    which is exactly what a previous revision did to save lines -- means one
    hostile ``From`` on one message erases every address on the conversation:
    ``get_thread`` reported no participants, and ``create_draft`` refused the
    thread for having nobody to reply to, blaming the mail for a parser bug.
    Passing a LIST of headers is the same defect wearing a different shape,
    because ``getaddresses`` joins its list with ``", "`` internally before
    parsing. The only safe granularity is one call per header, which is what
    every caller does and what the callers' tests pin.

    The semicolon fallback is second, never first. A conformant comma-separated
    header parses on the first attempt and never reaches it; the fallback runs
    only when the strict parse found NOTHING, so it cannot loosen the parse of
    a header that already worked. Its own result is parsed per chunk by the
    same strict parser, so a chunk that is genuinely malformed still yields
    nothing rather than being waved through -- the fallback widens which
    SEPARATORS are understood, not what counts as an address.

    Args:
        value: A single raw header value, such as one message's ``To``.

    Returns:
        The bare addresses found, in header order. Empty when the header is
        empty or unparseable -- which for a single header is the honest answer.
    """
    found = _parse_one(value)
    if found or ";" not in value:
        return found
    return [
        address
        for chunk in _SEMICOLON_FALLBACK.split(value)
        if chunk.strip()
        for address in _parse_one(chunk)
    ]


def _parse_one(value: str) -> list[str]:
    """One strict ``getaddresses`` call over one string. The only parse site."""
    return [
        formatted
        for _, address in getaddresses([value])
        if address and (formatted := address.strip())
    ]


def unique(values: Iterable[str]) -> list[str]:
    """De-duplicate, preserving first-seen order.

    ``dict.fromkeys`` rather than a set: the callers feed lists a human reads
    (a thread's participants, a thread's labels), and a set would reorder them
    arbitrarily between runs.
    """
    return list(dict.fromkeys(value for value in values if value))


def bare(address: str) -> str:
    """Lowercased bare address for comparison. Display names do not survive.

    Reuses ``split_addresses`` rather than re-parsing, so "who counts as this
    address" is decided in exactly one place for both the recipient lists and
    the owner comparison made against them.

    An unparseable input yields ``""``. That is the safe direction for the one
    caller that matters: ``gmail._without_owner`` treats an empty owner as
    "could not resolve" and drops NOBODY, so a parse failure produces a reply
    that includes the owner -- which mail clients de-duplicate -- rather than
    one that silently drops a real correspondent.
    """
    found = split_addresses(address)
    return found[0].lower() if found else ""
