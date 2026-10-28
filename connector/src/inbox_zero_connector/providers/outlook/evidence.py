"""Evidence types for the Graph adapter: values that CANNOT be degenerate.

WHY THIS MODULE EXISTS, AND WHY IT IS A SEPARATE FILE

``graph.py`` proved a message was sent mail by comparing two strings that were
each allowed to be empty::

    sent_folder_id = str(folder.get("id") or "")            # "" when unresolvable
    if str(item.get("parentFolderId") or "") != sent_folder_id:  # "" != "" is False

Both halves degrade to the empty string under the SAME condition -- absent
evidence -- so the comparison that was supposed to REFUSE an unproven message
accepted it instead, and ``read_sent_sample`` returned the full body of an
inbox message. The guard was not weak; it was inverted by emptiness.

Patching the comparison (``if not sent_folder_id: raise``) fixes the instance
and leaves the SHAPE: the next reader of ``self._folder_cache`` gets a ``str``
that is still allowed to be empty, and has to remember the rule again. So the
fix is a TYPE. ``ResolvedId`` cannot be constructed empty, which means a
comparison against one has no degenerate form to reach -- the failure mode is
removed from the vocabulary rather than guarded at each use.

It lives in its own module because ``graph.py`` is at its 500 code-line
ceiling, and because the alternative -- compressing working code to make room
-- is what caused a real regression the last time it was tried. A new file
costs nothing and hides nothing: the structural scans in
``tests/test_graph_provider.py`` scan this module alongside ``graph.py``
exactly so that moving code cannot move it out from under a guard.
"""

from __future__ import annotations

from loguru import logger

from inbox_zero_connector.providers.base import ProviderError
from inbox_zero_connector.providers.dates import parse_iso_date


class ResolvedId(str):
    """A provider id PROVEN non-empty at construction. Absence cannot be spelled.

    A ``str`` subclass rather than a dataclass so it compares, formats, and
    logs exactly like the string it replaces -- every existing reader stays
    byte-identical -- while making ``ResolvedId("")`` impossible to hold.

    THE DEGENERATE-CASE ANSWER, stated because this type exists to answer it:
    given empty, whitespace, or ``None``, construction RAISES. There is no
    value of this type that equals ``""``, so a membership test written against
    one cannot pass by both sides being absent. It fails CLOSED, and it fails
    at the moment the evidence goes missing rather than at the comparison that
    trusted it.

    Attributes:
        Nothing beyond ``str``. The invariant IS the type.
    """

    __slots__ = ()

    def __new__(cls, value: object, *, what: str) -> ResolvedId:
        """Build a non-empty id, or refuse.

        Args:
            value: The candidate, typically straight off a Graph response.
                Any type: a missing JSON key yields ``None`` and must refuse
                rather than stringify to ``"None"``.
            what: What was being resolved, for the refusal message. A FIXED
                string from the calling module, never caller-supplied text --
                this message reaches a log line and the host AI's context.

        Returns:
            The id, as a ``str`` subclass guaranteed non-empty.

        Raises:
            ProviderError: If ``value`` is not a non-empty, non-blank string.
        """
        if not isinstance(value, str) or not value.strip():
            raise ProviderError(
                f"Microsoft Graph did not return a usable {what}, so the connector "
                "could not prove what it was about to act on and refused. Nothing "
                "was read or changed. ACTION REQUIRED: retry; if this persists, "
                "re-run 'uv run inbox-zero-connector auth microsoft'."
            )
        return super().__new__(cls, value)


def resolved_or_none(value: object, *, what: str) -> ResolvedId | None:
    """``ResolvedId`` when the evidence is present, ``None`` when it is absent.

    For the side of a comparison that must be allowed to be MISSING -- a
    message whose ``parentFolderId`` Graph did not return -- while still being
    unable to be empty. ``None`` is not equal to any ``ResolvedId``, so the
    caller's membership test refuses absent evidence by construction instead of
    by remembering to check for ``""`` first.

    Args:
        value: The candidate, straight off a Graph response.
        what: What was being resolved. Unused in the return, present so the
            two constructors read identically at their call sites.

    Returns:
        The id, or ``None`` when Graph supplied nothing usable.
    """
    del what  # documented at the call site; no message is emitted on this path
    if not isinstance(value, str) or not value.strip():
        return None
    return ResolvedId(value, what="id")


def parse_date_bound(bound: str) -> str:
    """Return an ISO date RE-EMITTED from the parsed object, or raise.

    PARSE, DO NOT PATTERN-MATCH. The predecessor matched ``\\d{4}-\\d{2}-\\d{2}``
    and returned the INPUT, so ``2026-02-30``, ``9999-99-99`` and ``0000-00-00``
    all satisfied the shape, reached Graph, and failed there -- opaquely, since
    the adapter's error path withholds upstream text on purpose. The caller got
    "HTTP 400" with no way to learn that their date was the problem.

    ``date.fromisoformat`` accepts a real calendar date and nothing else, and
    the value handed back is BUILT FROM THE PARSED OBJECT, so nothing of what
    arrived survives into the filter. That is a stronger property than any
    filter: a value that parses cannot contain a space, a quote, or an OData
    operator by construction, so the injection argument the shape check was
    making is preserved rather than traded away.

    This mirrors ``gmail._validate_date``, which already parsed. The two
    adapters disagreeing about what a date bound IS was the defect; matching
    them is the fix.

    Args:
        bound: The caller's date bound, from an MCP tool parameter.

    Returns:
        ``YYYY-MM-DD``, generated from three integers.

    Raises:
        ProviderError: If ``bound`` is not a real calendar date. The rejected
            value is NOT echoed -- it is attacker-controlled and this text
            reaches a log line and the host AI's context.
    """
    parsed = parse_iso_date(
        bound,
        field="A date bound",
        hint=(
            "pass 'after' and 'before' as calendar dates, for example 2026-01-31. "
            "A bound that is not a real calendar date is refused here rather than "
            "opaquely by Graph."
        ),
    ).isoformat()
    logger.debug(f"GRAPH: date bound ok bound={parsed}")
    return parsed


_CALLER_HEADERS = frozenset({"if-match"})
"""Request headers a caller of ``_patch`` may set. Lowercase, HTTP is case-insensitive.

ALLOWLIST, NOT MERGE ORDER, and the choice is the security argument. A merge
would be one line and would work today: httpx applies per-request headers over
the client's, so ``Authorization`` is stomped only by a caller that names it,
and no caller does. That is precisely the problem -- it makes safety a property
of what callers HAPPEN to pass rather than of the mechanism, so the guarantee
is re-established by reading every call site, forever, and it is silently lost
the first time a header name arrives from a response, a config file, or an
error body. Reversing the merge (client headers last) is worse in a different
direction: it would fail SILENTLY, dropping a header the caller needed with
nothing to say so.

The allowlist fails loudly instead, and it fails BEFORE a request exists. It is
the same shape as ``_STATIC_SEGMENTS`` one constant above, for the same reason:
holding the permitted vocabulary as data lets the helper treat "not in this
set" as "refuse", rather than relying on each caller to have been careful. The
round-1 mistake in that guard was that it was opt-in; this one cannot be.

``Authorization`` and ``Content-Type`` are therefore unreachable by
construction -- not merely outranked. ``Authorization`` is set once on the
client in ``_http`` and is the only thing standing between this adapter and an
unauthenticated or WRONGLY-authenticated call; ``Content-Type`` is set by
httpx's ``json=`` and a caller changing it silently changes how Graph parses
the body. Neither is a per-request decision, so neither is on this list.

Adding a name here is a deliberate edit with a reviewer, which is the point.
"""


def caller_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Validate caller-supplied headers against the allowlist. Flat, no nesting.

    Returns the mapping unchanged when every name is permitted. Raises
    ``ProviderError`` naming the offending header otherwise -- loudly, and
    before the request is built, so a rejected header can never reach the wire
    even in a form that "would have been harmless". ``Authorization`` and
    ``Content-Type`` are refused by this rule like any other unlisted name.
    """
    refused = sorted(name for name in headers or {} if name.lower() not in _CALLER_HEADERS)
    if refused:
        raise ProviderError(
            f"outlook: header(s) {', '.join(refused)} may not be set per request. "
            f"ACTION REQUIRED: only {', '.join(sorted(_CALLER_HEADERS))} is allowed."
        )
    return headers or {}
