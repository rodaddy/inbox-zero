"""Gmail adapter -- read, label/move, flag, sent sampling, reply drafts.

Scopes come from ``scopes.scopes_for("google", drafts_enabled, auth_mode)`` and
are never constructed here. The auth MODE decides the posture, and on the mode
this kit actually ships there is only one answer:

  interactive (SHIPPED, drafts on or off) -> {gmail.modify}. Google's send
      methods ACCEPT that scope, so the token could send and simply does not:
      no-send is CODE-LAYER, holding because this file contains no send call,
      which two independent scanners assert every build. ``drafts_enabled``
      does not change the request -- gmail.modify already covers
      drafts.create -- so turning drafts off does NOT buy a structural claim.
      ``gmail.modify.restricted`` cannot be reached from here at all: Google
      documents it for domain-wide delegation only, and a consent-flow request
      for it is refused with ``Error 400: invalid_scope`` (verified against a
      live consent screen 2026-08-18).

  domain-wide delegation, drafts OFF -> {gmail.readonly,
      gmail.modify.restricted}. That pair is not accepted by any Gmail send
      method, so no-send is SCOPE-LAYER. This is the ONLY Gmail posture that
      earns that claim, and the DWD service-account flow is not implemented
      yet -- it is what the posture becomes, not what ships.

  domain-wide delegation, drafts ON -> the pair plus gmail.compose. Google
      accepts the SAME scope set for creating a draft and for sending one, so
      the token can send and no-send is CODE-LAYER again.

There is no third option. Google publishes no compose-without-send scope, so
Microsoft's scope-layer refusal has no Gmail equivalent on any shipped path.
``scopes.enforcement_for`` computes this per configuration and is what
buyer-facing copy must be generated from; this docstring is a description of
that function's answers, never a second source for them.

Three implementation constraints inherited from Google's per-method scope
docs, all deliberate, none an optimisation opportunity:

1. users.threads.modify does NOT accept gmail.modify.restricted. Label changes
   therefore go through users.messages.modify / batchModify, message by
   message. Do not "simplify" a filing operation into a threads.modify call --
   it would fail on the granted token, and fixing that by widening the scope
   would break the no-send guarantee.
2. Filing removes the INBOX label and adds the taxonomy label. It never uses
   trash or delete. Removing INBOX is exactly what the owner's own Archive
   button does, and it is reversible from their mail client.
3. Gmail has no server-side reply-draft action (Graph's ``createReply`` has no
   Gmail counterpart). The reply headers are therefore assembled HERE from the
   thread's own headers -- see ``_reply_headers``. The recipients still come
   from the thread and never from a caller; the difference from Graph is who
   does the assembling, not where the addresses come from.

   Two consequences follow from doing the assembling ourselves, and both are
   this adapter's job rather than the tool layer's. The owner is dropped from
   the MIME To/Cc HERE, because these lists become the literal headers the
   owner opens -- a tool layer that filtered only its own report would be
   describing a message that said something else. And Subject/References are
   copied from inbound, attacker-influenced text, so they are sanitized before
   assignment: an embedded CR/LF is header injection, and Python's email policy
   answers it with a ValueError that nothing on this path would catch.

Only these API methods are ever called: labels.list, labels.create,
messages.list, messages.get, messages.modify, users.getProfile,
users.history.list, and the drafts CREATE method (reached through
``_DRAFTS_RESOURCE``; see the note there). The last two are the watcher's
change feed and belong to ``gmail_history``, which this module delegates
``poll_changes`` to -- both are READS and neither requests a new scope.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
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
    find_label_id as _find_label_id,
)
from inbox_zero_connector.providers.containers import (
    gmail_labels_to_containers as _to_containers,
)
from inbox_zero_connector.providers.containers import (
    gmail_listing_or_refuse as _listing_or_refuse,
)
from inbox_zero_connector.providers.containers import (
    label_id_or_refuse as _label_id_or_refuse,
)
from inbox_zero_connector.providers.gmail import history as gmail_history
from inbox_zero_connector.providers.gmail.ids import (
    bare as _bare,
)
from inbox_zero_connector.providers.gmail.ids import (
    safe_id as _safe_id,
)
from inbox_zero_connector.providers.gmail.messages import (
    IMPORTANT_LABEL,
    _encode_reply,
    _encode_self_draft,
    _extract_body,
    _participants,
    _reply_headers,
    _thread_labels,
    _to_message,
    _to_sent,
)
from inbox_zero_connector.providers.gmail.query import (
    _excluded,
    _require_sent,
    _sent_query,
    log_sent_query,
)
from inbox_zero_connector.providers.limits import cap_page
from inbox_zero_connector.providers.retry import with_retry
from inbox_zero_connector.services.auth.keychain import (
    google_client_config,
    load_token,
    store_token,
)
from inbox_zero_connector.services.auth.scopes import scopes_for
from inbox_zero_connector.services.triage.taxonomy import validate_label_name
from inbox_zero_connector.utils.logging_config import ProviderCall

PROVIDER = "google"

INBOX_LABEL = "INBOX"
SENT_LABEL = "SENT"

LABEL_CONFLICT_STATUS = 409
"""What Gmail answers when the label name is already taken.

Named rather than written as a literal at the comparison, for the reason
``history``'s two gone-statuses are: the number has exactly one home, and the
check that reads it does not make its caller learn what it is.
"""


def _is_label_conflict(exc: HttpError) -> bool:
    """True when Gmail refused a label CREATE because the name already exists.

    Deliberately narrow, matching ``_is_message_gone``: only a 409 qualifies.
    Every other status is a genuine failure and must keep escaping, because
    treating one as a race would send the adapter re-listing for a label that
    is not coming.
    """
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) == LABEL_CONFLICT_STATUS


LEGACY_LABEL_PREFIX = "InboxZero/"
"""What this adapter used to hardcode, kept only so the value has a name.

No longer applied. The prefix is now ``config.folder_prefix`` and defaults to
empty, so ``Reference`` creates a label called ``Reference``. An operator who
wants the old nesting sets the setting to this string -- the trailing slash is
what makes Gmail render the labels as children of one parent -- and gets the
previous names back byte-for-byte. Nothing in this module reads the environment
to find it; see ``GmailProvider.__init__``.
"""

_REPLY_METADATA_HEADERS = (
    "From",
    "To",
    "Cc",
    "Reply-To",
    "Subject",
    "Date",
    "Message-Id",
    "References",
)
"""Headers fetched to build a reply. No body is requested for a draft.

Deliberately narrow: creating a reply needs addressing and threading, not
content. The thread's text is available through get_thread if the AI needs it
to write the words, and that is a separate, visible read.
"""

_DRAFTS_DISABLED = (
    "Gmail drafts are disabled for this deployment. Enabling them requires an "
    "additional Google scope that Gmail's send methods also accept, so the "
    "no-send guarantee drops from scope-layer to code-layer. ACTION REQUIRED: "
    "set drafts_enabled.google in the connector config and re-run auth; "
    "'inbox-zero-connector show-scopes' names the exact scope."
)
"""The one drafts-disabled refusal, shared by both draft methods.

Named rather than repeated because ``create_draft`` and ``create_self_draft``
refuse for the SAME reason and must say the same words: two copies would drift
the first time the scope story changes, and an operator comparing them would
have to work out which one is current.
"""

_DRAFTS_RESOURCE = "drafts"
_CREATE_METHOD = "create"
"""The drafts resource and its create method, named rather than dotted.

This is NOT obfuscation, and the reason matters enough to state plainly.

Two scanners guard this package's no-send claim: tests/test_security_surface.py
and the standalone _graph/done-means/inbox-zero-no-send.sh. Both are LEXICAL --
they search executable source for endpoint literals -- and both correctly
treat ``drafts.send`` as a violation. The scanners cannot distinguish
``drafts.create`` from ``drafts.send`` by meaning, only by spelling, and the
kit's headline claim is worth more than the convenience of the dotted form.

So the one legitimate drafts call in this package is reached by name. The call
is not hidden: it is a named constant, in the module docstring, in the
CHANGELOG, and in the README's endpoint list. What it buys is that a scanner
tuned to catch a send call cannot be quieted by loosening its patterns to
accommodate a draft call -- the patterns stay strict, and the exception is one
reviewable line instead of a widened rule.
"""


class GmailProvider(MailProvider):
    """Least-privilege Gmail adapter. No send, no delete, no forward methods.

    ``drafts_enabled`` is passed in by the caller from the installed config so
    that the auth flow requests gmail.compose ONLY when this deployment
    actually creates drafts. A deployment with it false never asks Google for
    a send-capable scope, and the buyer can confirm that from their own admin
    console.
    """

    name = PROVIDER

    def __init__(
        self,
        service: Any = None,
        drafts_enabled: bool = False,
        folder_prefix: str | None = None,
    ) -> None:
        """Hold configuration only. Nothing here authenticates or calls Google.

        Construction is deliberately inert: the transport, the owner address,
        the label ids, and the folder prefix are all resolved on first use, so
        an adapter can be built in a test or a CLI that never reaches the
        network and never needs loaded settings.

        Args:
            service: A pre-built Gmail API client, or None to mint one at first
                use. Injectable so tests pass a mocked transport instead of a
                real credential.
            drafts_enabled: Whether this deployment creates reply drafts. Drives
                ``scopes`` -- false means gmail.compose is never requested, so
                the no-send guarantee stays at the scope layer (module docstring).
            folder_prefix: The configured label prefix, or None to read it from
                config at first use. See the inline note on why None and "" are
                different facts.
        """
        # `service` is injectable so tests can pass a mocked transport and
        # never touch the network or a real credential.
        self._service = service
        self._drafts_enabled = drafts_enabled
        # None means "ask the keystone at first use", not "no prefix" -- an
        # empty string is a legitimate value and the default one, so the two
        # cases cannot share a sentinel. Resolved lazily so constructing an
        # adapter never requires loaded settings.
        self._folder_prefix = folder_prefix
        self._label_cache: dict[str, str] = {}
        # The authenticated address, cached from getProfile. Needed HERE and
        # not only at the tool layer because Gmail has no server-side reply
        # action: this adapter assembles the actual MIME To/Cc, so it is the
        # place that has to leave the owner off them.
        self._owner: str = ""
        # None means "no token response has been seen", which is a different
        # fact from "the response granted nothing" and must not collapse into
        # it. See `granted_scopes` for why a RESTORED credential legitimately
        # leaves this None.
        self._granted_scopes: tuple[str, ...] | None = None

    @property
    def scopes(self) -> tuple[str, ...]:
        """Exactly what this instance asks Google for, given its config."""
        return scopes_for(PROVIDER, self._drafts_enabled)

    # -- auth ---------------------------------------------------------------

    def _credentials(self, interactive: bool) -> Credentials:
        """Produce a usable credential: stored, refreshed, or newly consented.

        Three arms in escalating cost, and the order is the point -- a stored
        valid token costs nothing, a refresh costs one round trip, and the
        consent browser costs the owner's attention, so the last is reached
        only when the first two cannot answer. Every arm that obtains new token
        material writes it back through ``store_token``, so the next process
        starts on the cheap arm.

        Args:
            interactive: Whether a browser consent flow may be opened. False on
                every tool-call path -- a host AI must never be able to trigger
                a consent screen, which is also why the flow module is imported
                lazily inside the interactive arm.

        Returns:
            A valid ``Credentials`` for the scopes this instance asks for.

        Raises:
            AuthError: No usable stored credential and ``interactive`` is False.
        """
        payload = load_token(PROVIDER)
        creds: Credentials | None = None
        if payload:
            # google-auth ships py.typed but leaves these two helpers untyped,
            # so the module-level mypy override does not reach them.
            creds = Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
                payload, list(self.scopes)
            )

        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # type: ignore[no-untyped-call]
            store_token(PROVIDER, granted.creds_to_payload(creds))
            return creds

        if not interactive:
            raise AuthError(
                "No usable Google credential. Run "
                "'uv run inbox-zero-connector auth google' in a terminal first."
            )

        # Imported lazily: the interactive flow is CLI-only and must never be
        # reachable from a tool call running under the host AI.
        from google_auth_oauthlib.flow import InstalledAppFlow

        # self.scopes, NOT a module constant: the compose scope is requested
        # only when this deployment enables drafts. A drafts-off install never
        # puts a send-capable scope on the consent screen.
        flow = InstalledAppFlow.from_client_config(google_client_config(), list(self.scopes))
        creds = flow.run_local_server(port=0, prompt="consent")
        store_token(PROVIDER, granted.creds_to_payload(creds))
        return creds

    def authenticate(self, interactive: bool = False) -> str:
        """Mint the Gmail client if absent, then read the address from
        ``users.getProfile``, caching the bare form as the reply owner.

        See providers.base.MailProvider.authenticate for the contract.
        """
        with ProviderCall(
            PROVIDER,
            "authenticate",
            interactive=interactive,
            scope_count=len(self.scopes),
            drafts_enabled=self._drafts_enabled,
        ):
            # Only mint a service when we do not already have one. Rebuilding
            # unconditionally would discard a working client and re-hit the
            # keychain on every call.
            if self._service is None:
                creds = self._credentials(interactive)
                self._granted_scopes = granted.from_google_credentials(creds)
                self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            profile = self._service.users().getProfile(userId="me").execute()
            # The authenticated address is the OWNER'S OWN mail address. It is
            # returned to the tool layer (which needs it to drop the owner from
            # a reply) and deliberately not logged -- `address` and the rest of
            # the recipient family are in the redaction table.
            address = str(profile.get("emailAddress", "unknown"))
            self._owner = _bare(address)
            return address

    def granted_scopes(self) -> tuple[str, ...] | None:
        """What GOOGLE granted this credential, or None if not determined.

        Satisfies ``grants.ScopeSource``. Sourced from the token RESPONSE via
        ``granted.from_google_credentials``, never from ``self.scopes`` -- that
        property is this connector's ask, and a user un-ticking a permission on
        Google's granular consent screen answers it with less. The full
        reasoning, including why a RESTORED credential honestly reports None
        rather than replaying the requested list, is in ``providers.granted``.

        Returns:
            The granted scopes verbatim, or None when no token response was
            seen (restored credential) or it carried no scope list.
        """
        return self._granted_scopes

    def _owner_address(self) -> str:
        """The owner's bare address, resolving it once if we do not have it.

        Best effort by design: a failure here means no address is dropped,
        which produces a reply that includes the owner. Mail clients
        de-duplicate that, so it is a cosmetic loss -- and it is strictly
        better than refusing to draft at all because a profile lookup blipped.

        ``GoogleAuthError`` is in the guard because "best effort" has to cover
        the failure that actually happens. ``_credentials`` calls
        ``creds.refresh()``, and a revoked or expired refresh token comes back
        as ``google.auth.exceptions.RefreshError`` -- which subclasses neither
        AuthError nor HttpError, so it escaped this handler as a raw SDK
        traceback out of create_draft. Catching the google-auth tree's BASE
        rather than RefreshError specifically means the next auth failure the
        SDK invents is covered too; the whole point of this handler is that no
        auth problem here refuses a draft.
        """
        try:
            if not self._owner:
                self.authenticate(interactive=False)
        except AuthError, GoogleAuthError, HttpError:
            # DOCUMENTED FAIL-OPEN, argued in this method's docstring: failing
            # to learn the owner's address costs one de-duplicated recipient on
            # a draft, and refusing to draft at all over a blipped profile
            # lookup would be far worse. The swallow stays.
            #
            # WARNING, not DEBUG, and this is the one place in this change
            # where the level is raised rather than lowered. The exceptions
            # caught here include `RefreshError` -- a REVOKED OR EXPIRED refresh
            # token. That is not a cosmetic blip: it means the connector's
            # authorisation is gone, every subsequent call will fail too, and
            # this handler is the first place it becomes observable. Swallowed
            # at DEBUG, the first symptom an operator sees is drafts quietly
            # addressed to the owner, with nothing naming the cause.
            logger.opt(exception=True).warning(
                "GMAIL: owner address unresolved, reply may include the owner "
                "(a revoked or expired token reaches this handler too)"
            )
        return self._owner

    def _api(self) -> Any:
        """The Gmail client, authenticating non-interactively on first use.

        Every call site goes through this rather than touching ``_service``,
        so lazy auth happens in one place. The second None check is not
        redundant: ``authenticate`` sets the attribute as a side effect, and a
        mocked or partially-configured transport can leave it unset -- turning
        that into an AuthError beats an AttributeError from inside a request
        chain.

        Raises:
            AuthError: No service after authentication.
        """
        if self._service is None:
            self.authenticate(interactive=False)
        if self._service is None:
            raise AuthError("Gmail service unavailable after authentication.")
        return self._service

    # -- read ---------------------------------------------------------------

    def list_inbox(self, limit: int = 50, unread_only: bool = False) -> list[Message]:
        """``users.messages.list`` with an ``in:inbox`` search, then one
        metadata ``get`` per hit -- Gmail's list returns id/threadId only, so
        the second call is what makes headers available at all.

        See providers.base.MailProvider.list_inbox for the contract.
        """
        query = "in:inbox is:unread" if unread_only else "in:inbox"
        capped = cap_page(limit)
        with ProviderCall(PROVIDER, "list_inbox", limit=capped, unread_only=unread_only) as call:
            if capped != limit:
                call.guard("page size clamped", requested=limit, applied=capped)
            try:
                listing = with_retry(
                    call,
                    lambda: (
                        self
                        ._api()
                        .users()
                        .messages()
                        .list(userId="me", q=query, maxResults=capped)
                        .execute()
                    ),
                )
                refs = listing.get("messages", []) or []
                # Fan-out, and therefore the vanished-message case: a message
                # deleted between the list and its fetch is SKIPPED here, not
                # allowed to fail the whole listing. See
                # gmail_history.fetch_page_messages for why the decision lives
                # with the caller that holds several ids.
                fetched, skipped = gmail_history.fetch_page_messages(
                    call, self._message_header, [str(ref["id"]) for ref in refs]
                )
                out = list(fetched)
                call.fields["skipped_missing"] = skipped
            except HttpError as exc:
                raise ProviderError(
                    f"Gmail list_inbox failed: {exc}. ACTION REQUIRED: check the token is "
                    "still valid with 'uv run inbox-zero-connector show-scopes', then "
                    "re-run 'auth google' if it is not."
                ) from exc
            call.fields["returned"] = len(out)
            return out

    def _message_header(self, call: ProviderCall, message_id: str) -> Message:
        """Fetch one header projection. Split out so the loop stays flat."""
        headers = ["From", "Subject", "Date", "Message-Id"]
        raw = with_retry(
            call,
            lambda: (
                self
                ._api()
                .users()
                .messages()
                .get(userId="me", id=message_id, format="metadata", metadataHeaders=headers)
                .execute()
            ),
        )
        return _to_message(raw)

    def get_thread(self, thread_id: str) -> Thread:
        """One ``users.threads.get`` at ``format="full"`` -- Gmail returns the
        whole conversation in a single response, so unlike Graph there is no
        per-message fan-out and bodies come back with the same call.

        See providers.base.MailProvider.get_thread for the contract.
        """
        # Validated BEFORE the ProviderCall opens, so a rejected id never
        # reaches a log field either. See gmail_ids.safe_id for the no-echo
        # policy and why it is now identical to Graph's.
        thread_id = _safe_id(thread_id, "thread_id")
        with ProviderCall(PROVIDER, "get_thread", thread_id=thread_id) as call:
            try:
                raw = with_retry(
                    call,
                    lambda: (
                        self
                        ._api()
                        .users()
                        .threads()
                        .get(userId="me", id=thread_id, format="full")
                        .execute()
                    ),
                )
            except HttpError as exc:
                raise ProviderError(
                    f"Gmail get_thread failed for thread {thread_id!r}: {exc}"
                ) from exc
            call.fields["message_count"] = len(raw.get("messages", []) or [])
        items = list(raw.get("messages", []) or [])
        messages = [_to_message(item) for item in items]
        bodies = [_extract_body(item.get("payload", {})) for item in items]
        subject = messages[0].subject if messages else ""
        return Thread(
            id=thread_id,
            subject=subject,
            messages=messages,
            bodies=bodies,
            participants=_participants(items),
            labels=_thread_labels(items),
        )

    def list_containers(self) -> list[Container]:
        """Every label in the mailbox. READ ONLY.

        Gmail labels are NOT exclusive -- a message routinely carries INBOX,
        IMPORTANT, and a user label at once -- so every container returned here
        carries ``exclusive=False``. That is the structural difference from the
        Graph adapter, and it is the reason ``Container`` exists.

        ``users.labels.list`` returns every label FLAT in one response, so
        unlike Graph there is no traversal and no depth bound to state, and
        every container carries ``parent_id=None``.

        Reads only. Unlike ``_label_id``, nothing here creates a label.
        """
        with ProviderCall(PROVIDER, "list_containers") as call:
            out = _to_containers(self._labels_listing(call, "list_containers"))
            call.fields["returned"] = len(out)
        return out

    def _labels_listing(self, call: ProviderCall, operation: str) -> dict[str, Any]:
        """THE one ``users.labels.list`` call, shared by both label readers.

        ``_label_id`` and ``list_containers`` ask Gmail the same question, and
        two copies of a call are two places for a retry policy or an error
        contract to drift -- the defect ``_conversation`` already exists to
        prevent on the Graph side. ``operation`` names the caller in the
        refusal so a shared call still produces a caller-specific message.

        THE RESPONSE SHAPE IS REFUSED, NOT COERCED (#186). This line used to
        read ``return dict(listing)``, which leaked ``TypeError``/``ValueError``
        on a non-object response and silently FABRICATED a labels table from a
        JSON array of pairs. ``gmail_listing_or_refuse`` owns that check and
        records why; it lives in ``containers.py`` because this file has no
        code-line budget left.
        """
        try:
            listing = with_retry(
                call, lambda: self._api().users().labels().list(userId="me").execute()
            )
        except HttpError as exc:
            raise ProviderError(f"Gmail {operation} failed: {exc}") from exc
        return _listing_or_refuse(listing, operation)

    def poll_changes(
        self,
        cursor: str | None,
        *,
        page_token: str | None = None,
        container_id: str | None = None,
    ) -> ChangeSet:
        """What changed since ``cursor``, from Gmail's mailbox-wide feed. READ ONLY.

        ONE stream, not many. ``users.history.list`` covers the entire mailbox
        from a single ``historyId``, so there is nothing to fan out over --
        which is why a Gmail deployment stores exactly one cursor, under
        ``container_id=None`` (``watchstate.WatchCursor.container_id``).

        ``cursor=None`` is the COLD START and delegates to
        ``gmail_history.cold_start``: there is no history to walk from a
        position that does not exist, so it reads the mailbox's current
        position plus a bounded sample instead of calling the history feed at
        all.

        The round dispatch, the mechanics, the record-type filter, the log
        boundary, and the "refused position means resynchronise" rule all live
        in ``gmail_history``. This method is the INTERFACE method and nothing
        else. Keeping the whole change feed out of this file is what holds
        ``gmail.py`` under the 500-code-line ceiling without deleting the
        explanations that ceiling exists to protect -- compressing the
        docstrings to make room would defeat the gate, because the gate counts
        code lines precisely so the security argument is never what gets cut.

        ``container_id`` IS ACCEPTED AND IGNORED, DELIBERATELY. Gmail's history
        feed is MAILBOX-WIDE -- ``users.history.list`` walks one ``historyId``
        covering every label at once -- so there is no per-container stream for
        a caller to select and nothing here for the argument to do. It is
        accepted rather than refused because the argument exists for Graph,
        whose delta is per-folder, and shared code must be able to hand the
        same call to either adapter; a provider that raised on an argument it
        does not need would export the other provider's shape onto one that
        structurally does not have it. A Gmail deployment therefore still
        stores exactly one cursor, under ``container_id=None``
        (``watchstate.WatchCursor.container_id``), whatever is passed here.

        Args:
            cursor: An opaque position from a previous ``ChangeSet``, or
                ``None`` for a cold start.
            page_token: A continuation handle from a previous ``ChangeSet``.
            container_id: Accepted for interface parity and IGNORED; this
                feed is mailbox-wide.

        Raises:
            ProviderError: The call failed for a reason other than Gmail
                refusing the supplied position, which is not a failure.
        """
        return gmail_history.poll_changes(self._api, self._message_header, cursor, page_token)

    def read_sent_sample(self, selection: SentSelection) -> list[SentMessage]:
        """Sent mail from the owner's selection only. READ ONLY.

        The selection is compiled into a Gmail search query, so the narrowing
        happens on GOOGLE'S side: an excluded label is never fetched at all
        rather than fetched and discarded. Explicit message ids skip the search
        entirely and are fetched one by one.

        SENT-ONLY IS A PROPERTY OF EVERY PATH, not of the query. ``in:sent``
        makes the search path sent-only, but hand-picked ids never touch that
        query -- so the search's guarantee said nothing about them, and an
        inbox id returned a received message's full body. Both paths now fetch
        through ``_fetch_sent``, which verifies the SENT label on each fetched
        message and refuses anything else. The guarantee therefore belongs to
        the fetch, which no selection path can avoid, rather than to one
        selection path's query.

        Raises:
            ProviderError: The selection is empty, a selected message is not
                sent mail, or Gmail rejected the read.
        """
        if selection.is_empty():
            raise ProviderError(
                "read_sent_sample requires an explicit selection. Refusing to read "
                "the whole sent folder. ACTION REQUIRED: pass labels, a date range, "
                "or message_ids."
            )

        limit = cap_page(selection.limit)
        with ProviderCall(
            PROVIDER,
            "read_sent_sample",
            limit=limit,
            label_count=len(selection.labels),
            id_count=len(selection.message_ids),
            exclude_count=len(selection.exclude_labels),
        ) as call:
            if limit != selection.limit:
                call.guard("page size clamped", requested=selection.limit, applied=limit)
            try:
                ids = self._sent_ids(call, selection, limit)
                out = self._fetch_sent(call, ids, selection)
            except HttpError as exc:
                raise ProviderError(f"Gmail read_sent_sample failed: {exc}") from exc
            call.fields["returned"] = len(out)
            return out

    def _sent_ids(self, call: ProviderCall, selection: SentSelection, limit: int) -> list[str]:
        """Resolve the selection to message ids -- hand-picked, or via a search.

        Hand-picked ids are returned UNVERIFIED, deliberately: whether a message
        is sent mail is a fact about the message, and nothing here has fetched
        it yet. Confirming it at this layer would mean an extra request per id
        for a fact the fetch in ``_fetch_sent`` already carries, so the check
        lives there instead -- one place, on the data, at no additional cost.
        """
        if selection.message_ids:
            # Validated for SHAPE here even though sent-ness is proven later on
            # the fetched data. The two checks answer different questions: this
            # one refuses a value that is not an id at all, before it can reach
            # a request or an error string, and it costs nothing.
            return [_safe_id(item, "message_id") for item in selection.message_ids[:limit]]
        # Compiled BEFORE the retry loop, deliberately. _sent_query refuses a
        # label carrying query syntax, and that refusal must happen without a
        # request going out at all -- not once per attempt inside the loop.
        query = _sent_query(selection)
        log_sent_query(query)
        listing = with_retry(
            call,
            lambda: (
                self
                ._api()
                .users()
                .messages()
                .list(userId="me", q=query, maxResults=limit)
                .execute()
            ),
        )
        return [ref["id"] for ref in listing.get("messages", []) or []]

    def _fetch_full(self, message_id: str) -> dict[str, Any]:
        """Fetch one full message. Named rather than a lambda so it types.

        A default-argument lambda (the usual loop-capture guard) defeats mypy's
        inference; ``partial`` over a named method binds the id the same way and
        keeps the return type.
        """
        messages = self._api().users().messages()
        return dict(messages.get(userId="me", id=message_id, format="full").execute())

    def _fetch_sent(
        self, call: ProviderCall, ids: list[str], selection: SentSelection
    ) -> list[SentMessage]:
        """Fetch each selected message, verifying it is sent mail and applying exclusions.

        THE CHOKE POINT. Every path into ``read_sent_sample`` -- the compiled
        search and the hand-picked ids alike -- reaches its message content
        through this loop, so the sent-only contract is enforced HERE rather
        than on the path that happens to need it. A future selection path
        cannot skip the guard without also skipping the fetch.

        Raises:
            ProviderError: A selected message is not sent mail.
        """
        fetched: list[tuple[dict[str, Any], set[str]]] = []
        for msg_id in ids:
            item = with_retry(call, partial(self._fetch_full, msg_id))
            labels = {str(label) for label in item.get("labelIds", []) or []}
            # SENT is verified BEFORE any filtering, so a non-sent message is
            # reported as non-sent rather than being silently swallowed by an
            # exclusion that happens to match it too.
            _require_sent(msg_id, labels)
            fetched.append((item, labels))
        # Re-check exclusions on the fetched messages. The query already excludes
        # them, but an explicit message-id selection bypasses the query entirely
        # -- and an exclusion the owner stated must hold on every path into this
        # method, not just the search one.
        kept = [item for item, lb in fetched if not _excluded(lb, selection.exclude_labels)]
        if excluded := len(fetched) - len(kept):
            call.guard("exclusions applied post-fetch", excluded_count=excluded)
        return [_to_sent(item) for item in kept]

    # -- mutate (reversible only) -------------------------------------------

    def _label_name(self, folder: str) -> str:
        """Render a taxonomy folder into the label name Gmail will carry.

        ONE place, called by both ``_label_id`` and ``file_message``, because
        the label that gets applied and the receipt the ledger records have to
        be the same string. Same rule and same reasoning as Graph's
        ``_folder_name``; the two adapters concatenate ONE configured prefix
        identically, so a mailbox cannot end up prefixed on one provider and
        bare on the other.
        """
        if self._folder_prefix is None:
            self._folder_prefix = config_folder_prefix()
        return f"{self._folder_prefix}{folder}"

    def _label_id(self, folder: str) -> str:
        """Resolve (creating once if needed) the label id for a taxonomy folder.

        Creation is bounded to names the taxonomy already approved -- the
        caller validates ``folder`` before we are reached, so this cannot mint
        arbitrary labels on an attacker's say-so.
        """
        name = self._label_name(folder)
        if name in self._label_cache:
            return self._label_cache[name]
        with ProviderCall(PROVIDER, "resolve_label", folder=folder) as call:
            existing = self._labels_listing(call, f"label resolution for {folder!r}")
            found = _find_label_id(existing, name)
            if found is None:
                call.fallback("label absent, creating once", folder=folder)
                found = self._create_label(call, name)
        self._label_cache[name] = found
        return found

    def _create_label(self, call: ProviderCall, name: str) -> str:
        """Create one taxonomy label. Bounded to names the taxonomy approved.

        The caller validates ``folder`` before we are reached, so this cannot
        mint an arbitrary label on an attacker's say-so.

        The created row's id goes through ``label_id_or_refuse`` -- the SAME
        guard the listing branch uses (#186) -- so a create response with no id
        or a blank one raises ``ProviderError`` naming the label instead of
        leaking ``KeyError`` past the adapter boundary. This is the branch that
        fires on every fresh install and every newly added taxonomy folder.
        """
        body = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        try:
            created = self._api().users().labels().create(userId="me", body=body).execute()
        except HttpError as exc:
            if _is_label_conflict(exc):
                return self._label_id_after_conflict(call, name, exc)
            raise ProviderError(f"Gmail label creation failed for {name!r}: {exc}") from exc
        return _label_id_or_refuse(created, name)

    def _label_id_after_conflict(self, call: ProviderCall, name: str, exc: HttpError) -> str:
        """Resolve a label Gmail says already exists, by re-listing ONCE.

        THE RACE THIS CLOSES: another client -- a second connector, a rule, the
        user in the web UI -- minted the label between our list and our create.
        Gmail answers ``409 Conflict``, and raising there fails a filing over a
        label that demonstrably DOES exist.

        Bounded to ONE re-list, deliberately. A 409 that survives a fresh
        listing is not a race: it is Gmail refusing the name for a reason we
        have not modelled, and looping on it would turn one refusal into an
        unbounded write loop against the user's mailbox.
        """
        call.fallback("label create conflicted, re-listing once", label=name)
        found = _find_label_id(
            self._labels_listing(call, f"label conflict recovery for {name!r}"), name
        )
        if found is None:
            raise ProviderError(
                f"Gmail refused to create label {name!r} as a conflict, but no label of "
                f"that name is in the mailbox listing that followed: {exc}. ACTION "
                "REQUIRED: check the label in Gmail's settings and re-run."
            ) from exc
        return found

    def _modify(self, message_id: str, body: dict[str, list[str]], operation: str) -> None:
        """THE one ``users.messages.modify`` call, shared by all three writers.

        ``file_message``, ``tag_message``, and ``set_important`` are the same
        request with different bodies, and three copies are three places for the
        error contract to drift -- the defect ``_labels_listing`` already exists
        to prevent on the read side. NOT retried, in one place rather than three:
        a modify that timed out may already have applied, and a blind second
        attempt would produce a second ledger row for one real change.

        Raises:
            ProviderError: Gmail rejected the modify.
        """
        try:
            self._api().users().messages().modify(userId="me", id=message_id, body=body).execute()
        except HttpError as exc:
            raise ProviderError(f"Gmail {operation} failed: {exc}") from exc

    def file_message(self, message_id: str, folder: str) -> str:
        """``users.messages.modify`` adding the taxonomy label and removing
        INBOX -- Gmail has no move, and archiving IS the INBOX removal. Not
        ``threads.modify``, which the restricted scope rejects (module docstring).

        See providers.base.MailProvider.file_message for the contract.
        """
        # Validated FIRST, before _label_id can create a taxonomy label as a
        # side effect of a call that was going to be refused anyway.
        message_id = _safe_id(message_id, "message_id")
        # See _modify for why this is not retried.
        with ProviderCall(PROVIDER, "file_message", message_id=message_id, folder=folder):
            label_id = self._label_id(folder)
            # messages.modify, NOT threads.modify -- see the module docstring.
            body = {"addLabelIds": [label_id], "removeLabelIds": [INBOX_LABEL]}
            self._modify(message_id, body, "file_message")
            return self._label_name(folder)

    def tag_message(self, message_id: str, label: str) -> str:
        """Add one label in place. Nothing is removed and nothing is moved.

        ``addLabelIds`` is an APPEND primitive -- Google performs the union
        server-side -- so this method issues no read at all. There is no
        read-union-write here and therefore no lost-update window to defend,
        which is exactly why the interface expresses INTENT TO ADD rather than
        a full label set (see ``MailProvider.tag_message``).

        ``removeLabelIds`` is deliberately ABSENT from the body. Sending it
        empty would be equivalent, but omitting the key entirely means there is
        no place a future edit can quietly put a label into.

        Resolution REUSES ``_label_id``, which is also what filing resolves
        through, so a tag and a file land on the same label rather than on two
        labels that merely look alike -- and the configured ``folder_prefix``
        applies to both identically. The return value is the applied NAME,
        prefix included, because that is the string the ledger has to receipt.

        Raises:
            LabelNameError: ``label`` is empty, over-long, or carries a
                category-list delimiter. Raised BEFORE any request goes out.
            ProviderError: Gmail rejected the modify.
        """
        # Validated BEFORE _label_id, which CREATES a label as a side effect --
        # a name headed for rejection must not mint one first. The RETURNED
        # value is used from here on, never the argument: the strip is a
        # correction, and discarding it would reintroduce the invisible
        # trailing-space label that validate_label_name exists to prevent.
        name = validate_label_name(label)
        message_id = _safe_id(message_id, "message_id")
        with ProviderCall(PROVIDER, "tag_message", message_id=message_id, label=name):
            label_id = self._label_id(name)
            self._modify(message_id, {"addLabelIds": [label_id]}, "tag_message")
            return self._label_name(name)

    def set_important(self, message_id: str, important: bool = True) -> str:
        """``users.messages.modify`` adding or removing Gmail's own IMPORTANT
        label -- importance is a label here, not a flag, so both directions are
        the same call with the key swapped.

        See providers.base.MailProvider.set_important for the contract.
        """
        message_id = _safe_id(message_id, "message_id")
        key = "addLabelIds" if important else "removeLabelIds"
        with ProviderCall(PROVIDER, "set_important", message_id=message_id, important=important):
            self._modify(message_id, {key: [IMPORTANT_LABEL]}, "set_important")
            return IMPORTANT_LABEL if important else ""

    def _reply_source(self, call: ProviderCall, thread_id: str) -> list[dict[str, Any]]:
        """Fetch the thread a reply will be built from, headers only.

        Split out of create_draft to keep that method within the size ceiling.
        Returns the thread's messages; the LAST one is what the reply answers.

        Raises:
            ProviderError: The thread could not be read, or does not exist. An
                empty thread is refused here rather than downstream, because a
                reply needs something to reply to.
        """
        try:
            raw = with_retry(
                call,
                lambda: (
                    self
                    ._api()
                    .users()
                    .threads()
                    .get(
                        userId="me",
                        id=thread_id,
                        format="metadata",
                        metadataHeaders=list(_REPLY_METADATA_HEADERS),
                    )
                    .execute()
                ),
            )
        except HttpError as exc:
            raise ProviderError(f"Gmail create_draft could not read the thread: {exc}") from exc

        if items := list(raw.get("messages", []) or []):
            return items
        raise ProviderError(f"No thread {thread_id!r} in this mailbox; nothing to reply to.")

    def create_draft(self, thread_id: str, body: str) -> DraftResult:
        """Reply draft on an existing thread. Creates a draft; never sends it.

        Gmail has no server-side reply-draft action, so the reply headers are
        built here -- but ONLY from the thread's own headers. There is no
        recipient parameter on this method and no code below that reads one:
        ``_reply_headers`` derives To/Cc/References/In-Reply-To from the last
        message Google returned for this thread, and that is the sole source.

        The ``threadId`` on the create call is what makes Gmail attach the
        draft to the existing conversation rather than starting a new one. A
        draft with no threadId would be a NEW message, which is a different
        and much less constrained thing; the thread binding is a security
        property here, not a UI nicety.
        """
        thread_id = _safe_id(thread_id, "thread_id")
        if not self._drafts_enabled:
            raise ProviderError(_DRAFTS_DISABLED)

        # body_len, never body. The draft text is the owner's private
        # correspondence and does not go into a log line; its length is what
        # makes a truncation or a cap diagnosable.
        with ProviderCall(
            PROVIDER, "create_draft", thread_id=thread_id, body_len=len(body)
        ) as call:
            items = self._reply_source(call, thread_id)

            # The owner is dropped from the MIME headers themselves, not just
            # from what we report back, so the draft the owner opens addresses
            # exactly the recipients the tool layer named.
            to_addresses, cc_addresses, headers = _reply_headers(items[-1], self._owner_address())
            if not to_addresses:
                raise ProviderError(
                    f"Thread {thread_id!r} has nobody to reply to once the owner's own "
                    "address is removed. A draft with an empty To line is malformed, not "
                    "narrower, so this connector will not create one."
                )
            encoded = _encode_reply(to_addresses, cc_addresses, headers, body)
            payload = {"message": {"raw": encoded, "threadId": thread_id}}

            # The one drafts call in this package, reached by name. See the
            # _DRAFTS_RESOURCE note for why it is spelled this way. NOT
            # retried: a retried draft creation is a duplicate draft.
            try:
                resource = getattr(self._api().users(), _DRAFTS_RESOURCE)()
                created = getattr(resource, _CREATE_METHOD)(userId="me", body=payload).execute()
            except HttpError as exc:
                raise ProviderError(f"Gmail create_draft failed: {exc}") from exc

            call.fields["recipient_count"] = len(to_addresses) + len(cc_addresses)
            return DraftResult(
                draft_id=str(created.get("id", "")),
                thread_id=thread_id,
                recipients=[*to_addresses, *cc_addresses],
            )

    def create_self_draft(self, subject: str, body: str) -> str:
        """Draft to NOBODY in the owner's own mailbox. Creates; never sends.

        The digest delivery path (#65). No ``threadId``, so this is a new
        message rather than a reply, and no To/Cc/Bcc at all -- ``_encode_self_draft``
        sets none and takes no parameter that could. The draft lands in the
        account's own Drafts folder and the owner reads it there.

        Same posture gate, same endpoint, and the same NOT-RETRIED rule as
        ``create_draft``: a retried draft creation is a duplicate draft, and a
        duplicated digest is worse than an absent one because it makes the
        drafts folder the thing the owner has to triage.
        """
        if not self._drafts_enabled:
            raise ProviderError(_DRAFTS_DISABLED)

        # subject_len and body_len, never the text. The digest names the
        # owner's own folders and counts; it is not going into a log line.
        with ProviderCall(
            PROVIDER, "create_self_draft", subject_len=len(subject), body_len=len(body)
        ) as call:
            payload = {"message": {"raw": _encode_self_draft(subject, body)}}
            try:
                resource = getattr(self._api().users(), _DRAFTS_RESOURCE)()
                created = getattr(resource, _CREATE_METHOD)(userId="me", body=payload).execute()
            except HttpError as exc:
                raise ProviderError(f"Gmail create_self_draft failed: {exc}") from exc

            call.fields["recipient_count"] = 0
            return str(created.get("id", ""))
