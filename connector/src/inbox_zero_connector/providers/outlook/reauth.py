"""One retry, and only one, after Graph rejects a request with HTTP 401.

WHY THIS EXISTS
    The Sprint 2 live soak died about twelve minutes after launch on Graph
    ``401 InvalidAuthenticationToken``. The defect was not in the watcher loop:
    ``OutlookProvider._http`` acquires an access token ONCE, bakes it into an
    ``Authorization`` header on a cached ``httpx.Client``, and nothing ever
    refreshes it. A Graph access token expires in roughly an hour, so any
    unattended run longer than that was guaranteed to die -- the only question
    was when.

    The refresh path already existed and was simply never re-invoked.
    ``OutlookProvider.authenticate(interactive=False)`` runs MSAL
    ``acquire_token_silent`` against the stored serialized cache, which mints a
    fresh access token from the refresh token with no human present. This
    module is the small amount of control flow that connects "Graph said 401"
    to "call that method again, then try once more".

WHY THIS IS ITS OWN MODULE
    ``outlook.py`` was measured at 495 of the 500 code lines
    ``scripts/check_code_size.py`` allows, so there was no room to put this
    there. That constraint is not the only argument for the split, but it is
    the binding one: the ceiling counts CODE lines precisely so that the
    reasoning an auditor reads is never what gets deleted to make room.

    The import is ONE-DIRECTIONAL: ``outlook`` imports this module and never
    the other way. Both the refresh and the send arrive as CALLABLES rather
    than as a provider object, which is what keeps the arrow pointing one way
    and lets this module be tested without constructing a provider at all.

WHY EXACTLY ONE RETRY, AND WHY 401 IS SAFE TO RETRY AT ALL
    A 401 means Graph REJECTED the request before acting on it. Nothing was
    read, nothing was moved, nothing was created. That is what makes a retry
    side-effect-safe for POST and PATCH here, where the ordinary retry policy
    in ``providers/retry.py`` deliberately refuses to retry writes: a timed-out
    POST may have already moved the message, but a 401-ed POST provably has
    not.

    The retry budget is ONE because a second 401 is a different fact from the
    first. The first says "this access token is stale"; the second, after a
    fresh token was minted and sent, says "this credential is dead" -- expired
    refresh token, revoked consent, a Conditional Access policy that now
    refuses this client. None of those get better by trying again, and a loop
    around them would turn a credential that needs a human into a process that
    hammers Entra forever while the mailbox goes unwatched. It must fail loud.

WHY THIS IS NOT PART OF THE TENACITY RETRY
    ``_get`` already retries transient statuses through ``with_retry``, and 401
    is deliberately absent from ``retry.RETRYABLE_STATUS``. It does not belong
    there: a transient status is retried by REPEATING the identical request in
    the hope the service has recovered, and repeating an identical request with
    the same expired token produces the same 401 every time. This is a
    different shape -- refresh the credential, rebuild the request, try once --
    so it wraps OUTSIDE that policy rather than being folded into it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Final, Protocol
from weakref import WeakKeyDictionary

import httpx
from loguru import logger

from inbox_zero_connector.providers.base import AuthError
from inbox_zero_connector.providers.outlook.categories import GraphStatusError
from inbox_zero_connector.services.auth.keychain import (
    load_token,
    microsoft_client_config,
    store_token,
)

PROVIDER_KEY: Final = "microsoft"
"""The provider key this module's credential writes are filed under.

Defined HERE rather than imported from ``provider``: this module owns the
credential lifecycle and ``provider`` imports it, so taking the constant the
other way would close an import cycle. ``provider.PROVIDER`` aliases this one,
so there is still exactly one string.
"""

UNKNOWN_ACCOUNT: Final = "unknown"
"""What ``account_name`` reports when neither claims nor cache named a mailbox.

Named because two callers now branch on it: the label read returns it, and
``authenticate`` refuses to STORE it as this install's pin -- pinning the
literal string ``unknown`` would match no account on the next run and brick
the credential over a cosmetic gap.
"""

UNAUTHORIZED = 401
"""The one status this module reacts to.

Named because the number is a decision, not a detail: 403 is Graph saying the
credential is understood and not permitted, which no amount of refreshing
fixes, and 401 is Graph saying it could not authenticate the request at all.
Only the second is a stale-token symptom.
"""

IMMUTABLE_ID_PREFER: Final = 'IdType="ImmutableId"'
"""Graph's default message ids are FOLDER-DEPENDENT.

A ``/move`` hands back a NEW id and strands every id already stored, so anything
persisted against a default id -- a ledger row, a dedupe key, a link back to the
message -- points at nothing the moment the message is filed. This preference
makes Graph mint ids that survive moves.

It lives on the CLIENT, not on the call sites, because a preference applied
per-request is a preference someone forgets: one un-prefixed request is enough
to mint a folder-dependent id and reintroduce the whole defect. Setting it once
where the transport is constructed makes it unforgettable by construction.
"""


class Refreshable(Protocol):
    """The narrow slice of the Graph adapter this module is allowed to touch.

    A Protocol rather than an import of ``OutlookProvider``, because importing
    the provider here would make the dependency circular -- ``outlook`` already
    imports this module. Stating the three members as a structural type keeps
    the arrow pointing one way AND documents the entire surface: this module
    can clear the cached credential and ask for a new one, and it can do
    nothing else to the adapter.
    """

    _token: str | None
    _client: httpx.Client | None

    def authenticate(self, interactive: bool = False) -> str:
        """Acquire a credential. MUST be able to run with no terminal."""
        ...


def graph_client(provider: Refreshable, base_url: str, timeout: httpx.Timeout) -> httpx.Client:
    """The adapter's transport, built on first use and cached on the provider.

    Lives beside ``refresh_credential`` because the two are one decision seen
    from both ends: this function is what BAKES the access token into an
    ``Authorization`` header, which is the entire reason a refresh has to
    invalidate the client rather than merely replace the token. Split across
    two files, that coupling is invisible and the next edit breaks it.

    An INJECTED client is returned untouched and never rebuilt. That is what
    lets the whole Graph test suite run against ``httpx.MockTransport`` with no
    credential and no network, and it is also why a refresh clears the field:
    once cleared, the next call lands here and builds a real one.
    """
    if provider._client is not None:
        return provider._client
    if not provider._token:
        provider.authenticate(interactive=False)
    provider._client = httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {provider._token}", "Prefer": IMMUTABLE_ID_PREFER},
        timeout=timeout,
    )
    return provider._client


def refresh_credential(provider: Refreshable) -> None:
    """Drop the cached credential and mint a fresh one, unattended.

    BOTH the token and the client are dropped, and dropping the client is the
    load-bearing half: the access token is baked into an ``Authorization``
    header when the client is CONSTRUCTED, so a client that survives the
    refresh keeps sending the expired token no matter how fresh the stored
    token becomes. Clearing it forces the adapter's transport factory to
    rebuild the header from the new value.

    ``interactive=False`` is not a default that happens to be right here -- it
    is the contract. This runs from inside a failed request, which under the
    watcher loop and the MCP server means no terminal is attached, so a
    device-code prompt would hang the caller forever with no signal. The
    adapter answers that combination with ``AuthError`` instead.
    """
    provider._token = None
    provider._client = None
    provider.authenticate(interactive=False)


_REFRESH_STATE: WeakKeyDictionary[Any, tuple[threading.Lock, list[int]]] = WeakKeyDictionary()
"""Per-provider refresh lock and generation counter, keyed by the provider itself.

WHY PER PROVIDER AND NOT ONE GLOBAL LOCK
    The thing being protected is one adapter's credential -- its token, its
    cached client, and the keychain entry keyed by the mailbox it signed in as.
    Two providers watching two mailboxes share none of that, so a single global
    lock would serialize refreshes that never conflict and make a slow Entra
    round trip for one mailbox stall the other.

WHY WEAK KEYS
    The map must not be what keeps a provider alive. A strong dict here would
    pin every adapter ever constructed -- including the throwaway ones tests
    build by the hundred -- for the life of the process, which is a leak in
    exactly the module that runs on the long-lived watcher path.

WHY A GENERATION COUNTER AND NOT JUST THE LOCK
    A lock alone makes the refreshes SEQUENTIAL, not single-flight: eight
    threads would still perform eight sign-ins, one after another, which is
    the same N round trips and the same last-writer-wins keychain race, only
    slower. The counter is what lets a thread that waited observe that the
    credential it was about to refresh has ALREADY been refreshed since it saw
    the 401, and go straight to its retry.

    Held as a one-element list because the value is mutated by whoever holds
    the lock and the map entry itself is never rebound; an ``int`` would have
    to be written back into the dict, which is a second mutation and a second
    chance to race.
"""

_REFRESH_STATE_GUARD: Final = threading.Lock()
"""Guards insertion into ``_REFRESH_STATE`` only, never a refresh itself.

Two threads hitting the first 401 for the same provider must not each create
their own lock -- two locks protecting one credential protect nothing. This one
is held for the duration of a dict lookup and is never held across
``authenticate``, so it cannot become the global serialization point the
per-provider design exists to avoid.
"""


def _refresh_state(provider: Refreshable) -> tuple[threading.Lock, list[int]]:
    """The lock and generation counter for one provider, created on first need."""
    with _REFRESH_STATE_GUARD:
        state = _REFRESH_STATE.get(provider)
        if state is None:
            state = (threading.Lock(), [0])
            _REFRESH_STATE[provider] = state
        return state


def refresh_credential_once(provider: Refreshable, seen: int) -> None:
    """Refresh this provider's credential unless somebody already did (#44).

    Single-flight, not merely mutually exclusive. ``seen`` is the generation
    the caller read BEFORE it made the request that came back 401; if the
    counter has moved since, another thread minted the fresh credential while
    this one was blocked and repeating the work would buy nothing but a second
    Entra round trip and a second keychain write.

    Args:
        provider: The adapter whose credential is refreshed.
        seen: The generation from ``_refresh_state`` read before the attempt.
    """
    lock, generation = _refresh_state(provider)
    with lock:
        if generation[0] != seen:
            return
        refresh_credential(provider)
        generation[0] += 1


def _is_unauthorized(error: BaseException) -> bool:
    """True only for a Graph failure that carried status 401.

    Read off ``GraphStatusError.status``, the attribute that subclass exists to
    carry, and NEVER off the message text. The bounded message is assembled
    from server-supplied fragments, so matching prose in it would make this
    branch depend on wording Microsoft is free to change and on content an
    intermediary proxy can influence.
    """
    return isinstance(error, GraphStatusError) and error.status == UNAUTHORIZED


def with_reauth_once[Result](
    provider: Refreshable,
    operation: str,
    send: Callable[[], Result],
) -> Result:
    """Run ``send``; on a 401, refresh the credential once and run it again.

    Args:
        provider: The adapter whose credential is refreshed, via
            ``refresh_credential``. Taken as the narrow ``Refreshable``
            protocol, so this function can reach the token, the cached client,
            and ``authenticate`` -- and nothing else on the adapter.
        operation: The adapter-level operation name, for the log line only. It
            reaches a log and nothing else -- never a URL, a filter, or a
            header -- so it carries no injection surface.
        send: The whole Graph call, INCLUDING building the transport. It is
            re-invoked verbatim on the retry, which is what makes the second
            attempt pick up the client the refresh rebuilt; a caller that
            captured a client OUTSIDE this callable would resend the expired
            token and see the same 401 twice.

    Returns:
        Whatever ``send`` returns, from either the first or the second attempt.

    Raises:
        GraphStatusError: The second 401, propagated unchanged so a genuinely
            dead credential fails loud rather than being retried forever.
        Exception: Anything else ``send`` raises, on the first attempt,
            untouched. Only a 401 is treated as a stale-token symptom.

    The refresh is SINGLE-FLIGHT per provider (#44). An access token expires
    for every in-flight request at the same instant, so concurrent callers 401
    together; without this, each ran its own sign-in and each wrote the shared
    keychain entry. The generation is read BEFORE the attempt, not after the
    failure, because a refresh that completed while this request was on the
    wire is precisely the one this caller must not repeat.
    """
    seen = _refresh_state(provider)[1][0]
    try:
        return send()
    except GraphStatusError as first:
        if not _is_unauthorized(first):
            raise
        logger.info(f"GRAPH: token expired, re-authenticating operation={operation}")
        refresh_credential_once(provider, seen)
        return send()


def account_name(result: dict[str, Any], *, account: dict[str, Any] | None = None) -> str:
    """Which mailbox was authenticated: id-token claims first, cached account second.

    Lives HERE because it reads the msal sign-in result this module's
    credential lifecycle owns; the provider calls it at the end of
    ``authenticate`` and keeps no claim-reading logic of its own.

    Three keys are tried in order because Entra populates different ones per
    account type: ``preferred_username`` for most work and school accounts,
    ``email`` for personal Microsoft accounts, ``upn`` for older tenants. The
    order is most-to-least reliable, not preference.

    ``account`` is the msal cache entry the silent path authenticated AS
    (``app.get_accounts()[0]``). A silent ``acquire_token_silent`` response
    carries no ``id_token_claims`` at all (observed live 2026-08-22, #114), so
    without this fallback every re-run after the first consent reported
    ``unknown`` for a credential that was bound to a known mailbox. Its
    ``username`` is the UPN msal itself indexes the cache by -- the same value
    ``preferred_username`` carries on the interactive path.

    ``"unknown"`` rather than a raise when neither is present: this is a display
    label on a successful sign-in, so failing the whole authentication over a
    missing claim would trade a working credential for a cosmetic gap. Only the
    claims and the account label are read -- the access token itself is never
    inspected here.
    """
    claims = result.get("id_token_claims") or {}
    for key in ("preferred_username", "email", "upn"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    username = (account or {}).get("username")
    if isinstance(username, str) and username:
        return username
    return UNKNOWN_ACCOUNT


def restore_credential(msal: Any) -> tuple[Any, Any, dict[str, Any] | None]:
    """Rebuild the msal app from the stored cache and choose THIS install's account.

    The read half of the pair ``persist_credential`` closes: it deserializes
    the keychain blob into a fresh cache, constructs the public client over it,
    and resolves which cached account the silent path may authenticate as.

    ``msal`` arrives as a parameter rather than being imported here for the
    reason the caller imports it function-locally -- it is a heavy dependency
    only the auth path needs, and a module-scope import would cost every CLI
    invocation and every test that merely injects a transport. Passing it also
    keeps the caller's monkeypatch of ``msal`` effective, which is what the
    provider suite authenticates against.

    A stored payload whose ``cache`` is not a string is ignored rather than
    refused: that is a credential written by a version that shaped it
    differently, and the correct outcome is a fresh interactive sign-in, not a
    dead install.

    Returns:
        ``(cache, app, account)``. ``account`` is ``None`` only when the cache
        holds no accounts at all, which is the caller's signal to go
        interactive.

    Raises:
        AuthError: The cache cannot be resolved to one account. See
            ``pinned_account``.
    """
    client_id, authority = microsoft_client_config()
    cache = msal.SerializableTokenCache()
    stored = load_token(PROVIDER_KEY)
    if stored and isinstance(stored.get("cache"), str):
        cache.deserialize(stored["cache"])
    app = msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)
    return cache, app, pinned_account(app.get_accounts(), (stored or {}).get("account"))


def persist_credential(cache: Any, signed_in_as: str) -> None:
    """Write the msal cache back, pinning the account it authenticated as (#149).

    Only when msal reports the cache CHANGED: an unchanged cache is a keychain
    write that would rewrite the same bytes, and the write is the one operation
    here that can fail on a locked keychain.

    The UPN rides beside the cache blob rather than in a config file. The pin is
    only meaningful for as long as the credential it names exists, and the
    keychain entry is already scoped per ``install_id`` -- storing it here gives
    it that lifetime and that scope without a second file to keep in sync.

    The UPN also NAMES the keychain entry (#53). An install watching two
    Microsoft mailboxes writes two caches, and a name keyed by provider alone
    would let the second sign-in overwrite the first mailbox's cache -- so the
    account this sign-in actually authenticated as is what the write is keyed
    by. ``UNKNOWN_ACCOUNT`` again does not qualify: with no mailbox name there
    is nothing to key by, so the write falls to the install's default account,
    which is the same entry a single-account install has always used.

    ``UNKNOWN_ACCOUNT`` is never stored. It is the label for "no claim and no
    cache entry named a mailbox", and persisting the literal string would pin
    this install to an account name that matches nothing, so the NEXT run would
    refuse a credential that works. A missing pin is recoverable; a wrong one
    is not.

    Args:
        cache: The ``msal.SerializableTokenCache`` the sign-in ran against.
        signed_in_as: The label ``account_name`` returned for this sign-in.
    """
    if not cache.has_state_changed:
        return
    payload: dict[str, Any] = {"cache": cache.serialize()}
    account: str | None = None
    if signed_in_as != UNKNOWN_ACCOUNT:
        payload["account"] = signed_in_as
        account = signed_in_as
    store_token(PROVIDER_KEY, payload, account=account)


def pinned_account(accounts: list[dict[str, Any]], pin: object) -> dict[str, Any] | None:
    """Which cached msal account this install signs in as. Never ``accounts[0]``.

    The msal token cache is a LIST whose order belongs to msal, not to this
    install. One machine, one registration, and two Microsoft sign-ins -- a
    personal account beside a work one, or two tenants -- puts two entries in
    it, and taking element zero made the mailbox this connector read, filed,
    and drafted in a function of cache order. It changed silently, with no
    error and a label that moved underneath the operator (#149).

    ``pin`` is the UPN stored beside the cache blob at the sign-in that
    established the credential. Matching is case-insensitive because Entra
    treats a UPN that way and returns whichever casing the account was created
    with, which is not necessarily the casing the operator typed. It is also
    STRIPPED, on both sides of the comparison: a stored value carrying a
    trailing newline still names the mailbox it names, and a value that is
    nothing but whitespace names none at all and is treated as no pin rather
    than as a pin that matches nothing.

    An UNPINNED install -- a credential stored before the pin existed -- keeps
    working off a one-account cache, because there is exactly one answer and
    refusing it would break every install that upgraded. Two accounts and no
    pin is a genuine ambiguity, and it refuses: guessing is what this function
    exists to stop.

    BOTH REFUSALS PRESCRIBE ``logout`` FIRST, AND #190 IS WHY. They used to say
    "run auth microsoft", which is a loop rather than a recovery:
    ``OutlookProvider.authenticate`` restores the stored credential BEFORE it
    reaches the interactive branch, so the prescribed command re-enters this
    function against the same cache and raises the identical error. The two
    escapes that actually work are clearing the credential first
    (``logout microsoft``, then ``auth microsoft``) or namespacing a new
    keychain key with a fresh ``INBOX_ZERO_INSTALL_ID``, and they are not
    substitutes -- logout throws the old credential away, the install id keeps
    it and watches both mailboxes -- so each message names both.

    Args:
        accounts: ``app.get_accounts()``, exactly as msal returned it.
        pin: The stored UPN, or anything falsy, blank, or non-string for no pin.

    Returns:
        The account to pass to ``acquire_token_silent``, or ``None`` when the
        cache is empty -- which is the caller's signal to go interactive.

    Raises:
        AuthError: The pin names an account the cache does not hold, or the
            cache is ambiguous and nothing pins it. The message names the
            wanted UPN and the ones present, because the operator's fix
            depends on which of the two it is.
    """
    if not accounts:
        return None
    present = [str(entry.get("username", "")) for entry in accounts]
    wanted = pin.strip() if isinstance(pin, str) else ""
    if wanted:
        for entry, username in zip(accounts, present, strict=True):
            if username.strip().casefold() == wanted.casefold():
                return entry
        raise AuthError(
            f"The stored Microsoft credential for this install names {wanted}, but the "
            f"token cache holds {', '.join(present) or 'no accounts'}. This install "
            "will not sign in as a mailbox it was not bound to. ACTION REQUIRED: run "
            "'uv run inbox-zero-connector logout microsoft' FIRST, then "
            f"'uv run inbox-zero-connector auth microsoft' and sign in as {wanted}. "
            "Running auth without the logout re-reads this same credential and "
            "repeats this error. To keep this credential and watch the other mailbox "
            "as well, set INBOX_ZERO_INSTALL_ID to a new value and auth under that."
        )
    if len(accounts) > 1:
        raise AuthError(
            f"The Microsoft token cache holds {len(accounts)} accounts "
            f"({', '.join(present)}) and this install has no stored account to "
            "choose between them, so signing in silently would pick one at random. "
            "ACTION REQUIRED: run 'uv run inbox-zero-connector logout microsoft' "
            "FIRST, then 'uv run inbox-zero-connector auth microsoft' and sign in as "
            "this install's mailbox, which records the choice. Running auth without "
            "the logout re-reads this same cache and repeats this error. To keep this "
            "credential and watch the other mailbox as well, set INBOX_ZERO_INSTALL_ID "
            "to a new value and auth under that."
        )
    return accounts[0]
