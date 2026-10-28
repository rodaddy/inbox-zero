"""Read-union-write for Graph message ``categories``, guarded by ``If-Match``.

WHY THIS IS A SEPARATE MODULE

``outlook.py`` sat at 469 of its 500 code-line ceiling when this landed, and
the alternative -- compressing the adapter's existing docstrings to make room
-- deletes the security argument an auditor reads in order to fit a feature.
A new file costs nothing and hides nothing: ``_graph_modules()`` in
``tests/test_outlook_provider.py`` enumerates every ``outlook*.py`` on disk
and asserts the structural scans cover it, so moving code cannot move it out
from under a guard.

WHY THE WHOLE DANCE IS NECESSARY (verified live 2026-08-18, real tenant)

A ``PATCH`` of ``categories`` REPLACES the collection. Writing ``["B"]`` onto
a message carrying ``["A"]`` answers ``200 OK`` and ``A`` is GONE -- silently,
with no error to catch. In a law firm's mailbox those labels can carry matter,
client, or privilege meaning, so a naive write is real data loss with no
signal. Adding one category therefore means: read the current set, union ours
in, write the FULL set back.

That read-then-write has a window. Between the GET and the PATCH the owner in
Outlook, an Outlook rule, or another tool can re-categorise the message, and
our write would then clobber them from a stale read -- the same loss through a
narrower gap.

``If-Match`` closes it, and that it does so is OBSERVED rather than
vendor-promised: Microsoft does not list ``If-Match`` in the ``message:
update`` request-headers table at all. Live receipts:

    current etag    -> 200, the write lands
    stale etag      -> 412 ErrorIrresolvableConflict, resource UNTOUCHED
    garbage / ``*`` -> 400 ErrorInvalidChangeKey, write refused

BRANCH ON THE HTTP STATUS, NEVER ON THE ``error.code`` STRING. The code was
guessed once as ``preconditionFailed`` and the guess was wrong; the real value
is the EWS-legacy ``ErrorIrresolvableConflict``. Graph's own error page says
the ``message`` field may change at any time, and ``outlook_errors`` withholds
upstream free text on purpose, so a string is not something to steer on.

The 412/400 split is the load-bearing one, and collapsing it is the defect
this module is shaped to prevent. 412 means SOMEBODY ELSE WROTE FIRST -- our
view is stale, the resource is provably unchanged, and re-GET/re-union/re-PATCH
is safe and correct. 400 means OUR OWN header was malformed, which is a bug in
this package; retrying it would spin the whole attempt budget on our defect and
report contention that never happened.

THE FALLBACK THAT MUST NEVER BE WRITTEN

    except PreconditionFailed:
        patch(categories=union, headers={})   # no If-Match, force it through

Dropping the guard to force the write through reintroduces exactly the
lost update ``If-Match`` was added to prevent. Exhausting the budget RAISES.
Losing three races in a row means a human is editing that message right now,
and deferring to them is the correct outcome, not a failure to try harder.

WHAT THIS MODULE DELIBERATELY DOES NOT DO

    No ledger write. Providers do not ledger; the caller records the outcome.
    No ``W/`` stripping. The prefix is optional in both directions (live:
        identical behaviour with and without it), so a normaliser would be
        code with no job that could only ever be wrong.
    No ``parentFolderId``, ever. Graph ignores it on PATCH and answers 200, so
        a stray field would look successful while doing nothing -- and the
        no-move guarantee here is code-layer, not scope-layer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from loguru import logger

from inbox_zero_connector.providers.base import ProviderError

CATEGORIES_FIELD: Final = "categories"
"""The one writable property this module ever sends."""

ETAG_FIELD: Final = "@odata.etag"
"""Where the etag lives, and the ONLY place it lives.

There is no ``ETag`` HTTP response header on a Graph message read -- every
response in the live run reported none. Code reading ``response.headers`` for
it gets nothing. It is a body property, emitted regardless of ``$select``.
"""

IF_MATCH_HEADER: Final = "If-Match"

PRECONDITION_FAILED: Final = 412
"""Somebody else wrote between our GET and our PATCH. Retryable, after a re-GET."""

BAD_REQUEST: Final = 400
"""A malformed ``If-Match``. OUR bug, never retried."""


class GraphStatusError(ProviderError):
    """A ``ProviderError`` that still knows which HTTP status produced it.

    A SUBCLASS rather than a new field on ``ProviderError`` so that every
    existing ``except ProviderError`` keeps catching it unchanged and the
    message stays byte-identical -- nothing outside this path can tell the
    difference. The status is carried because the conditional-write loop has to
    tell 412 (somebody else wrote; re-read and try again) from 400 (our own
    malformed header; a bug, stop). Both are ``>= 400`` and both otherwise
    arrive as the same bounded refusal, so without the status the only way to
    distinguish them would be parsing the message string -- which is the
    dependency on server prose that ``outlook_errors`` exists to refuse.
    """

    def __init__(self, message: str, status: int) -> None:
        """Carry the bounded message and the status that caused it."""
        super().__init__(message)
        self.status = status


def etag_of(payload: dict[str, Any]) -> str:
    """The message's ``@odata.etag``, verbatim, or refuse.

    Returned BYTE-FOR-BYTE as received, ``W/`` prefix and quotes included. The
    prefix is optional to Graph (live-verified: a bare quoted tag and a
    ``W/``-prefixed one behave identically, both matching and both rejecting),
    so sending back what arrived is both simplest and correct, and a
    normalising step would be code that can only introduce a defect.

    Raises:
        ProviderError: Graph returned no usable etag, so the write cannot be
            guarded. Refusing beats writing unguarded -- an unguarded write is
            the clobber this whole path exists to prevent.
    """
    value = payload.get(ETAG_FIELD)
    if isinstance(value, str) and value.strip():
        return value
    raise ProviderError(
        "Microsoft Graph returned no @odata.etag for this message, so the category "
        "write could not be guarded against overwriting a change made in Outlook. "
        "Nothing was changed. ACTION REQUIRED: retry; if this persists, re-run "
        "'uv run inbox-zero-connector auth microsoft'."
    )


def current_categories(payload: dict[str, Any]) -> list[str]:
    """The message's categories as Graph reported them, blanks dropped.

    Read from the RAW payload rather than through ``GraphMessage`` because the
    union has to be written back and the etag read from the same body, and
    ``GraphMessage`` models neither ``@odata.etag`` nor round-trip fidelity --
    it is ``extra="ignore"``, which is right for reads and wrong for a value
    that must survive a write.
    """
    raw = payload.get(CATEGORIES_FIELD)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item]


def union_categories(current: list[str], label: str) -> list[str] | None:
    """The full set to write, or ``None`` when ``label`` is already present.

    ``None`` is the NO-OP signal and it is a real outcome, not an error: a
    write that changes nothing still costs a request and still bumps the etag,
    which makes every other client's in-flight ``If-Match`` fail for no reason.

    Matching is case-insensitive to agree with the comparison the adapter's own
    category filtering already uses, so a message carrying ``needs-reply`` is
    not given a second ``Needs-Reply``. Existing entries are preserved EXACTLY
    as Graph reported them -- ours is appended, theirs is never re-cased.
    """
    if any(existing.casefold() == label.casefold() for existing in current):
        return None
    return [*current, label]


def apply_category(
    read: Callable[[], dict[str, Any]],
    write: Callable[[list[str], dict[str, str]], object],
    label: str,
    *,
    attempts: int,
) -> str:
    """Add ``label`` to a message's categories without losing anyone else's.

    One attempt is the WHOLE sequence -- re-read, re-union, re-write -- and
    that is the point rather than an implementation detail. A retry that
    replays the previous body reintroduces the clobber the loop was added to
    prevent, because the previous body is stale BY DEFINITION: a 412 is Graph
    saying so.

    ``read`` and ``write`` arrive as callables so this module owns the
    concurrency rule and nothing else: no client, no path assembly, no token.
    That is also what lets the loop be tested without a transport.

    Args:
        read: Fetch the message's ``categories`` and ``@odata.etag``.
        write: Send the full category set under an ``If-Match`` header. Its
            return value is DISCARDED -- the updated entity Graph echoes back
            is not read, because the only thing that matters here is that the
            write was accepted.
        label: The label to add, ALREADY validated by the caller.
        attempts: Total attempts, not retries. Bounded; see ``MAX_ATTEMPTS``.

    Returns:
        ``label``, as applied.

    Raises:
        ProviderError: The attempt budget was spent on 412s, or Graph refused
            for any other reason. Never a silent success -- the caller ledgers
            the outcome and cannot do that from a return value that lies.
    """
    for attempt in range(1, attempts + 1):
        outcome = _attempt_once(read, write, label)
        if outcome is None:
            return label
        # "attempt=1 of 3", never "1/3": the structural scan in
        # tests/test_outlook_provider.py refuses an f-string whose literal parts
        # look path-shaped, and a slash between two interpolations is exactly
        # that shape. The scan is right to be blunt here -- it is what keeps a
        # dynamic value out of a Graph URL -- so the message bends, not the scan.
        logger.warning(
            f"GRAPH: category write lost a race attempt={attempt} of {attempts} "
            "status=412 (re-reading and retrying)"
        )
    raise ProviderError(
        f"Microsoft Graph refused the category write {attempts} times running: the "
        "message's categories changed in Outlook between each read and write. "
        "Nothing was changed and no existing category was lost. ACTION REQUIRED: "
        "somebody is editing this message right now -- retry once they are done."
    )


def _attempt_once(
    read: Callable[[], dict[str, Any]],
    write: Callable[[list[str], dict[str, str]], object],
    label: str,
) -> GraphStatusError | None:
    """One read-union-write. ``None`` on success or no-op; the 412 to retry on.

    Extracted so ``apply_category`` stays a flat loop over a single decision.
    Only a 412 is RETURNED -- every other failure, including the 400 that means
    our own header was malformed, propagates immediately and unretried.
    """
    payload = read()
    union = union_categories(current_categories(payload), label)
    if union is None:
        logger.debug(f"GRAPH: category already present, no write issued label={label!r}")
        return None
    try:
        write(union, {IF_MATCH_HEADER: etag_of(payload)})
    except GraphStatusError as exc:
        if exc.status == PRECONDITION_FAILED:
            return exc
        raise
    return None
