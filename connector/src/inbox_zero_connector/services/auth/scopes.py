"""OAuth scope constants for every provider this connector talks to.

THIS MODULE IS THE SECURITY MANIFEST. It is the single place in the codebase
where an OAuth scope string may be defined, and it is the target of an
automated scan (``tests/test_security_surface.py``) that fails the build if a
send-capable scope string appears anywhere in ``src/``.

No other module may define, concatenate, or otherwise construct a scope
string. Providers import from here and pass the tuple through unchanged.

--------------------------------------------------------------------------
SCOPE POSTURE (Stage 2: triage + voice sampling + reply drafts)
--------------------------------------------------------------------------

The requested set depends on the provider, on whether the installer enabled
DRAFTS (``config.drafts_enabled``), and -- for Gmail -- on the AUTH MODE.

Gmail, interactive, either way -> {gmail.modify}                     CODE-LAYER
Gmail, DWD, drafts OFF  -> {gmail.readonly, gmail.modify.restricted} SCOPE-LAYER
Gmail, DWD, drafts ON   -> the pair plus gmail.compose               CODE-LAYER
Graph, either way       -> {Mail.ReadWrite}                          SCOPE-LAYER

The Gmail auth-mode split was forced by a LIVE consent screen on 2026-08-18:
``gmail.modify.restricted`` is DWD-ONLY. Google's users.messages.modify
reference documents it solely for "a service account that has domain-wide
delegation authority", and an interactive consent request for it is refused
with ``Error 400: invalid_scope`` ("Some requested scopes cannot be shown").
Until the DWD service-account flow is implemented in ``auth``, interactive is
the only mode that RUNS, and its honest posture is code-layer: the one scope
that lets a consent-flow token file mail (``gmail.modify``) is also accepted
by Gmail's send methods.

The Gmail split is forced by Google, not chosen by us: ``users.drafts.create``
and ``users.drafts.send`` accept an IDENTICAL scope set, so a Gmail token that
can put a draft in the Drafts folder can also send that draft. There is no
compose-without-send scope in the Gmail API. Enabling Gmail drafts therefore
DOWNGRADES the Gmail guarantee from "the provider's servers refuse it" to
"this connector has no code path that does it" -- see NO_SEND_ENFORCEMENT.

Graph is unaffected. ``Mail.ReadWrite`` covers draft creation (including
``createReply``) and is accepted by neither send path, so drafts on Microsoft
keep the scope-layer guarantee. That asymmetry is real; do not flatten it in
buyer copy.

Verified 2026-08-17 against the per-method "Authorization scopes" sections of
the vendor REST references, not against summary/guide prose. Google's own
labels *guide* is misleading on this point (it reads as though ``gmail.labels``
covers label assignment on messages); the per-method reference contradicts it
and the reference wins.
"""

from __future__ import annotations

from typing import Final, Literal

from loguru import logger

# ---------------------------------------------------------------------------
# Google / Gmail
# ---------------------------------------------------------------------------

GMAIL_READONLY: Final = "https://www.googleapis.com/auth/gmail.readonly"
"""Read messages, threads, and labels.

Accepted by users.messages.get / users.messages.list / users.labels.list.
https://developers.google.com/workspace/gmail/api/v1/reference/users/messages/get
https://developers.google.com/workspace/gmail/api/v1/reference/users/messages/list

NOT accepted by users.messages.send or users.drafts.send. A token bearing only
this scope is rejected by Gmail's send API server-side.
"""

GMAIL_MODIFY_RESTRICTED: Final = "https://www.googleapis.com/auth/gmail.modify.restricted"
"""Add and remove labels on messages (file, archive, star, mark read).

Accepted by users.messages.modify and users.messages.batchModify.
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/batchModify

This is the load-bearing discovery for the kit's headline claim. It is the
ONLY Gmail mutation scope that is not also accepted by a send method:
users.messages.send accepts exactly {mail.google.com, gmail.modify,
gmail.compose, gmail.send} and this scope is on none of those lists.
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
https://developers.google.com/workspace/gmail/api/auth/scopes

Three operational caveats, all from the official docs:

1. users.messages.modify documents this scope as the domain-wide-delegation
   path -- "requests require authorization with a service account that has
   domain-wide delegation authority to impersonate users with the
   gmail.modify.restricted scope". It is a Workspace admin arrangement, not a
   plain end-user consent flow. PROVEN LIVE 2026-08-18: an interactive consent
   request including this scope is refused by Google's consent screen with
   ``Error 400: invalid_scope`` before any sign-in completes. This scope is
   usable ONLY by the (not yet implemented) DWD service-account flow; never
   put it in an interactive request. Deployment guidance is in the README.
2. It is a RESTRICTED scope: it requires restricted-scope OAuth verification,
   and a third-party security assessment if data is stored or transmitted
   server-side. This connector stores nothing server-side (see README).
3. users.threads.modify does NOT list it. Thread-level labelling must be done
   message-by-message through users.messages.modify / batchModify. The Gmail
   provider honours this; do not "optimise" it into a threads.modify call.
"""

GMAIL_DWD_TRIAGE_SCOPES: Final[tuple[str, ...]] = (
    GMAIL_READONLY,
    GMAIL_MODIFY_RESTRICTED,
)
"""What a DWD service account is granted when Gmail drafts are DISABLED.

no-send enforcement for Gmail in this posture: SCOPE LAYER (structural).
A token minted for this set is not accepted by users.messages.send,
users.drafts.create, or users.drafts.send. Sending is refused by Google's
servers, not by this connector's code.

REACHABLE ONLY VIA DWD, and the DWD service-account flow is NOT YET
IMPLEMENTED in ``auth`` -- today this set documents the target posture of the
assisted install rather than anything the shipped code can request. The
interactive flow must never request it: proven refused live 2026-08-18.
"""

GMAIL_SEND_WOULD_REQUIRE: Final[tuple[str, ...]] = (
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
)
"""The four scopes users.messages.send accepts.

Recorded here so the gap is auditable rather than implicit. Two of the four
(mail.google.com, gmail.send) are never requested under any configuration.
The SECOND entry, gmail.modify, is the interactive flow's one scope -- see
GMAIL_INTERACTIVE_SCOPES. The THIRD, gmail.compose, is the DWD drafts
conditional -- see GMAIL_COMPOSE.

users.drafts.create and users.drafts.send accept the SAME three scopes
(mail.google.com, gmail.modify, gmail.compose). Any Gmail token that can
create a draft can send that draft. There is no compose-without-send scope in
the Gmail API.
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/create
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/send
"""

GMAIL_COMPOSE: Final = GMAIL_SEND_WOULD_REQUIRE[2]
"""The Gmail drafts scope -- and it is SEND-CAPABLE. Read this before using it.

Deliberately defined by INDEXING INTO the send-capable set rather than by
writing the literal again, because that is the honest shape of the fact: the
only Gmail scope that can create a draft is a member of the set that can send
one. Restating the string would let the two drift apart in a diff; this cannot.

Requested ONLY when the installer sets ``drafts_enabled`` true for Google
under DWD. The moment it is requested, the Gmail no-send guarantee stops being
structural and becomes a property of this codebase -- NO_SEND_ENFORCEMENT
reports "code-layer" for google in that configuration, and buyer copy must
say so.
"""

GMAIL_MODIFY: Final = GMAIL_SEND_WOULD_REQUIRE[1]
"""Bare ``gmail.modify`` -- send-capable, and the interactive flow's one scope.

Defined by INDEXING into the send-capable set, exactly like ``GMAIL_COMPOSE``
above and for the same reason: the only interactive scope that can file mail
is a member of the set that can send it, and restating the string would let
that fact drift out of a diff. The literal-scan test enforces this shape --
no send-scope literal exists outside GMAIL_SEND_WOULD_REQUIRE.
"""

GMAIL_INTERACTIVE_SCOPES: Final[tuple[str, ...]] = (GMAIL_MODIFY,)
"""What the interactive consent flow requests. One scope, either drafts mode.

``gmail.modify`` covers every call this connector makes -- messages.list/get,
threads.get, labels.list/create, messages.modify, drafts.create -- so the
consent screen shows a single permission. It is also accepted by Gmail's send
methods, which is why this posture is CODE-LAYER and is reported as such:
Google sells no interactive scope that can file mail but not send it.
"""

# WHY GMAIL DRAFTS COST THE SCOPE-LAYER CLAIM, in one place, permanently.
#
# users.drafts.create and users.drafts.send accept an IDENTICAL scope set
# {mail.google.com, gmail.modify, gmail.compose}. Verified 2026-08-17 against
# the per-method "Authorization scopes" section of each reference, not summary
# prose. There is no compose-without-send scope, so "can create a draft but
# cannot send it" is not expressible as a Gmail token.
#
# With drafts on, the Gmail guarantee rests on THREE walls, none of them the
# token:
#   1. code    -- no send code path exists in this package (scanned, see
#                 tests/test_security_surface.py and the standalone
#                 _graph/done-means/inbox-zero-no-send.sh).
#   2. tenant  -- Workspace per-app access control denying send to this app,
#                 a standard onboarding step in the assisted install.
#   3. config  -- the installer can decline drafts entirely and keep the
#                 Stage 1 scope-layer posture (drafts_enabled = false).
#
# Microsoft is unaffected: Graph's Mail.ReadWrite covers drafts including
# createReply and is accepted by neither send path, so Graph keeps the
# scope-layer guarantee with drafts on.

# ---------------------------------------------------------------------------
# Microsoft / Graph
# ---------------------------------------------------------------------------

GRAPH_MAIL_READWRITE: Final = "Mail.ReadWrite"
"""Read, move, and flag mail in the signed-in user's mailbox.

Accepted by GET /me/messages, POST /me/messages/{id}/move, and
PATCH /me/messages/{id} (which documents isRead, flag, importance, and
categories as updatable on a non-draft message).
https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0
https://learn.microsoft.com/en-us/graph/api/message-move?view=graph-rest-1.0
https://learn.microsoft.com/en-us/graph/api/message-update?view=graph-rest-1.0

Microsoft's permissions reference states the send exclusion in the
permission's own description, verbatim: "Allows the app to create, read,
update, and delete email in user mailboxes. Does not include permission to
send mail."
https://learn.microsoft.com/en-us/graph/permissions-reference

CAUTION when re-verifying: an AI summarisation pass over that reference once
rendered this description as "...and send mail on behalf of the user", which
is the opposite of the source text. Read the raw doc, never a summary.

Honest framing: this is whole-mailbox read/write. Graph has no folder-scoped
OAuth permission -- even the folder-scoped POST /me/mailFolders/{id}/messages
requires full Mail.ReadWrite. The defensible claim is "cannot send", NOT
"narrowly scoped".
"""

GRAPH_SCOPES: Final[tuple[str, ...]] = (GRAPH_MAIL_READWRITE,)
"""The exact set of scopes this connector requests from Microsoft. Nothing else.

no-send enforcement for Graph: SCOPE LAYER (structural, for the issued token).
Both send paths -- POST /me/sendMail and POST /me/messages/{id}/send -- list
Mail.Send as least-privileged with higher-privileged "Not available".
Mail.ReadWrite does not satisfy either.

One caveat that must not be dropped from buyer copy: delegated permissions
support DYNAMIC consent, so omitting Mail.Send from the app registration is
not by itself an unbypassable barrier. The token THIS connector holds
genuinely cannot send; the same client could request a different token that
can. Say "the token this kit uses cannot send", never "sending is impossible".
Tenant-side hardening (Entra consent policy, Exchange ApplicationAccessPolicy)
is what makes it structural, and it is an onboarding step, not a product
default. See the connector README.
"""

GRAPH_SEND_WOULD_REQUIRE: Final[tuple[str, ...]] = ("Mail.Send",)
"""Documentation only -- the permission both Graph send paths require.

Deliberately NOT requested. Inert data, referenced by nothing but docs.
https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0
https://learn.microsoft.com/en-us/graph/api/message-send?view=graph-rest-1.0
"""

# ---------------------------------------------------------------------------
# Enforcement posture, machine-readable
# ---------------------------------------------------------------------------

SCOPE_LAYER: Final = "scope-layer"
"""The provider's own servers reject a send call made with the token we hold."""

CODE_LAYER: Final = "code-layer"
"""Only this connector's code prevents a send. A software control, and weaker.

It must be labelled as such wherever it is claimed. A code-layer guarantee is
still a real guarantee -- the code path does not exist and a lexical scan
proves it every build -- but it is our word plus a scanner, not Google's
server saying no.
"""

PROVIDERS: Final[tuple[str, ...]] = ("google", "microsoft")


GmailAuthMode = Literal["interactive", "dwd"]
"""How the Gmail token is obtained. Decides which scope set is even legal.

``interactive`` is the only mode the shipped ``auth`` command implements.
``dwd`` names the designed assisted-install posture (service account with
domain-wide delegation); selecting it before that flow exists is refused by
``scopes_for`` rather than silently returning scopes nothing can use.
"""


def scopes_for(
    provider: str, drafts_enabled: bool = False, auth_mode: GmailAuthMode = "interactive"
) -> tuple[str, ...]:
    """Return the exact scope tuple for ``provider`` under this configuration.

    ``drafts_enabled`` is the installer's per-provider choice. Under Gmail
    DWD it adds ``GMAIL_COMPOSE`` (send-capable); under Gmail interactive it
    changes nothing, because ``gmail.modify`` already covers drafts.create.
    Graph ignores both knobs -- ``Mail.ReadWrite`` covers everything.

    Raises ``ValueError`` for an unknown provider rather than defaulting, so a
    typo can never silently produce an empty or broader scope request.
    """
    if provider == "google" and auth_mode == "interactive":
        selected = GMAIL_INTERACTIVE_SCOPES
    elif provider == "google":
        selected = (
            (*GMAIL_DWD_TRIAGE_SCOPES, GMAIL_COMPOSE) if drafts_enabled else GMAIL_DWD_TRIAGE_SCOPES
        )
    elif provider == "microsoft":
        selected = GRAPH_SCOPES
    else:
        raise ValueError(f"Unknown provider {provider!r}. Valid providers: google, microsoft.")
    # DEBUG shows the EXACT scope set requested for this configuration -- the
    # first thing an auditor asks for, straight from the deciding function.
    logger.debug(
        f"SCOPES: selected provider={provider} drafts_enabled={drafts_enabled} "
        f"auth_mode={auth_mode} scope_count={len(selected)} scopes={' '.join(selected)}"
    )
    return selected


def enforcement_for(
    provider: str, drafts_enabled: bool = False, auth_mode: GmailAuthMode = "interactive"
) -> str:
    """Where the no-send guarantee actually lives, for this exact configuration.

    This is the function buyer-facing copy must be generated from, and the
    reason the posture is a computed value rather than a hardcoded table: the
    honest answer depends on config, and a constant would go stale the first
    time somebody flipped it.
    """
    if provider == "google" and auth_mode == "interactive":
        # gmail.modify is the only interactive scope that can file mail, and
        # the send methods accept it. The token can send; only this codebase
        # (proven by the scanners) does not.
        layer = CODE_LAYER
    elif provider == "google":
        # DWD: requesting gmail.compose is requesting a scope the send methods
        # accept. Nothing below the token can restore the structural claim.
        layer = CODE_LAYER if drafts_enabled else SCOPE_LAYER
    elif provider == "microsoft":
        # Mail.ReadWrite covers drafts and is refused by both send paths.
        layer = SCOPE_LAYER
    else:
        raise ValueError(f"Unknown provider {provider!r}. Valid providers: google, microsoft.")
    logger.debug(
        f"SCOPES: enforcement provider={provider} drafts_enabled={drafts_enabled} "
        f"auth_mode={auth_mode} layer={layer}"
    )
    return layer


def no_send_enforcement(drafts_enabled_by_provider: dict[str, bool]) -> dict[str, str]:
    """The whole posture table, computed from the installed configuration."""
    return {
        name: enforcement_for(name, drafts_enabled_by_provider.get(name, False))
        for name in PROVIDERS
    }


def __getattr__(name: str) -> dict[str, str]:
    """Compute ``NO_SEND_ENFORCEMENT`` on first access rather than at import.

    The default posture -- drafts off everywhere, scope-layer on both providers
    -- is what a deployment reports before the installer enables anything. It is
    NOT a claim about a running deployment; call ``enforcement_for`` with that
    deployment's real ``drafts_enabled`` value for that.

    It is computed LAZILY because ``enforcement_for`` logs, and a module-level
    call runs before ``logging_config.setup`` has installed any sink. Those
    records went to loguru's DEFAULT stderr handler at DEBUG, which no setting
    of ours reaches -- ``logging.level`` and ``logging.console_level`` both
    configure sinks that do not exist that early. The symptom was two DEBUG
    lines above the output of every CLI command, including on an install whose
    console is deliberately quiet. Module attribute access (PEP 562) keeps the
    public name and the value identical while moving the call past startup.
    """
    if name == "NO_SEND_ENFORCEMENT":
        return no_send_enforcement(dict.fromkeys(PROVIDERS, False))
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
