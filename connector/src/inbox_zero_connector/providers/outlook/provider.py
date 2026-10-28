"""Microsoft Graph adapter -- read, move, flag. Stage 1.

Scopes come from ``scopes.GRAPH_SCOPES`` and are never constructed here:
{Mail.ReadWrite}. Microsoft's permissions reference states the exclusion in
the permission's own description, verbatim: "Allows the app to create, read,
update, and delete email in user mailboxes. Does not include permission to
send mail." Both send paths (POST /me/sendMail, POST /me/messages/{id}/send)
require Mail.Send with higher-privileged alternatives listed as "Not
available", so the token this adapter holds cannot send.

Honest framing, carried into the README and the buyer copy:

- Mail.ReadWrite is WHOLE-MAILBOX read/write. Graph has no folder-scoped OAuth
  permission. The defensible claim is "cannot send", not "narrowly scoped".
- Delegated permissions support DYNAMIC consent, so omitting Mail.Send from
  the app registration is not by itself an unbypassable barrier. The token
  THIS connector holds cannot send; a different token could. Tenant-side
  hardening (Entra consent policy, Exchange ApplicationAccessPolicy) is what
  makes it structural, and it is an onboarding step.

Stage 2 adds reply drafts and sent-mail sampling, and BOTH stay inside
Mail.ReadWrite. The draft is created with Graph's reply-draft action, which
Microsoft documents under Mail.ReadWrite and which addresses the reply
SERVER-SIDE from the original message. That is why the Graph draft path never
handles a recipient list: Microsoft builds it, we only supply the body. The
scope posture is UNCHANGED by Stage 2 on this provider -- still scope-layer,
still no Mail.Send anywhere.
https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0

Only these endpoints are ever called: GET /me/messages, GET /me/messages/{id},
GET /me/mailFolders, POST /me/mailFolders, POST /me/messages/{id}/move,
POST /me/messages/{id}/<reply-draft action>, PATCH /me/messages/{id}. Note
that /move relocates a message and the reply-draft action creates a draft;
Graph's DELETE verb, /sendMail, and the per-message send action are never
issued by this package.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from inbox_zero_connector.config import folder_prefix as config_folder_prefix
from inbox_zero_connector.providers import granted
from inbox_zero_connector.providers.base import (
    AuthError,
    ChangeSet,
    Container,
    DraftResult,
    MailProvider,
    Message,
    ProviderError,
    SentMessage,
    SentSelection,
    Thread,
)
from inbox_zero_connector.providers.containers import (
    resolve_graph_well_known,
    walk_graph_folders,
)
from inbox_zero_connector.providers.ids import validate_id
from inbox_zero_connector.providers.limits import MAX_PAGE_SIZE, cap_page
from inbox_zero_connector.providers.outlook import consent, self_draft
from inbox_zero_connector.providers.outlook import delta as outlook_delta
from inbox_zero_connector.providers.outlook.categories import (
    CATEGORIES_FIELD,
    GraphStatusError,
    apply_category,
)
from inbox_zero_connector.providers.outlook.errors import _provider_error, _raise_if_retryable
from inbox_zero_connector.providers.outlook.evidence import (
    ResolvedId,
    caller_headers,
    parse_date_bound,
    resolved_or_none,
)
from inbox_zero_connector.providers.outlook.models import (
    GraphFolder,
    GraphFolderList,
    GraphMessage,
    GraphMessageList,
    parse_graph,
)
from inbox_zero_connector.providers.outlook.reauth import (
    PROVIDER_KEY,
    account_name,
    graph_client,
    persist_credential,
    restore_credential,
    with_reauth_once,
)
from inbox_zero_connector.providers.retry import MAX_ATTEMPTS, with_retry
from inbox_zero_connector.services.auth.scopes import GRAPH_SCOPES
from inbox_zero_connector.services.triage.taxonomy import validate_label_name
from inbox_zero_connector.utils.logging_config import ProviderCall

PROVIDER = PROVIDER_KEY
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LEGACY_FOLDER_PREFIX = "InboxZero-"
"""What this adapter used to hardcode, kept only so the value has a name.

No longer applied. The prefix is now ``config.folder_prefix`` and defaults to
empty, so ``Reference`` creates a folder called ``Reference``. An operator who
wants the old separation sets the setting to this string and gets the previous
display names back byte-for-byte. Nothing in this module reads the environment
to find it -- see ``OutlookProvider.__init__``.
"""

_TIMEOUT = httpx.Timeout(30.0)

_HEADER_FIELDS = "id,conversationId,internetMessageId,from,subject,receivedDateTime,bodyPreview,isRead,importance,flag,parentFolderId"
"""Header-only projection for list_inbox -- no body, so a sweep stays cheap.

``parentFolderId`` is the message's ``containers`` entry. Graph returns only
the fields ``$select`` names, so omitting it here would leave every message
reporting no placement at all rather than the one folder it is actually in.
It is an id, not a body, so it does not cost the sweep its cheapness.
"""

_THREAD_FIELDS = (
    "id,conversationId,from,subject,receivedDateTime,bodyPreview,isRead,importance,body,"
    "toRecipients,ccRecipients,categories,parentFolderId"
)
"""get_thread additionally pulls the body, which is untrusted input.

It also pulls the recipient lists and categories, and both are load-bearing
rather than incidental: the recipients become the ONLY source of a draft's
addressing, and the categories feed the deterministic sales-voice rule. Both
come from Graph's view of the thread, so neither can be supplied by a caller.
"""

_SENT_FIELDS = (
    "id,conversationId,subject,sentDateTime,body,toRecipients,ccRecipients,categories,"
    "parentFolderId"
)
"""read_sent_sample projection. Bodies included -- that is the point of it.

``parentFolderId`` is what PROVES a message is sent mail on the explicit-id
path, where the URL is mailbox-wide rather than scoped to sentitems. Graph
returns only the fields ``$select`` names, so dropping it here would make every
message look unparented and refuse the whole path -- the field and the check in
``_sent_items_by_id`` are one change, not two.
"""

_SENT_FOLDER_CACHE_KEY = "\x00sentitems"
"""Cache key for the resolved sentitems folder id.

Prefixed with a NUL so it cannot collide with a taxonomy folder name. That
guard USED to be redundant, because every taxonomy key carried an
``InboxZero-`` prefix; now the prefix defaults to empty and the keys are bare
folder names, so the NUL is the only thing keeping the two apart. Sharing the
map keeps one cache to reason about instead of two that can disagree about
their lifetime.
"""

_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9_\-=.]+")
"""The full grammar of a Graph message id: URL-safe base64 plus '=' and '.'.

Deliberately an ALLOWLIST, not a blocklist of dangerous characters. A
blocklist has to anticipate every way to leave a path segment -- '/', '..',
'#', '?', CRLF, percent-encoded forms of each, and whatever the next parser
disagreement turns out to be. The allowlist admits only what a real id
contains, so an unanticipated metacharacter fails by DEFAULT instead of
needing to have been foreseen.
"""


_REPLY_DRAFT_ACTION = "createReply"
"""Graph's reply-draft action. Creates a DRAFT; it does not send anything.

Held as a named constant rather than inlined so that the one place this
package builds a draft URL is greppable, and so the endpoint-literal scanners
(tests/test_security_surface.py and _graph/done-means/inbox-zero-no-send.sh)
have a single reviewable definition to point at instead of a path fragment
buried in an f-string.
"""

_STATIC_SEGMENTS = frozenset({
    "me",
    "messages",
    "mailFolders",
    "childFolders",
    "inbox",
    "junkemail",
    "deleteditems",
    "archive",
    "sentitems",
    "move",
})
"""Path words this adapter is allowed to write into a URL literally.

Everything a Graph URL in this package is made of is either one of these fixed
words, the reply-draft action constant, or a DYNAMIC segment that goes through
``_safe_message_id``. Holding the fixed vocabulary as data is what lets
``_graph_path`` treat "not in this set" as "untrusted, validate it" rather than
relying on each caller to say which of its arguments were attacker-supplied --
the round-1 mistake, where the guard was correct but opt-in.
"""


def _graph_path(segments: tuple[str, ...]) -> str:
    """Assemble a request path, validating every segment that is not fixed.

    THIS IS THE BOUNDARY. It is the only place in this module that produces a
    Graph URL, and it is reached only through ``_get``/``_post``/``_patch``,
    whose signatures take segments rather than a formatted string. A caller
    therefore cannot hand the HTTP layer a path it built itself, so a future
    method CANNOT forget to validate an id -- there is no code path that skips
    this function.

    That structural placement is the fix. Validating at each call site was
    tried first and failed exactly as opt-in guards do: ``_sent_items`` was
    added later, interpolated ``selection.message_ids`` straight into a URL,
    and reproduced the original defect on a path nobody remembered to cover.

    Args:
        segments: Path words in order. Any segment outside
            ``_STATIC_SEGMENTS`` and the reply-draft action is treated as
            untrusted and must satisfy the Graph id grammar.

    Returns:
        The absolute request path, each dynamic segment percent-encoded.

    Raises:
        ProviderError: If a dynamic segment fails ``_safe_message_id``.
    """
    parts = [
        segment
        if segment in _STATIC_SEGMENTS or segment == _REPLY_DRAFT_ACTION
        else _safe_message_id(segment)
        for segment in segments
    ]
    return "/" + "/".join(parts)


def _safe_message_id(message_id: str) -> str:
    """Validate an untrusted message id, then encode it as ONE path segment.

    ``message_id`` arrives from an MCP tool parameter, which means it is
    attacker-controlled whenever the host AI is reading attacker-controlled
    mail. Interpolated raw it can leave its segment and re-aim the request:
    ``m1/../../me/sendMail`` normalizes to ``/me/me/sendMail/move``, which is
    how a connector that structurally "cannot send" grows a path toward a send
    action.

    Both halves are load-bearing and neither is redundant:

    - The regex REFUSES anything outside the id grammar, before any HTTP, so a
      crafted id costs zero requests and cannot create a folder as a side
      effect of the caller's own rejection.
    - ``quote(safe="")`` then encodes what remains, so even a legitimate id's
      ``=`` padding cannot be re-read as structure by an intermediary that
      disagrees with us about what a path separator is.

    Called from ``_graph_path`` and nowhere else. Public methods do NOT call it
    directly any more: a guard a method has to remember is a guard the next
    method will not have.

    Args:
        message_id: The provider's own id for a message, from a tool call.

    Returns:
        The id, percent-encoded for use as a single URL path segment.

    Raises:
        ProviderError: If the id is empty, over-long, or contains anything
            outside the Graph id grammar.
    """
    return quote(
        validate_id(
            message_id,
            field="message_id",
            pattern=_MESSAGE_ID_RE,
            kind="Microsoft Graph message",
            hint="pass an id exactly as list_inbox returned it, unmodified.",
        ),
        safe="",
    )


def _safe_date_bound(bound: str) -> str:
    """Validate a caller's date bound before it reaches a ``$filter``.

    ``after`` and ``before`` are MCP tool parameters that ``server.py`` only
    strips, so "it is an ISO date" was an assumption rather than a guarantee.
    Interpolated raw, ``2026-01-01 or id ne null`` widens the sent-mail filter
    back into the whole-folder read that ``read_sent_sample`` exists to refuse.

    A date is not escapable the way a string literal is -- OData dates are
    UNQUOTED, so there is no quote to double and no escaping that would make a
    malformed bound inert. The check IS the whole defence here, which is why it
    PARSES rather than shape-matching: the predecessor's ``\\d{4}-\\d{2}-\\d{2}``
    accepted ``2026-02-30`` and ``9999-99-99``, which are not dates, and sent
    them to Graph to fail as an opaque HTTP 400 -- this module withholds
    upstream error text on purpose, so the caller could not learn why.

    Delegates to ``graph_evidence.parse_date_bound``, which re-emits the value
    from the parsed object. Kept as a named wrapper here because this is the
    call site's boundary and the filter scan reads this module.

    Args:
        bound: The caller's date bound. Non-empty; callers skip empties.

    Returns:
        A real calendar date as ``YYYY-MM-DD``, generated from the parse, safe
        to place unquoted in a filter.

    Raises:
        ProviderError: If the bound is not a real calendar date.
    """
    return parse_date_bound(bound)


def _odata_string_literal(value: str) -> str:
    """Render ``value`` as an OData string literal, quote-doubling included.

    OData escapes a single quote inside a string literal by DOUBLING it, so
    ``x' or true`` becomes ``'x'' or true'`` -- one literal that matches
    nothing, instead of a closed quote followed by an attacker's clause.

    This is the second half of the filter boundary and is deliberately not
    load-bearing on its own today: ``_safe_thread_id`` already refuses a quote,
    so nothing reaching here can contain one. Both halves exist for the same
    reason they do on the path boundary -- the allowlist decides WHETHER a
    value may travel, the escaper decides that it cannot be re-read as
    structure once it does. If a later change widens the id grammar, this is
    what still stands between a caller and an OR clause.
    """
    return "'" + value.replace("'", "''") + "'"


def _odata_filter(field: str, value: str) -> str:
    """Build one ``<field> eq '<value>'`` clause. THE FILTER BOUNDARY.

    This is to ``$filter`` what ``_graph_path`` is to the URL: the only place
    in this module that produces a filter value from untrusted input, so a
    future method cannot reintroduce OData injection by writing one more
    f-string. ``tests/test_graph_provider.py`` scans the AST and FAILS on any
    interpolation that builds filter-shaped text outside this function, which
    is what makes that a structural property rather than a convention.

    Round 3 made path segments safe and left this class open on two methods.
    The lesson it repeats: a fix that patches the call sites is the wrong fix,
    because the next call site has not been written yet.

    Args:
        field: A FIXED field name from this module. Never caller-supplied.
        value: Untrusted. Validated against the id grammar, then escaped.

    Returns:
        A single OData equality clause, safe to place in ``$filter``.

    Raises:
        ProviderError: If ``value`` fails ``_safe_thread_id``.
    """
    return f"{field} eq {_odata_string_literal(_safe_thread_id(value))}"


def _safe_thread_id(thread_id: str) -> str:
    """Validate an untrusted conversation id before it reaches a ``$filter``.

    A Graph ``conversationId`` shares the message id's grammar -- URL-safe
    base64 plus ``=`` and ``.`` -- so it is checked against the SAME allowlist
    rather than a second, looser one. Reusing the grammar is the point: two
    validators drift, and the looser one becomes the way in.

    Unvalidated, this value closed the adapter's own quote and appended a
    clause: ``x' or id ne null or conversationId eq '`` turned ``get_thread``
    into a whole-mailbox read whose projection includes ``body``, and turned
    ``create_draft`` into a reply anchored on someone else's conversation.

    Args:
        thread_id: The provider's own conversation id, from a tool call.

    Returns:
        The id, unchanged. The caller escapes it for its context.

    Raises:
        ProviderError: If the id is empty, over-long, or outside the grammar.
    """
    return validate_id(
        thread_id,
        field="thread_id",
        pattern=_MESSAGE_ID_RE,
        kind="Microsoft Graph conversation",
        hint="pass a thread_id exactly as list_inbox returned it, unmodified.",
    )


class FiledMessage(str):
    """The applied folder name, carrying the POST-MOVE message id alongside it.

    WHY A ``str`` SUBCLASS AND NOT A DATACLASS
        ``MailProvider.file_message`` is declared to return ``str`` and the
        tool layer puts that return straight into the ``applied_as`` field of
        its response. A dataclass would be the tidier shape, but it changes
        the base contract and every caller in one move; a ``str`` subclass
        keeps every existing reader byte-identical -- ``str(result)``,
        equality, and formatting all still produce the folder name -- while
        making the new id reachable to a caller that wants it.

        The narrow fix is the point. Graph assigns a NEW id when a message
        moves, so the ledger was recording an id that no longer resolves, and
        correcting the RECEIPT should not require rewriting the interface the
        receipt travels through.

    Attributes:
        message_id: The message's id in its DESTINATION folder, or ``None``
            when Graph's ``/move`` response carried no id to report.

            ``None`` MEANS UNKNOWN, AND UNKNOWN IS NOT THE OLD ID. This used
            to fall back to the pre-move id on the reasoning that a stale id
            beats an empty one. It does not: Graph's ``/move`` is
            copy-then-delete, so the pre-move id is DEAD the instant the move
            lands, and a receipt carrying it is not a weaker record but a
            WRONG one -- it names a message that no longer exists while
            looking exactly like a row that names one that does. ``reset
            restore`` cannot tell the two apart, so it resolves the row to
            nothing and reports a gap it cannot explain. ``None`` is
            checkable; a dead id is not.

        destination_id: The CONTAINER id the move filed the message into, or
            ``None`` when the caller did not report one.

            THE FOLDER NAME CANNOT STAND IN FOR IT (#173 round 3). This value
            is the string ``file_message`` itself carries, which is
            ``_folder_name(folder)`` -- prefix included -- while the caller
            that needs the destination holds the RAW taxonomy folder the
            classification named. With any non-empty ``folder_prefix`` (a real
            install ships ``rTech/``) those two strings never match, so a
            watcher resolving the destination by name resolved nothing and
            marked the moved id in the SOURCE folder's replay window; the
            destination stream then met the message as new and refiled it,
            forever. Graph also permits the same displayName under two
            parents, so even an unprefixed name match can land on the wrong
            container. The id is the answer to both, and the adapter already
            has it: it is what it POSTed as ``destinationId``.
    """

    message_id: str | None
    destination_id: str | None

    def __new__(
        cls, filed_as: str, *, message_id: str | None, destination_id: str | None = None
    ) -> FiledMessage:
        """Build the folder-name string, then attach the post-move id to it.

        Construction happens in ``__new__`` rather than ``__init__`` because
        ``str`` is immutable: its value is fixed at allocation, so ``filed_as``
        has to reach ``str.__new__`` and cannot be assigned afterwards. The id
        is keyword-only so no caller can pass the two strings in the wrong
        order -- they are both plain strings and the mistake would be silent.

        Args:
            filed_as: The applied folder name. This IS the string value.
            message_id: The message's id in its destination folder, or
                ``None`` when the move response reported none.
            destination_id: The container id the move filed into. Defaults to
                ``None`` so every existing construction site stays valid; a
                caller that knows the id passes it.
        """
        self = super().__new__(cls, filed_as)
        self.message_id = message_id
        self.destination_id = destination_id
        return self

    def __getnewargs_ex__(self) -> tuple[tuple[str], dict[str, str | None]]:
        """Tell copy and pickle how to rebuild this, kw-only argument included.

        ``__new__`` takes a REQUIRED keyword-only ``message_id``, and the copy
        protocol's default for a ``str`` subclass reconstructs it positionally
        from ``str.__getnewargs__``. That call raises ``TypeError: __new__()
        missing 1 required keyword-only argument``, so ``copy.copy``,
        ``copy.deepcopy``, and ``pickle`` all failed on a value the tool layer
        hands out as a receipt.

        Returning both halves is what makes the id survive: the tuple rebuilds
        the string, the dict re-supplies the keyword. Without the dict a copy
        would either raise as before or silently lose the post-move id, which
        is the failure this class exists to prevent.

        Returns:
            The positional and keyword arguments for ``__new__``.
        """
        return (str(self),), {"message_id": self.message_id, "destination_id": self.destination_id}

    @property
    def filed_as(self) -> str:
        """The applied folder name. Same value the string itself carries."""
        return str(self)

    def __repr__(self) -> str:
        """Show BOTH halves, because the default ``str`` repr hides the id.

        Without this, a debugger, a test failure, and a log line all render this
        as an ordinary quoted folder name, so the post-move id this class exists
        to carry would be invisible exactly where someone is looking for it.
        Neither value is a secret: a folder name and a Graph message id.
        """
        ids = f"message_id={self.message_id!r}, destination_id={self.destination_id!r}"
        return f"FiledMessage({str(self)!r}, {ids})"


class OutlookProvider(MailProvider):
    """Least-privilege Graph adapter. No send, no draft, no delete methods."""

    name = PROVIDER

    def __init__(
        self,
        client: httpx.Client | None = None,
        token: str | None = None,
        folder_prefix: str | None = None,
    ) -> None:
        """Record what was injected. NOTHING is resolved, read, or fetched here.

        Constructing an adapter must not require a credential, a config file, or
        a network: every field below is either a caller's override or a sentinel
        meaning "not determined yet", and each is resolved at first use by the
        method that owns it. That is what lets a test build a provider with a
        mocked transport and no environment at all.

        Args:
            client: A pre-built transport, or None to build one on first use.
            token: A pre-obtained access token, or None to authenticate.
            folder_prefix: The filing-folder prefix, or None to ask the
                keystone at first use. An empty string is a real value and the
                default one, so it cannot share a sentinel with "unset".
        """
        # `client` is injectable so tests use a mocked transport with no
        # network and no credential.
        self._client = client
        self._token = token
        # None means "no token response has been seen", which is NOT the same
        # fact as "the response granted nothing" and must not collapse into
        # it. `grants.discover` reports the first as unreadable (a bug to
        # chase) and the second as an empty grant (possibly a correct, very
        # locked-down account). An injected-token test adapter has never
        # authenticated, so it stays None and honestly reports not-discoverable
        # rather than inheriting whatever this connector happened to request.
        self._granted_scopes: tuple[str, ...] | None = None
        # None means "ask the keystone at first use", not "no prefix" -- an
        # empty string is a legitimate value and the default one, so the two
        # cases cannot share a sentinel. Resolved lazily rather than here
        # because constructing an adapter must not require loaded settings;
        # every test that injects a transport would otherwise need a config
        # file to exist.
        self._folder_prefix = folder_prefix
        # ResolvedId, not str: nothing that failed to resolve can be stored
        # here, so no reader has to defend against an empty id it did not
        # write. The cache holds PROVEN ids or nothing.
        self._folder_cache: dict[str, ResolvedId] = {}

    # -- auth ---------------------------------------------------------------

    def authenticate(self, interactive: bool = False) -> str:
        """MSAL silent-first, falling back to the device-code flow on a terminal.

        The cache is the credential store: msal serialises its own token cache,
        ``persist_credential`` writes that blob to the keychain only when msal
        reports it CHANGED, and the refresh token never travels as a value this
        code handles. ``interactive=False`` refuses rather than prompting, because
        under the MCP server a device-code prompt would hang the host AI's tool
        call with no signal.

        The msal import is function-local: it is a heavy dependency only the
        auth path needs, and importing it at module scope would cost every CLI
        invocation and every test that merely injects a transport.

        The silent path signs in as the account this install STORED, never
        ``get_accounts()[0]`` -- see ``pinned_account`` for why cache order is
        not an identity (#149).

        See providers.base.MailProvider.authenticate for the contract.
        """
        import msal

        with ProviderCall(
            PROVIDER,
            "authenticate",
            interactive=interactive,
            scope_count=len(GRAPH_SCOPES),
        ) as call:
            cache, app, account = restore_credential(msal)
            result: dict[str, Any] | None = (
                app.acquire_token_silent(list(GRAPH_SCOPES), account=account)
                if account is not None
                else None
            )

            if not result:
                call.fallback("silent token unavailable", interactive=interactive)
                result = self._interactive_token(app, interactive)

            if "access_token" not in result:
                detail = result.get("error_description", "no access token returned")
                raise AuthError(f"Microsoft authentication failed: {detail}")

            signed_in_as = account_name(result, account=account)
            persist_credential(cache, signed_in_as)

            # The access token is assigned to an instance attribute and never
            # logged, returned, or interpolated into a message.
            self._token = str(result["access_token"])
            self._granted_scopes = granted.from_msal_result(result)
            return signed_in_as

    def granted_scopes(self) -> tuple[str, ...] | None:
        """What the TENANT granted this credential, or None if not determined.

        Satisfies ``grants.ScopeSource``. The value is read off the msal token
        RESPONSE, never off ``scopes.GRAPH_SCOPES`` -- that constant is this
        connector's ask, and an Entra admin, a Conditional Access policy, or an
        app-consent policy is free to answer it with less. Reporting the ask
        would report our own request back as the tenant's answer.

        Returns raw and fully qualified, exactly as Entra sent it
        (``<resource-uri>/Mail.ReadWrite``). ``grants._normalize`` owns the
        stripping; doing it here would put scope INTERPRETATION in two modules
        and leave the security verdict deriving from a string this adapter had
        already edited.

        Returns:
            The granted permissions, or None when no token response has been
            seen or the response carried no ``scope`` key at all.
        """
        return self._granted_scopes

    def _interactive_token(self, app: Any, interactive: bool) -> dict[str, Any]:
        """Browser (auth-code + PKCE) flow first, device code as the fallback.

        Refusing rather than prompting is the load-bearing part: under the MCP
        server there is no terminal, so a prompt would hang the host AI's tool
        call forever with no signal. The order is not a preference: Entra
        security defaults block device code (AADSTS530035); see ``consent``.
        """
        if not interactive:
            raise AuthError(
                "No usable Microsoft credential. ACTION REQUIRED: run "
                "'uv run inbox-zero-connector auth microsoft' in a terminal first."
            )
        result = consent.browser_token(app)
        if "access_token" in result:
            return result
        return consent.device_code_token(app)

    def _http(self) -> httpx.Client:
        """The transport, built on first use and cached. See ``outlook_reauth``.

        One of two interface methods over the credential lifecycle that module
        owns; ``_reauthenticate`` is the other. Building the client and
        clearing it are the SAME concern -- the access token is baked into the
        client's headers, so whoever builds it decides what a refresh must
        invalidate -- and splitting them across two files is how they drift.
        """
        return graph_client(self, GRAPH_BASE, _TIMEOUT)

    def _get(
        self,
        *segments: str,
        params: dict[str, Any] | None = None,
        operation: str = "get",
        link: str | None = None,
    ) -> dict[str, Any]:
        """One Graph read. Retried, because a repeated read is free.

        Takes path SEGMENTS, not a url. That is the security-relevant part of
        the signature: ``_graph_path`` validates every non-fixed segment, so a
        caller cannot reach httpx with a path it assembled itself and cannot
        forget to validate an id it was handed.

        ``link`` is the one exception, and it is not a hole in that rule: it is
        a server-supplied ``@odata.nextLink`` replayed VERBATIM -- never
        assembled here, never parsed, never edited -- which is the same trust
        rule ``outlook_delta`` already applies to its continuation links. A
        page cursor is Graph's own opaque state, and rewriting it is how a walk
        silently reads the wrong page. Segments-with-validation remains the
        only way to NAME a path locally.

        The five points are emitted HERE rather than in each public method:
        every Graph call funnels through these three helpers, so wrapping them
        covers the whole adapter and no future method can forget.

        The retried operation RAISES on a retryable status rather than
        returning the response. httpx does not raise on 4xx/5xx, so a bare
        ``client.get`` inside the wrapper hands tenacity a 429 as a SUCCESSFUL
        return: the policy in ``retry.py`` never sees a status, and
        ``RETRYABLE_STATUS`` matches nothing. Raising inside the retried
        callable is what puts the status where ``is_retryable`` can read it.

        The 401 refresh wraps OUTSIDE tenacity and INSIDE ``_unwrap``, and both
        halves of that placement are deliberate. Outside, because 401 is not a
        transient status and repeating the identical request with the same
        expired token only reproduces it. Inside, because a 401 does not reach
        ``_raise_if_retryable`` at all -- it arrives as the ``GraphStatusError``
        that ``_unwrap`` raises, so the unwrap has to sit within the retried
        callable for the refresh to ever see it.
        """
        url = link if link is not None else _graph_path(segments)
        with ProviderCall(PROVIDER, operation, method="GET", path=url) as call:

            def _attempt() -> httpx.Response:
                """One GET, raising on a retryable status so tenacity can see it."""
                response = self._http().get(url, params=params)
                _raise_if_retryable(response)
                return response

            def _send() -> dict[str, Any]:
                """The whole retried read, wrapped so a 401 refresh can replay it.

                This is the unit ``with_reauth_once`` re-invokes: it has to
                contain the ``_unwrap`` as well as the retry, because a 401
                arrives as the ``GraphStatusError`` the unwrap raises and would
                otherwise never reach the refresh.
                """
                try:
                    response = with_retry(call, _attempt)
                except httpx.HTTPStatusError as exc:
                    # The attempt budget is spent. The transient status
                    # travelled as an exception only so the retry policy could
                    # read it; the adapter's contract is a bounded
                    # ProviderError, same as any other failed status.
                    raise _provider_error(exc.response, "GET", url) from exc
                return _unwrap(response, "GET", url)

            data = with_reauth_once(self, operation, _send)
            call.fields["items"] = len(data.get("value", []) or [])
            return data

    def _post(
        self, *segments: str, payload: dict[str, Any], operation: str = "post"
    ) -> dict[str, Any]:
        """One Graph write. NOT retried -- see providers/retry.py.

        Segments, not a url, for the same reason as ``_get``.

        A POST that timed out may already have moved the message or created the
        draft; a second attempt would produce a duplicate and a second ledger
        row for one real change.
        """
        url = _graph_path(segments)
        with ProviderCall(PROVIDER, operation, method="POST", path=url):
            # Retried on 401 despite the no-retry rule above, and the two are
            # consistent: that rule refuses to repeat a write whose outcome is
            # UNKNOWN, and a 401 is Graph refusing the request before acting,
            # so nothing was created and there is no duplicate to make.
            return with_reauth_once(
                self, operation, lambda: _unwrap(self._http().post(url, json=payload), "POST", url)
            )

    def _patch(
        self,
        *segments: str,
        payload: dict[str, Any],
        operation: str = "patch",
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One Graph update. NOT retried, same reason as ``_post``.

        ``headers`` carries the per-request headers a caller genuinely needs to
        set, and NOTHING else -- every name in it must be in
        ``outlook_evidence._CALLER_HEADERS`` or the call raises before any
        request is built.
        Omitting it sends exactly what this adapter sent before the parameter
        existed: httpx merges an empty/absent mapping into nothing, so the
        no-headers path is byte-identical to the previous signature's.

        The motivating caller is concurrency control -- ``If-Match`` against a
        message etag, which Graph honours on a message PATCH (verified live
        2026-08-18: a stale etag answers 412 and leaves the resource untouched).
        This helper only makes such a header SENDABLE; it implements no
        conditional-request logic of its own.
        """
        url = _graph_path(segments)
        sendable = caller_headers(headers)
        with ProviderCall(PROVIDER, operation, method="PATCH", path=url):
            # Retried on 401 for the same reason as ``_post``: the request was
            # refused before Graph acted, so the resource is untouched. An
            # ``If-Match`` in ``sendable`` still holds on the retry -- it is
            # re-sent verbatim, so a concurrent writer still earns its 412.
            return with_reauth_once(
                self,
                operation,
                lambda: _unwrap(
                    self._http().patch(url, json=payload, headers=sendable), "PATCH", url
                ),
            )

    # -- read ---------------------------------------------------------------

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[Message]:
        """GET ``me/mailFolders/inbox/messages``, newest first, header fields only.

        ``unread_only`` becomes a server-side ``$filter``; Graph can express it,
        so no page is fetched and discarded. The raw items are zipped back
        alongside the validated ones because ``internetMessageId`` is read off
        the RAW payload -- it is not part of the projected model -- and
        ``strict=True`` turns any misalignment between the two into a raise
        rather than a message carrying another message's id.

        See providers.base.MailProvider.list_inbox for the contract.
        """
        params: dict[str, Any] = {
            "$top": cap_page(limit),
            "$orderby": "receivedDateTime desc",
            "$select": _HEADER_FIELDS,
        }
        params.update({"$filter": "isRead eq false"} if unread_only else {})
        data = self._get(
            "me", "mailFolders", "inbox", "messages", params=params, operation="list_inbox"
        )
        page = parse_graph(GraphMessageList, data, operation="list_inbox")
        return [
            _to_message(item, internet_message_id=raw_item.get("internetMessageId") or "")
            for item, raw_item in zip(page.value, data.get("value", ()), strict=True)
        ]

    def _conversation(self, thread_id: str, *, operation: str) -> list[GraphMessage]:
        """Every message in one conversation, NEWEST FIRST.

        THE ONE PLACE a conversation is fetched. It exists because there were
        two, and they drifted: this query and ``create_draft``'s were copies of
        each other until one grew an ``$orderby`` and started failing against
        real Graph while the other kept working. A shared helper is the fix for
        that class of defect; patching the query in place would have left the
        second copy waiting to drift again.

        ORDERING IS DONE HERE, IN PYTHON, NOT IN THE QUERY. Graph rejects
        ``$filter`` on ``conversationId`` combined with ``$orderby`` on
        ``receivedDateTime`` -- it answers HTTP 400 ``InefficientFilter``,
        because the two together need an index it will not build. Sorting the
        returned page is exact for a conversation (a thread is small and
        arrives whole) and costs one comparison per message, so the sort moves
        to our side of the wire and the query stays legal.

        Verified live against Microsoft Graph on 2026-08-18, which is the only
        way this was going to surface: a stub answers whatever it is told to,
        and had been answering 200 to a query the real service refuses.
        """
        params = {
            "$filter": _odata_filter("conversationId", thread_id),
            "$select": _THREAD_FIELDS,
        }
        data = self._get("me", "messages", params=params, operation=operation)
        items = list(parse_graph(GraphMessageList, data, operation=operation).value)
        # reverse=True puts newest first, matching what Reply would act on.
        # `or ""` keeps a message with no received stamp sortable instead of
        # raising midway through a comparison -- it sorts oldest, which is the
        # safe end: it can never silently become the anchor a reply is built on.
        return sorted(items, key=lambda item: item.received or "", reverse=True)

    def get_thread(self, thread_id: str) -> Thread:
        """A Graph ``conversationId`` filter, not a thread resource -- Graph has none.

        ``_conversation`` owns the query and its newest-first ordering, so the
        subject is taken from ``messages[0]``: the most recent message's, which
        is what a reader of the thread sees at the top. An empty conversation
        yields an empty subject rather than an IndexError, because a
        conversation that has been emptied is not an error to raise on.

        See providers.base.MailProvider.get_thread for the contract.
        """
        items = self._conversation(thread_id, operation="get_thread")
        messages = [_to_message(item) for item in items]
        bodies = [item.body.content for item in items]
        subject = messages[0].subject if messages else ""
        return Thread(
            id=thread_id,
            subject=subject,
            messages=messages,
            bodies=bodies,
            participants=_participants(items),
            labels=_categories(items),
        )

    def list_containers(self) -> list[Container]:
        """Every mail folder in the mailbox, root-level and nested. READ ONLY.

        Graph folders are EXCLUSIVE: a message sits in exactly one, so every
        container returned here carries ``exclusive=True``.

        The traversal and the well-known resolution both live in
        ``containers``; this method supplies the validated fetch and nothing
        else, so URL construction stays behind ``_graph_path`` while the
        provider-shape rules stay testable without a transport.

        Reads only. Unlike ``_folder_id``, nothing here creates a folder.
        """
        well_known = resolve_graph_well_known(self._container_fetch)
        return walk_graph_folders(self._container_fetch, well_known)

    def _container_fetch(self, *segments: str, operation: str, link: str = "") -> dict[str, Any]:
        """The bounded GET ``containers`` walks with. Page size is OURS, not the caller's.

        ``link`` replays a server-supplied ``@odata.nextLink`` VERBATIM and
        therefore sends NO ``$top``: Graph already encoded its own page size
        into the continuation, and re-adding the parameter edits a cursor the
        service issued. That is the same rule ``_folder_id`` follows when it
        pages the folder listing. Empty string, not ``None``, so the whole
        thing stays one expression at a file already on its size ceiling.
        """
        return self._get(
            *segments,
            params=None if link else {"$top": MAX_PAGE_SIZE},
            operation=operation,
            link=link or None,
        )

    def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
        """Sent mail from the owner's selection only. READ ONLY.

        The selection becomes a Graph ``$filter``. Graph has no label model, so
        ``labels`` map to CATEGORIES and are applied client-side after the
        fetch -- Graph's ``categories`` collection is not reliably filterable
        with the other clauses in one expression. That is a performance
        difference, not a security one: the date bound and the id list still
        go server-side, so the read is bounded before it leaves Microsoft, and
        exclusions are applied to whatever comes back either way.
        """
        if selection.is_empty():
            raise ProviderError(
                "read_sent_sample requires an explicit selection. Refusing to read "
                "the whole sent folder. ACTION REQUIRED: pass labels, a date range, "
                "or message_ids."
            )

        items = self._sent_items(selection)
        wanted = {label.lower() for label in selection.labels}
        unwanted = {label.lower() for label in selection.exclude_labels}
        out: list[SentMessage] = []
        for item in items:
            categories = {category.lower() for category in item.categories}
            # Exclusions win over inclusions -- see SentSelection.
            if unwanted and categories & unwanted:
                continue
            if wanted and not (categories & wanted):
                continue
            out.append(_to_sent(item))
        return out[: selection.limit]

    def _sent_items(self, selection: SentSelection) -> list[GraphMessage]:
        """Fetch the raw sent messages the selection names. Narrowing only."""
        if selection.message_ids:
            return self._sent_items_by_id(selection)
        clauses = [
            f"sentDateTime {operator} {_safe_date_bound(bound)}T00:00:00Z"
            for operator, bound in (("ge", selection.after), ("lt", selection.before))
            if bound
        ]
        params: dict[str, Any] = {
            "$top": cap_page(selection.limit),
            "$orderby": "sentDateTime desc",
            "$select": _SENT_FIELDS,
        }
        if clauses:
            params["$filter"] = " and ".join(clauses)
        data = self._get(
            "me",
            "mailFolders",
            "sentitems",
            "messages",
            params=params,
            operation="read_sent_sample",
        )
        return list(parse_graph(GraphMessageList, data, operation="read_sent_sample").value)

    def _sent_items_by_id(self, selection: SentSelection) -> list[GraphMessage]:
        """Fetch explicitly named messages, PROVING each one is sent mail.

        ``GET /me/messages/{id}`` is MAILBOX-WIDE: it resolves any message the
        token can see, in any folder. The collection endpoint the other branch
        uses is scoped to sentitems by its path, so it carries the sent-only
        contract structurally; this branch does not, and assuming otherwise is
        what let an id from ``list_inbox`` return an inbox message's full body
        from a tool whose contract says SENT MAIL ONLY.

        So the contract is enforced in code here: every fetched message's
        ``parentFolderId`` must equal the mailbox's real sentitems folder id.
        A message that is not in that folder -- or that arrives without the
        field, which is the same thing evidentially -- is REFUSED, never
        skipped. A silent skip returns a short list with no signal, and the
        caller reads "nothing there" instead of "you asked for the wrong
        mail"; a voice profile built from that sample is wrong in a way nobody
        can see.

        Ordering, not a second validator: every id is run through the path
        boundary BEFORE the folder lookup, because otherwise a crafted id would
        cost a request -- the folder GET -- as a side effect of the adapter's
        own refusal. Same shape as ``file_message``, and the check itself still
        lives at the boundary rather than here.
        """
        wanted = selection.message_ids[: selection.limit]
        for msg_id in wanted:
            _graph_path(("me", "messages", msg_id))
        sent_folder_id = self._sent_folder_id()
        items: list[GraphMessage] = []
        for msg_id in wanted:
            data = self._get(
                "me",
                "messages",
                msg_id,
                params={"$select": _SENT_FIELDS},
                operation="read_sent_message",
            )
            item = parse_graph(GraphMessage, data, operation="read_sent_message")
            parent = resolved_or_none(item.parent_folder_id, what="parent folder id")
            # `parent` is None when Graph returned no usable parentFolderId, and
            # None equals no ResolvedId, so a message arriving WITHOUT the field
            # refuses here regardless of what the folder id turned out to be.
            # That is the whole point of the two types: there is no pair of
            # values on this line that can be equal by both being absent.
            if parent != sent_folder_id:
                # The offending id is NOT echoed: it came from a tool call and
                # this text reaches a log line and the host AI's context.
                raise ProviderError(
                    "read_sent_sample was given a message that is not in the sent "
                    "items folder, and refused the whole selection rather than "
                    "returning mail it does not promise. This tool reads SENT MAIL "
                    "ONLY. ACTION REQUIRED: pass ids of messages you sent, or use "
                    "labels/date bounds to select from sent mail instead."
                )
            items.append(item)
        return items

    def _sent_folder_id(self) -> ResolvedId:
        """The mailbox's real sentitems folder id, resolved once and cached.

        ``sentitems`` is a WELL-KNOWN NAME in a URL, not the id that appears in
        a message's ``parentFolderId``, so proving membership needs the real
        id. Cached in the same map as the taxonomy folders: the check runs per
        message, and a folder lookup per message would turn one selection into
        a burst of identical requests.

        ONLY A SUCCESS IS CACHED, and that is a fix, not an optimisation. The
        write used to be unconditional while the read guard was "key not in
        cache", so an unresolvable folder stored ``""`` and every subsequent
        sent read in the process reused it without ever re-asking Graph -- one
        bad response poisoned the mailbox for the process lifetime. Now the
        ``ResolvedId`` construction raises BEFORE the assignment, so a failed
        resolution leaves the cache untouched and the next call retries.

        Returns:
            The folder id, proven non-empty by its type.

        Raises:
            ProviderError: If Graph returns no usable id for sentitems. There
                is no fallback: without the id nothing can be proven to be sent
                mail, and the only safe answer to absent evidence is to refuse.
        """
        cached = self._folder_cache.get(_SENT_FOLDER_CACHE_KEY)
        if cached is not None:
            return cached
        data = self._get("me", "mailFolders", "sentitems", operation="resolve_sent_folder")
        folder = parse_graph(GraphFolder, data, operation="resolve_sent_folder")
        resolved = ResolvedId(folder.id, what="sent items folder id")
        self._folder_cache[_SENT_FOLDER_CACHE_KEY] = resolved
        return resolved

    # -- mutate (reversible only) -------------------------------------------

    def _folder_name(self, folder: str) -> str:
        """Render a taxonomy folder into the display name Graph will carry.

        ONE place, called by both ``_folder_id`` and ``file_message``, because
        those two strings have to be the same string: the first decides where
        the message goes and the second is the receipt the ledger records. Two
        concatenations would be two chances for a receipt to name a folder the
        move did not use.
        """
        if self._folder_prefix is None:
            self._folder_prefix = config_folder_prefix()
        return f"{self._folder_prefix}{folder}"

    def _folder_id(self, folder: str) -> ResolvedId:
        """Resolve (creating once if needed) the mail folder for a taxonomy name.

        The caller has already validated ``folder`` against the taxonomy, so
        this cannot mint arbitrary folders on an attacker's say-so.

        SAME SHAPE AS ``_sent_folder_id``, and it carried the same defect. A
        listed folder whose ``id`` was empty was cached as a resolution and
        POSTed as ``{"destinationId": ""}``, so ``file_message`` reported
        the destination folder name for a move Graph had no destination for -- a
        receipt for a filing that did not happen. Routing both ids through
        ``ResolvedId`` refuses before the cache write, so an unresolvable
        folder raises and the next call re-resolves instead of reusing ``""``.

        The listing is walked to EXHAUSTION before concluding absence, because
        "not on the first page" is not "absent". A tenant with more mail
        folders than one page hides an existing folder behind the next link,
        and the create issued on that misreading mints the DUPLICATE the
        receipt then points at. The next link is read off the raw response
        dict and replayed verbatim, exactly as ``outlook_delta`` does.

        Returns:
            The destination folder id, proven non-empty by its type.

        Raises:
            ProviderError: If the matched or created folder carries no usable
                id. Refusing is correct: a move to an unproven destination is
                the failure the receipt is supposed to rule out.
        """
        name = self._folder_name(folder)
        cached = self._folder_cache.get(name)
        if cached is not None:
            return cached
        data = self._get(
            "me", "mailFolders", params={"$top": MAX_PAGE_SIZE}, operation="resolve_folder"
        )
        while True:
            existing = parse_graph(GraphFolderList, data, operation="resolve_folder")
            for item in existing.value:
                if item.display_name == name:
                    found = ResolvedId(item.id, what="mail folder id")
                    self._folder_cache[name] = found
                    return found
            next_link = data.get("@odata.nextLink")
            if next_link is None:
                break
            data = self._get(link=str(next_link), operation="resolve_folder")
        created_data = self._post(
            "me", "mailFolders", payload={"displayName": name}, operation="create_folder"
        )
        created = parse_graph(GraphFolder, created_data, operation="create_folder")
        minted = ResolvedId(created.id, what="created mail folder id")
        self._folder_cache[name] = minted
        return minted

    def file_message(self, message_id: str, folder: str) -> FiledMessage:
        """POST ``me/messages/{id}/move``. Graph's DELETE verb is never issued.

        Returns a ``FiledMessage`` rather than a bare folder name because
        Graph's ``/move`` is copy-then-delete: the message in the destination is
        a NEW entity with a NEW id, so the id the caller passed in is dead the
        moment this returns and a receipt carrying it resolves to nothing.

        See providers.base.MailProvider.file_message for the contract; the
        return is a ``str`` subclass, so every declared-``str`` reader is
        unaffected.
        """
        # Ordering, not validation. _graph_path would refuse the id anyway when
        # the move is issued, but by then _folder_id has already run a GET and
        # possibly CREATED a folder -- side effects of an id the adapter is
        # about to reject. Building the path first makes the refusal cost zero
        # requests; the check itself still lives at the boundary.
        move_path = ("me", "messages", message_id, "move")
        _graph_path(move_path)
        destination = self._folder_id(folder)
        # /move relocates the message. Graph's DELETE verb is never issued.
        moved_data = self._post(
            *move_path,
            payload={"destinationId": destination},
            operation="file_message",
        )
        moved = parse_graph(GraphMessage, moved_data, operation="file_message")
        # Graph's /move is copy-then-delete: the message in the destination
        # folder is a NEW entity with a NEW id, and the id we were handed is
        # dead the moment the move lands. Returning only the folder name left
        # the ledger recording an id that can no longer be resolved to
        # anything, which defeats the receipt.
        #
        # NO FALLBACK TO THE PRE-MOVE ID. This used to report `message_id`
        # when the response carried none, on the reasoning that a stale id
        # beats an empty one. It does not. The pre-move id is dead, so that
        # row names a message that does not exist while being indistinguishable
        # from a row that names one that does -- `reset restore` resolves it to
        # nothing and cannot say why. Reporting None says UNKNOWN, which a
        # caller can check and a reader can read.
        # NO FALLBACK TO THE PRE-MOVE ID. This used to report the caller's id
        # when the response carried none, on the reasoning that a stale id
        # beats an empty one. It does not: a row carrying a dead id is not a
        # weaker record but a WRONG one, indistinguishable from a row naming a
        # message that still exists, so `reset restore` resolves it to nothing
        # and cannot say why. None says UNKNOWN, which a caller can check.
        #
        # WARNING, not debug: the move SUCCEEDED (a non-2xx would already have
        # raised), so the mutation landed and only its receipt is incomplete --
        # the one state an operator must know about, because nothing later can
        # recover the id. No message id is logged; there is none to log.
        if not moved.id:
            logger.warning(f"GRAPH: /move reported no id, receipt id UNKNOWN folder={folder}")
        # `destination` is the id this very call POSTed as `destinationId`, so
        # it names the container the message is in NOW -- no name, no prefix,
        # and no second lookup that could disagree with the write. The watcher
        # needs it to mark the post-move id in the right stream's replay window
        # (#173); resolving that by display name is what a folder prefix broke.
        moved_id, dest = moved.id or None, str(destination)
        return FiledMessage(self._folder_name(folder), message_id=moved_id, destination_id=dest)

    def poll_changes(
        self, cursor: str | None, *, page_token: str | None = None, container_id: str | None = None
    ) -> ChangeSet:
        """What changed since ``cursor``, from ONE folder's delta stream. READ ONLY.

        ONE stream per FOLDER, not one per mailbox. Graph has no mailbox-wide
        delta -- ``/me/messages/delta`` does not exist -- so covering a Graph
        mailbox costs one stream and one independently-expiring token per
        folder, which is why a Graph deployment stores a cursor per container
        (``watchstate.WatchCursor.container_id``) where Gmail stores exactly
        one under ``None``. Which folder this call walks rides INSIDE the
        opaque link ``cursor`` and ``page_token`` carry; on a COLD START, where
        there is no link yet, ``container_id`` names it, and ``None`` means the
        inbox. It is read on a cold start only -- a stored link already encodes
        the folder Graph issued it for, and overriding that would point a
        stream at a folder it was never opened on.

        ``cursor=None`` is the COLD START, and unlike Gmail's it is not a
        different operation: Graph offers no position read to consult first, so
        a cold start is the same GET against the folder's delta path with no
        stored position yet.

        The round dispatch, the mechanics, the ``@removed`` filter, the log
        boundary, and the "refused token means resynchronise" rule all live in
        ``outlook_delta``. This method is the INTERFACE method and nothing
        else. Keeping the whole change feed out of this file is what holds it
        under the code-line ceiling; ``_to_message`` is passed as a VALUE so
        the import between the two modules stays one-directional.

        The delta read was the ONE Graph call sitting outside the 401 refresh,
        so an expired token turned a healthy watcher into a hard failure. The
        wrap sits HERE and not in ``outlook_delta`` because refreshing needs
        the PROVIDER -- ``refresh_credential`` clears the cached client so the
        re-invoked lambda rebuilds ``_http`` with the fresh token -- and
        ``outlook_delta`` stays transport-pure, holding no credential state.
        """
        args = (self._http, _to_message, cursor, page_token, container_id)
        return with_reauth_once(self, "poll_changes", lambda: outlook_delta.poll_changes(*args))

    def tag_message(self, message_id: str, label: str) -> str:
        """Add one category in place. Nothing is removed and nothing is moved.

        Graph's categories PATCH REPLACES the collection, so this is a
        read-union-write guarded by ``If-Match`` against the window between the
        read and the write. The rule, the live receipts, and the 412-vs-400
        split live in ``outlook_categories``; this method is the wiring, and it
        holds nothing a caller can steer.

        ``$select`` names ``categories`` ONLY. ``parentFolderId`` is not read
        because it is not needed and not written because Graph IGNORES it on a
        PATCH while answering 200 -- a stray field would look successful while
        doing nothing, and the no-move guarantee here is code-layer rather than
        scope-layer, so the field simply never appears. ``@odata.etag`` needs no
        projection: Graph emits it regardless of what ``$select`` asks for.

        The attempt budget is ``retry.MAX_ATTEMPTS``, reused rather than
        redeclared. It fits without adjustment: it is documented there as TOTAL
        attempts and this loop counts the same way, and a 412 loop is
        self-limiting in the way that number was chosen for -- each 412 proves
        somebody else just wrote, so a mailbox that loses three races in a row
        has a human actively working the message, and backing off is the correct
        answer rather than a failure to try hard enough.

        Raises:
            LabelNameError: ``label`` is empty, over-long, or carries a
                category-list delimiter. Raised BEFORE any request goes out.
            ProviderError: Graph refused the write, or the attempt budget was
                spent on lost races. The caller records the failed outcome; a
                provider never writes to the ledger itself.
        """
        # Validated FIRST, before any request. The RETURNED value is used from
        # here on, never the argument: the strip is a correction, and
        # discarding it would reintroduce the invisible trailing-space category
        # that validate_label_name exists to prevent.
        name = validate_label_name(label)
        path = ("me", "messages", message_id)
        with ProviderCall(PROVIDER, "tag_message", message_id=message_id, label=name):
            return apply_category(
                lambda: self._get(
                    *path, params={"$select": CATEGORIES_FIELD}, operation="tag_message_read"
                ),
                lambda union, headers: self._patch(
                    *path,
                    payload={CATEGORIES_FIELD: union},
                    operation="tag_message",
                    headers=headers,
                ),
                name,
                attempts=MAX_ATTEMPTS,
            )

    def set_important(self, message_id: str, important: bool = True) -> str:
        """PATCH both of Graph's importance signals, because it has two.

        ``importance`` is the sender-set header field and ``flag`` is the
        reader's own follow-up flag; Outlook surfaces them in different places,
        so writing only one leaves the message looking important in one view and
        ordinary in the other. The returned value names the ``importance`` level
        only -- ``"high"``/``"normal"`` -- since that is the field the contract's
        ``str`` return describes.

        See providers.base.MailProvider.set_important for the contract.
        """
        payload: dict[str, Any] = {
            "importance": "high" if important else "normal",
            "flag": {"flagStatus": "flagged" if important else "notFlagged"},
        }
        self._patch("me", "messages", message_id, payload=payload, operation="set_important")
        return "high" if important else "normal"

    def create_draft(self, thread_id: str, body: str) -> DraftResult:
        """Reply draft on an existing thread, via Graph's reply-draft action.

        The recipients are MICROSOFT'S doing, not ours. The action builds the
        reply from the original message server-side, which is why this method
        takes no recipient argument and why there is no code here that could
        add one. We supply the body and nothing else.

        The draft lands in Drafts. Nothing in this package can send it: the
        per-message send action and /me/sendMail are not called anywhere, and
        the token does not carry Mail.Send in the first place.
        """
        items = self._conversation(thread_id, operation="create_draft_read_thread")
        if not items:
            raise ProviderError(f"No thread {thread_id!r} in this mailbox; nothing to reply to.")

        # Reply to the most recent message in the conversation, which is what
        # a human hitting Reply would do. ``_conversation`` guarantees the
        # newest-first order; this line depends on that and on nothing else.
        anchor = items[0]
        # Graph's own id, not a caller's -- but it goes through the same
        # boundary anyway. A provider-sourced id has no exemption here: the
        # rule is "every dynamic segment is validated", and an exemption is a
        # second code path that a future reviewer has to reason about.
        # ResolvedId, not `str(anchor["id"])`: an anchor arriving without the
        # key raised a bare KeyError, which escapes this adapter's contract --
        # the tool layer maps ProviderError, not arbitrary exceptions, so the
        # host AI saw a crash instead of an actionable refusal. Same failure
        # direction (closed) either way; this makes it a legible refusal.
        created_data = self._post(
            "me",
            "messages",
            ResolvedId(anchor.id, what="thread anchor message id"),
            _REPLY_DRAFT_ACTION,
            payload={"comment": body},
            operation="create_draft",
        )
        created = parse_graph(GraphMessage, created_data, operation="create_draft")
        return DraftResult(
            draft_id=created.id,
            thread_id=thread_id,
            recipients=created.recipient_addresses() or _participants(items),
        )

    def create_self_draft(self, subject: str, body: str) -> str:
        """Draft to NOBODY in the owner's own mailbox. Creates; never sends.

        The digest delivery path (#65). ``POST /me/messages`` creates a message
        in the account's own Drafts folder -- it is the create half of Graph's
        compose flow, and the SEND half (``/send`` on the created message, and
        ``/me/sendMail``) is a separate action this package calls nowhere and
        the token does not carry ``Mail.Send`` for.

        The payload is a subject and a body and NOTHING ELSE -- see
        ``models.self_draft_payload``, which is where that absence is stated
        and reviewable. No ``toRecipients``, no ``ccRecipients``, no
        ``bccRecipients``, and no parameter on this method that could supply
        one; Graph defaults those collections to empty, so the draft the owner
        opens is addressed to no one.

        Both path segments are ``_STATIC_SEGMENTS`` words, so nothing dynamic
        reaches the URL here at all. NOT retried, for the same reason
        ``_post`` states: a POST whose outcome is unknown would duplicate the
        draft.
        """
        return self_draft.create_self_draft(self._post, subject, body)


def _unwrap(response: httpx.Response, method: str, path: str) -> dict[str, Any]:
    """Turn a Graph response into a dict, or raise a bounded ProviderError.

    Args:
        response: The Graph response.
        method: The HTTP verb, for the failure message.
        path: The request path, already validated and encoded.

    Raises:
        GraphStatusError: On any 4xx/5xx, carrying the status and a truncated
            detail. Never the whole body. A ``ProviderError`` SUBCLASS, so
            every existing handler catches it unchanged and the message is
            byte-identical; the status rides along because the conditional
            category write has to tell a 412 (somebody else wrote first --
            re-read and retry) from a 400 (our own malformed ``If-Match`` --
            a bug, never retried). Both are ``>= 400`` and both otherwise
            arrive as the same bounded refusal.
    """
    if response.status_code >= 400:
        raise GraphStatusError(str(_provider_error(response, method, path)), response.status_code)
    if response.status_code == 204 or not response.content:
        return {}
    parsed = response.json()
    return parsed if isinstance(parsed, dict) else {}


def _participants(items: Sequence[GraphMessage]) -> list[str]:
    """Every address on the thread: senders plus To plus Cc, de-duplicated.

    The owner is INCLUDED here. Removing them is the tool layer's job, done
    once against the authenticated account address, so that the "minus the
    owner" rule lives in one place instead of once per adapter.
    """
    seen: dict[str, None] = {}
    for item in items:
        if item.sender_address:
            seen.setdefault(item.sender_address, None)
        for address in item.recipient_addresses():
            seen.setdefault(address, None)
    return list(seen)


def _categories(items: Sequence[GraphMessage]) -> list[str]:
    """Every category across a thread, de-duplicated, FIRST-SEEN ORDER PRESERVED.

    A dict keyed on the name rather than a set, for the ordering: the caller
    hands these to ``Thread.labels``, which a human reads, and a set would
    reshuffle them between calls for no reason. Empty strings are dropped
    rather than propagated: an empty category is not a label anyone applied,
    and it would render as a blank chip the reader cannot act on.
    """
    seen: dict[str, None] = {}
    for item in items:
        for category in item.categories:
            if category:
                seen.setdefault(category, None)
    return list(seen)


def _to_sent(item: GraphMessage) -> SentMessage:
    """Project one validated Graph message into the provider-neutral sent shape.

    Carries the BODY, which ``_to_message`` deliberately does not: a sent
    message is evidence of what this connector's owner already put on the wire,
    so there is nothing to withhold, whereas an inbox listing gets a preview
    only. Field selection is the entire job -- nothing here fetches or decides.
    """
    return SentMessage(
        id=item.id,
        thread_id=item.conversation_id,
        recipients=item.recipient_addresses(),
        subject=item.subject,
        sent=item.sent,
        body=item.body.content,
    )


def _to_message(item: GraphMessage, *, internet_message_id: str = "") -> Message:
    """Project one validated Graph message into the provider-neutral header shape.

    THE ONE PLACE Graph's message shape becomes this connector's, which is why
    it is passed as a VALUE into ``delta.poll_changes`` rather than imported
    there -- that keeps the import arrow between the two modules pointing one
    way (see that module's docstring).

    A SNIPPET, never the body: ``body_preview`` is Graph's own truncation, so
    an inbox listing hands the host AI enough to triage on without moving the
    owner's mail through it. ``is_important`` is a UNION of Graph's two signals
    -- the sender's ``importance`` header and the reader's own follow-up flag --
    because Outlook surfaces them separately and either one means the owner
    should look.

    Args:
        item: The already-validated Graph message.
        internet_message_id: Read off the RAW payload by the caller, since it is
            not part of the projected model and ``$select`` does not always
            carry it. Defaults to empty, which stays distinguishable from a
            real value.
    """
    return Message(
        id=item.id,
        thread_id=item.conversation_id,
        sender=item.sender_address,
        subject=item.subject,
        received=item.received,
        internet_message_id=internet_message_id,
        snippet=item.body_preview,
        is_unread=not item.is_read,
        is_important=item.importance == "high" or item.flag.flag_status == "flagged",
        # Exactly one, or none. A Graph message sits in a single folder
        # (Container.exclusive is True for Graph), so this is a one-element
        # tuple -- and an empty one when the projection did not resolve a
        # parent, which stays distinguishable from "parented at ''".
        containers=(item.parent_folder_id,) if item.parent_folder_id else (),
    )
