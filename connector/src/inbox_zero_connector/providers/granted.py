"""Reading the GRANTED scope list off a live credential, one reader per vendor.

=============================================================================
THE REQUEST SITS ONE ATTRIBUTE AWAY FROM THE ANSWER. NEVER READ THE REQUEST.
=============================================================================

``scopes`` declares what this connector ASKS FOR. This module reads what the
authorization server ANSWERED, off the token response, for ``grants.discover``
to interpret. The two are routinely different -- an Entra admin consents to a
subset, a Workspace admin grants a service account less, a user un-ticks a
permission on Google's granular consent screen -- and in every one of those
cases the request is unchanged while the answer shrinks.

Both vendors put the answer and the ask within one attribute of each other,
which is what makes this worth its own module rather than two inline
expressions:

    google-auth   Credentials.scopes           the REQUEST
                  Credentials.granted_scopes   the ANSWER
    msal          the ``scopes`` argument      the REQUEST
                  response["scope"]            the ANSWER

Reading the near one would report our own ask back as the tenant's answer --
silently, plausibly, and precisely in the case the discovery layer exists to
catch. Keeping both readers here puts that distinction in one reviewable place
instead of once per adapter.

UNDETERMINED IS NOT EMPTY. Every function here returns ``None`` for "no grant
could be read" and a tuple for "this is the grant, possibly empty". Neither
falls back to the requested set, because that fallback is exactly how an ask
gets laundered into the record as an answer. ``grants`` fails closed on both
but reports them differently, and only one of them means something is broken.

NOTHING IS NORMALIZED HERE. Scope strings are returned verbatim, in the
vendor's own spelling. ``grants._normalize`` owns interpretation; stripping a
resource prefix here would put scope parsing in two modules and leave the
security verdict deriving from a string an adapter had already edited.
"""

from __future__ import annotations

from typing import Any


def from_msal_result(result: dict[str, Any]) -> tuple[str, ...] | None:
    """Read the granted scope list out of an msal token response.

    msal writes the authorization server's answer to the ``scope`` key and
    treats it as SPACE-DELIMITED (``msal/application.py:1457,1733``, both
    spelled ``response["scope"].split()``). It arrives as a plain string, so
    splitting it is reading the documented format rather than parsing anything.

    A response with NO ``scope`` key returns None, not ``()``. msal itself
    falls back to the requested scopes in that case; this function deliberately
    does not.

    Values come back fully qualified (``<resource-uri>/Mail.ReadWrite``) and
    are returned that way. ``grants._normalize`` strips the prefix.

    Args:
        result: The msal token response.

    Returns:
        The granted permissions verbatim, or None if the response named none.
    """
    scope = result.get("scope")
    if isinstance(scope, str):
        return tuple(scope.split())
    if isinstance(scope, (list, tuple)):
        return tuple(str(item) for item in scope)
    return None


def from_google_credentials(creds: Any) -> tuple[str, ...] | None:
    """Read the granted scope list off a google-auth credential.

    Reads ``granted_scopes`` and nothing else. google-auth populates it ONLY
    from a token response -- at ``google/oauth2/credentials.py:454`` from
    ``grant_response["scope"]`` on refresh, and at
    ``google_auth_oauthlib/helpers.py:148`` from ``session.token.get("scope")``
    after a fresh consent.

    A RESTORED CREDENTIAL HAS NO GRANT, AND None IS THE HONEST ANSWER.
    ``Credentials.from_authorized_user_info(payload, requested)`` assigns the
    requested list to ``scopes`` and leaves ``granted_scopes`` as None. The
    keychain payload's own ``scopes`` key is not a substitute: that constructor
    consults it only when the caller passes ``scopes=None``
    (``credentials.py:505``), and the value stored there was itself
    ``creds.scopes`` -- the request, carried through. There is no trustworthy
    grant on that path, so this returns None rather than a plausible guess.

    Args:
        creds: A google-auth credential, fresh or restored.

    Returns:
        The granted scopes verbatim, or None when the credential carries none.
    """
    granted = getattr(creds, "granted_scopes", None)
    if granted is None:
        return None
    return tuple(str(scope) for scope in granted)


def creds_to_payload(creds: Any) -> dict[str, Any]:
    """Serialize a credential for the OS keychain.

    Lives here rather than in the adapter because its ``scopes`` key is the
    exact trap this module exists to document: the name reads like a grant and
    the value is the request. Keeping the writer beside
    ``from_google_credentials`` means the two cannot drift into disagreeing
    about which of the two attributes is evidence of anything.

    Args:
        creds: The credential to persist.

    Returns:
        The payload ``auth.store_token`` writes. Contains a refresh token and
        a client secret, so it never reaches a log line.
    """
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        # NOT the grant, despite the name, and this comment used to claim
        # otherwise. `creds.scopes` is the REQUESTED list: on a restored
        # credential `_credentials` passes `list(self.scopes)` into
        # `from_authorized_user_info`, which assigns it verbatim, so what
        # round-trips through the keychain is our own ask. It is kept because
        # google-auth needs a scope list to rebuild the credential and to
        # detect a scope change on refresh -- NOT as evidence of a grant.
        # `granted_scopes` is the answer and is deliberately not persisted:
        # a stored copy would age into a stale claim about live permissions,
        # and `GmailProvider.granted_scopes` reports None on this path instead.
        "scopes": list(creds.scopes or ()),
    }
