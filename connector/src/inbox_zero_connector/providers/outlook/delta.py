"""Graph's change feed: one per-folder ``messages/delta`` round.

WHY THIS IS ITS OWN MODULE
    Split out of ``outlook.py`` for the same reason ``gmail_history.py`` was
    split out of ``gmail.py``: that module sits at the 500-code-line ceiling
    ``_DOCS/STANDARDS-python.md`` enforces, with TWO lines of headroom measured
    on 2026-08-19 before this work began. The alternative -- compressing
    docstrings to make room -- defeats the gate, because the ceiling counts
    CODE lines precisely so that the security argument an auditor reads is
    never what gets deleted to fit.

    The seam is real and not merely convenient. Everything here answers one
    question -- what changed since a position -- and none of it is reachable
    from any other Graph operation. ``OutlookProvider.poll_changes`` stays in
    ``outlook.py`` as the interface method and delegates the mechanics here.

    The import is ONE-DIRECTIONAL: ``outlook`` imports this module, never the
    other way. That is why the provider's transport factory and its message
    projection arrive as ARGUMENTS rather than as imports -- passing them as
    values is what keeps the dependency arrow pointing one way and this module
    testable without constructing a provider.

GRAPH'S DELTA IS PER-FOLDER, AND GMAIL'S CHANGE FEED IS NOT
    Gmail has ONE mailbox-wide feed: ``users.history.list`` walks forward from
    a ``historyId`` that covers the whole mailbox. Graph has no mailbox-wide
    delta at all -- ``/me/messages/delta`` does not exist -- so covering a
    Graph mailbox costs one stream and one independently-expiring token PER
    FOLDER. ``watchstate.WatchCursor.container_id`` already models exactly that
    difference, storing ``None`` for Gmail and a folder id for Graph.

    The consequence for this module is that folder identity rides INSIDE the
    opaque delta and next links Graph hands back, not in any parameter here.
    One call is one stream is one folder, and WHICH folders to cover is the
    caller's decision, made by calling this once per folder with that folder's
    own cursor. There is no fan-out loop in this file and there must not be
    one: a module that looped over folders would have to own the folder set,
    the page budget, and the failure policy for a partially-covered mailbox --
    three decisions that belong to the watcher loop above it.

    A COLD START is the same GET with no stored position. Graph has no profile
    or position read like Gmail's ``getProfile``, so there is nothing to read
    ahead of the walk: a cold start simply walks ``INBOX_DELTA_PATH`` from the
    beginning and ends, like every other round, on a ``@odata.deltaLink``.

THE 410 IS NORMAL CONTROL FLOW, AND THE KNOWLEDGE STAYS HERE
    Graph answers ``410 Gone`` with error code ``syncStateNotFound`` when the
    supplied delta token has been evicted. Microsoft documents no lifetime
    guarantee for these tokens, so this is EXPECTED operation on a perfectly
    healthy mailbox -- it is the service saying "that position is gone, start
    over" -- and this module answers it with ``ChangeSet(resync_required=True)``
    rather than an exception. A watcher that raised on it would log an error
    where it should have run a cold start.

    The status code and the error code are named in THIS FILE and nowhere else.
    Shared code -- the ``ChangeSet`` model, ``watchstate``, the loop above them
    -- learns only the concept, because the concept ports between providers and
    the numbers do not: Gmail signals the same thing with a bare ``404``.
    Shared code that hardcoded either would silently stop recognising the other.

LINKS ARE OPAQUE, AND THAT IS ENFORCED BY NEVER LOOKING AT THEM
    ``@odata.nextLink`` and ``@odata.deltaLink`` are replayed VERBATIM as the
    next request url. They are never parsed, compared, normalised, or
    constructed. Graph encodes the folder, the token, and its own paging state
    into them in a form it is free to change, and a caller that reconstructed
    one would be inventing a position the service never issued.

WHAT THIS MODULE DOES NOT DO
    It classifies nothing, labels nothing, moves nothing, and persists nothing.
    It reads one page of the feed and reports it. Scope enforcement, the replay
    guard, the filing decision, and the cursor WRITE all belong to the loop
    above it and to the modules that already own them. It issues exactly one
    HTTP verb, ``GET``, against exactly one kind of url.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol

import httpx
from loguru import logger

from inbox_zero_connector.models.mail import Message
from inbox_zero_connector.providers.base import ChangeSet, ProviderError
from inbox_zero_connector.providers.outlook.categories import GraphStatusError
from inbox_zero_connector.providers.outlook.errors import (
    _provider_error,
    _raise_if_retryable,
)
from inbox_zero_connector.providers.outlook.models import GraphMessage, parse_graph
from inbox_zero_connector.providers.retry import with_retry
from inbox_zero_connector.utils.logging_config import ProviderCall

if TYPE_CHECKING:
    from collections.abc import Callable

PROVIDER_LOG_NAME: Final = "microsoft"
"""Which provider this module's log lines are attributed to.

Stated here rather than imported from ``outlook`` because that module imports
THIS one -- taking the name back the other way would be a circular import. It
is the same literal ``outlook.PROVIDER`` holds, and
``test_the_delta_module_logs_under_the_same_provider_name`` asserts the two
agree so the duplication cannot drift.
"""

SYNC_STATE_GONE_STATUS: Final = 410
"""What Graph returns when the supplied delta token has been evicted.

THE ONE PLACE THIS NUMBER APPEARS. It means "your resume position is gone,
resynchronise from scratch" -- documented behaviour with no token lifetime
guarantee, not an error to propagate. See the module docstring for why no
shared module may learn it.
"""

SYNC_STATE_GONE_CODE: Final = "syncStateNotFound"
"""The Graph error code that accompanies an evicted delta token.

THE ONE PLACE THIS STRING APPEARS. Matched CASE-INSENSITIVELY: Graph has been
observed serving ``SyncStateNotFound`` with a leading capital, and a
case-sensitive comparison would have turned an ordinary resync into a raised
exception on a healthy mailbox. The code is checked in addition to the status
so a gateway that rewrites the status but preserves the envelope is still
recognised.
"""

DEFAULT_CONTAINER: Final = "inbox"
"""Which folder a cold start walks when the caller names none.

Graph has no mailbox-wide delta, so "no container" cannot mean "the whole
mailbox" here -- it has to resolve to SOME folder, and the inbox is the one
this connector's product is about. Named rather than inlined so the default and
the caller-supplied case build the same url through the same expression.
"""

DELTA_PATH_TEMPLATE: Final = "me/mailFolders/{container_id}/messages/delta"
"""Where a cold start begins for one folder's stream.

RELATIVE on purpose: the provider's httpx client carries the Graph base url, so
this resolves against it, while every subsequent url in a round is an ABSOLUTE
link Graph handed back. Both go through the same request path because httpx
resolves each correctly and neither is ever inspected here.

This is the ONE url this module ever BUILDS. Every other url it issues is a
link Graph produced, replayed verbatim -- which is why the container reaches
this module as a parameter only for the cold start: on a resume the folder is
already encoded in the stored ``@odata.deltaLink`` and re-deriving it would be
inventing a position the service never issued.
"""

INBOX_DELTA_PATH: Final = DELTA_PATH_TEMPLATE.format(container_id=DEFAULT_CONTAINER)
"""Where a cold start begins for the inbox stream.

RELATIVE on purpose: the provider's httpx client carries the Graph base url,
so this resolves against it, while every subsequent url in a round is an
ABSOLUTE link Graph handed back. Both go through the same request path because
httpx resolves each correctly and neither is ever inspected here.

Graph offers no profile or position read to consult first (Gmail's cold start
reads ``getProfile``), so a cold start IS a full delta walk of the folder from
the beginning -- the same GET, with no stored position yet.
"""


DELTA_SELECT: Final = (
    "id,conversationId,internetMessageId,from,subject,receivedDateTime,bodyPreview,"
    "isRead,importance,flag,parentFolderId,isDraft"
)
"""The projection this module asks Graph for, and it is EXACTLY what is read.

Every name here is consumed: the first eleven are the fields
``outlook._to_message`` projects into a ``Message`` (``internetMessageId`` is
lifted off the raw item, since it is not on ``GraphMessage``), and ``isDraft``
is what ``_added_messages`` filters on.

WHY NAME THEM AT ALL. Without ``$select`` a delta page carries Graph's FULL
message resource, which includes ``body`` -- the owner's mail, in a response
this module logs around and hands to a projection that deliberately never
reads it. Asking for the header fields is the cheap read AND the data
minimisation, in one parameter. Anything added to ``_to_message`` has to be
added here in the same change, because Graph returns only what this names.
"""


class _ToMessage(Protocol):
    """How this module converts one validated Graph message into a ``Message``.

    A Protocol rather than a bare ``Callable`` so ``mypy --strict`` checks the
    KEYWORD argument too: ``internet_message_id`` is passed by name, and a
    ``Callable[..., Message]`` would type-check any callable at all, which is
    the same as not checking. ``outlook._to_message`` satisfies this
    structurally and is passed in as a value, keeping the import arrow pointing
    one way (see the module docstring).
    """

    def __call__(self, item: GraphMessage, *, internet_message_id: str = ...) -> Message:
        """The call shape ``outlook._to_message`` must match, keyword included.

        Declared with the keyword SPELLED OUT because that is the whole point of
        the Protocol: ``internet_message_id`` does not live on ``GraphMessage``
        -- it is lifted off the RAW page item, since ``$select`` on a delta page
        is not guaranteed to project it -- so the keyword is the only channel
        for it, and mistyping it here would be a silent ``TypeError`` at the
        first arrival rather than a mypy failure at edit time.
        """
        ...


def _delta_page(
    call: ProviderCall,
    http: Callable[[], httpx.Client],
    url: str,
    *,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Fetch ONE delta page. Returns the response WITHOUT judging its status.

    Split from ``poll_changes`` so the transport call and the interpretation of
    its result are separate things a test can drive independently -- and
    because the ``410`` this may return is not a failure, so a helper that
    raised on non-2xx would have to be told which non-2xx is fine, which is the
    caller's knowledge.

    The retried callable RAISES on a retryable status rather than returning the
    response, matching ``OutlookProvider._get``. httpx does not raise on
    4xx/5xx, so a bare ``client.get`` inside the wrapper hands the retry policy
    a 429 as a SUCCESSFUL return and ``RETRYABLE_STATUS`` matches nothing.

    Args:
        call: The open ``ProviderCall`` this round logs under.
        http: Returns the authenticated Graph client. The provider's own
            ``_http`` bound method, passed as a value.
        url: A relative path on a cold start, or an absolute ``@odata`` link
            replayed verbatim. Never parsed, and never assembled from parts.
        params: Query parameters for a COLD START only. A replayed ``@odata``
            link already carries Graph's own ``$select`` inside it, and adding
            parameters to one would be editing a position the service issued.

    Returns:
        The Graph response, whatever its status. The caller interprets it.

    Raises:
        ProviderError: The attempt budget was spent on a retryable status.
    """

    def _attempt() -> httpx.Response:
        """One GET, raising on a retryable status so ``with_retry`` can see it."""
        response = http().get(url, params=params)
        _raise_if_retryable(response)
        return response

    try:
        return with_retry(call, _attempt)
    except httpx.HTTPStatusError as exc:
        # The attempt budget is spent. The transient status travelled as an
        # exception only so the retry policy could read it; the adapter's
        # contract is a bounded ProviderError, same as any other failed status.
        raise _provider_error(exc.response, "GET", url) from exc


def _is_sync_state_gone(response: httpx.Response) -> bool:
    """True when Graph refused the supplied delta token rather than failing.

    Two signals, either of which is sufficient. The STATUS is the documented
    one. The error CODE is checked as well, on any 4xx, because a gateway or
    proxy between the connector and Graph can rewrite a status while passing
    the error envelope through intact -- and misreading that as a hard failure
    would raise on a mailbox with nothing wrong with it.

    The code comparison is case-insensitive; see ``SYNC_STATE_GONE_CODE``.

    A non-JSON body answers ``False`` rather than raising. That is not a
    swallow: a body that is not an envelope carries no code, so there is
    nothing to recognise, and the caller then treats the response as the
    ordinary failure it is and reports it through ``_provider_error``. NO
    EXCERPT OF THE BODY IS READ OR LOGGED -- a rejected Graph request routinely
    echoes the content it rejected, which here is the owner's mail.
    """
    if response.status_code == SYNC_STATE_GONE_STATUS:
        return True
    if not (400 <= response.status_code < 500):
        return False
    try:
        payload = response.json()
    except ValueError:
        # DOCUMENTED FAIL-OPEN, same rule as ``outlook_errors._error_code``. A
        # non-JSON error body (an HTML proxy page, a truncated response) is not
        # an envelope and has no code to recognise, so this reports "not a
        # resync" and the caller raises the ordinary bounded failure instead.
        #
        # DEBUG, and NO EXCERPT OF THE BODY IS LOGGED -- not even a truncated
        # one. A rejected Graph request routinely echoes the content it
        # rejected, which here is the owner's mail, and a character bound is
        # not redaction. The status is generated by Graph and is safe to name.
        logger.debug(
            f"GRAPH: delta error body is not a JSON envelope, cannot check for a "
            f"refused delta token status={response.status_code}"
        )
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    return isinstance(code, str) and code.casefold() == SYNC_STATE_GONE_CODE.casefold()


RESUME_HEADER: Final = "Location"
"""The header Graph MAY set on a refused delta token, naming where to resume.

THE ONE PLACE THIS NAME APPEARS. It is optional: Graph documents the header on
a ``410`` for the delta feed but guarantees nothing, so its absence is the
ordinary case and costs the cold start it always cost. When it IS present,
discarding it buys a full folder walk for nothing -- and a 410 is expected
operation on a perfectly healthy mailbox, so that walk is paid over and over.
"""


def _resume_url(response: httpx.Response) -> str | None:
    """The absolute url to replay after a refused token, or ``None``.

    OPAQUE, exactly like ``@odata.nextLink`` and ``@odata.deltaLink``: whatever
    Graph put in the header is issued verbatim as the next request url, never
    parsed, compared, normalised, or rebuilt, and never handed query parameters
    of ours. Graph encodes the folder and its own position into it in a form it
    is free to change, and reassembling one would re-encode the percent escapes
    it issued -- inventing a position the service never handed out.

    A blank or whitespace-only value answers ``None`` rather than being
    replayed. httpx resolves an empty url against the client's base url, so
    replaying one would issue a GET against the Graph ROOT dressed as a
    position Graph named. Absent and unusable get the same answer: cold start.
    """
    location: str | None = response.headers.get(RESUME_HEADER)
    if location is None or not location.strip():
        return None
    return location


def _is_arrival(item: dict[str, Any]) -> bool:
    """True when this delta record is a message the watcher may act on.

    Two kinds are dropped, and both are dropped HERE -- before projection --
    because a record that is never going to be acted on should not be turned
    into a ``Message`` first, and because a draft that reaches the watcher gets
    classified, filed, and receipted as if the owner had received it.

    Items carrying an ``"@removed"`` key are SKIPPED. Deletions are read-side
    noise for a watcher whose entire output is a label and a move: it acts on
    messages that ARRIVED, and it never removes anything, so a record about a
    removal has nothing for it to do. This is the same rationale
    ``gmail_history.HISTORY_TYPES`` applies on the other provider -- Gmail can
    filter server-side, Graph cannot, so the filter lands here instead.

    Items with ``isDraft`` true are SKIPPED for a stronger reason than noise.
    Graph's delta reports the owner's OWN half-typed replies, saved
    automatically as they type, and every keystroke-driven save is another
    delta record. Filing one moves the message the owner is still writing out
    of the folder their mail client has open. It is not incoming mail, the
    owner is not waiting on a triage decision about it, and the connector's
    single write would actively interfere with them.

    A missing ``isDraft`` reads as FALSE, which is the same reading an absent
    key has had here since the first round: ``$select`` names the field
    (``DELTA_SELECT``), so its absence means Graph did not mark it, not that
    the answer is unknown.
    """
    return "@removed" not in item and not item.get("isDraft")


def _added_messages(page: dict[str, Any], to_message: _ToMessage) -> tuple[Message, ...]:
    """The ARRIVALS on one delta page, as header projections.

    Which records count as arrivals is ``_is_arrival``'s decision; this
    function owns the projection and the DEBUG count of what the filter
    dropped.

    A COUNT, never the items themselves -- same rule ``ChangeSet`` states for
    ``skipped_missing``. A draft record carries the owner's unsent prose in its
    subject and preview, so naming even one of them in a log line would put
    mailbox content in a file the connector writes to disk. The count is
    generated by us and is safe.

    Every kept item goes through ``parse_graph``, the shape boundary, so a
    response Graph changed the shape of is one refusal in one place rather than
    an ``AttributeError`` further down.
    """
    items = list(page.get("value", []) or [])
    drafts = sum(1 for item in items if "@removed" not in item and item.get("isDraft"))
    if drafts:
        logger.debug(
            f"GRAPH: delta drafts skipped before projection, the owner is still "
            f"writing them skipped={drafts}"
        )
    return tuple(
        to_message(
            parse_graph(GraphMessage, item, operation="poll_changes"),
            internet_message_id=item.get("internetMessageId") or "",
        )
        for item in items
        if _is_arrival(item)
    )


def poll_changes(
    http: Callable[[], httpx.Client],
    to_message: _ToMessage,
    cursor: str | None,
    page_token: str | None = None,
    container_id: str | None = None,
) -> ChangeSet:
    """ONE page of ONE folder's delta stream. READ ONLY.

    ``OutlookProvider.poll_changes`` is the interface method and delegates
    straight to this: the provider class states the contract and this module
    owns every mechanic behind it, so the change feed is one file a reviewer
    reads end to end rather than a method in ``outlook.py`` calling helpers
    here.

    One page, not the whole round: a function that looped internally would have
    to decide when to stop, and the page budget belongs to the loop that owns
    the round (``base.py``:487). When Graph reports another page, this returns
    it as ``next_page_token`` and NO cursor.

    The three inputs select the url and nothing else does. ``page_token``
    continues the CURRENT round, ``cursor`` starts the next one from a stored
    position, and neither is a cold start -- which is not a degenerate case of
    the same operation so much as the only one with no link to replay.

    This is also the log boundary: one ``ProviderCall`` per round, opened here
    so every path reports under the same operation name and a reader of the log
    sees one round as one story.

    Args:
        http: Returns the authenticated Graph client (the provider's ``_http``).
        to_message: Converts one validated Graph message to a ``Message``.
        cursor: The opaque ``@odata.deltaLink`` to resume from, replayed
            verbatim, or ``None`` for a cold start.
        page_token: The opaque ``@odata.nextLink`` continuing the current
            round, or ``None``. Takes precedence over ``cursor``, because a
            round in progress is finished before a new one begins.
        container_id: Which folder a COLD START walks, or ``None`` for
            ``DEFAULT_CONTAINER``. Read ONLY when there is no link to replay:
            a stored ``deltaLink`` and a ``nextLink`` already encode the folder
            Graph issued them for, so consulting this alongside one would let a
            caller point a stream at a folder it was not opened on.

    Returns:
        A ``ChangeSet``. ``cursor`` is populated only on the final page, and
        never alongside ``next_page_token`` or ``resync_required``.

    Raises:
        ProviderError: The call failed for a reason other than a refused token,
            or the page carried neither continuation link.
    """
    with ProviderCall(PROVIDER_LOG_NAME, "poll_changes", has_cursor=cursor is not None) as call:
        link = page_token if page_token is not None else cursor
        # A COLD START is the only round that BUILDS a url, and therefore the
        # only one that may name a container or a projection. Graph puts both
        # inside the links it hands back, so a replayed link is issued exactly
        # as received -- adding parameters to one would be editing a position
        # the service created.
        url = (
            link
            if link is not None
            else DELTA_PATH_TEMPLATE.format(container_id=container_id or DEFAULT_CONTAINER)
        )
        params = None if link is not None else {"$select": DELTA_SELECT}
        response = _delta_page(call, http, url, params=params)
        if _is_sync_state_gone(response):
            # Expected operation, not a failure: the stored token has been
            # evicted. Graph MAY name where to resume in a ``Location`` header,
            # and when it does that link is replayed VERBATIM as this round's
            # request -- the same rule the ``@odata`` links follow -- so the
            # refusal costs one extra GET instead of a full folder walk. With
            # no header there is no position to replay and the caller drops the
            # cursor and cold starts. Logged at the call's own level rather
            # than raised, because a raise here would fire on a perfectly
            # healthy quiet mailbox.
            resume = _resume_url(response)
            if resume is None:
                # DEBUG, and the HOST only: the url carries Graph's delta token
                # in its query, which is a resume position for the owner's
                # mailbox and does not belong in a file on disk.
                logger.debug(
                    f"GRAPH: delta token refused with no resume position offered, "
                    f"a cold start follows host={response.request.url.host}"
                )
                call.fields["resync_required"] = True
                return ChangeSet(resync_required=True)
            logger.debug(
                f"GRAPH: delta token refused, replaying the resume position Graph "
                f"named host={httpx.URL(resume).host}"
            )
            call.fields["resumed"] = True
            response = _delta_page(call, http, resume)
        if not response.is_success:
            # TYPED, not bare, and for one reason: the reauth wrap one module
            # up reads ``.status`` to tell a stale token from any other
            # refusal. The message is byte-identical to the bare form and
            # ``GraphStatusError`` subclasses ``ProviderError``, so every
            # existing handler catches exactly what it caught before.
            raise GraphStatusError(str(_provider_error(response, "GET", url)), response.status_code)
        page = dict(response.json())
        messages = _added_messages(page, to_message)
        call.fields["returned"] = len(messages)
        next_link = page.get("@odata.nextLink")
        # THE CURSOR RULE (WATCHER-SPEC.md 2.4): a page that is not the last
        # one yields NO storable cursor. `ChangeSet` enforces this too; passing
        # only the token here is what makes the rule readable at the site that
        # decides. Storing a cursor mid-page marks a partial read as a
        # completed one and loses every change on the unfetched pages, with no
        # error and no log line -- the one silent-data-loss bug in the design.
        if next_link is not None:
            return ChangeSet(messages=messages, next_page_token=str(next_link))
        delta_link = page.get("@odata.deltaLink")
        if delta_link is None:
            raise ProviderError(
                "Graph delta page carried neither @odata.nextLink nor @odata.deltaLink, "
                "so this round has no resume position and cannot be stored. ACTION "
                "REQUIRED: re-run the watcher; a cold start will rebuild the stream."
            )
        return ChangeSet(messages=messages, cursor=str(delta_link))
