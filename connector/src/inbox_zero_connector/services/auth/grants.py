"""What the ACCOUNT actually granted, discovered from the live credential.

=============================================================================
REQUESTED IS OUR ASK. GRANTED IS THEIR ANSWER. THE GRANTED SET ALWAYS WINS.
=============================================================================

``scopes`` declares what this connector REQUESTS. That module is a statement of
intent, fixed at build time, identical on every install: it is the ask we put
on a consent screen or in an app registration. It is not evidence of anything.

THIS module answers the other half, and it is the half that decides what the
connector may do. An authorization server is free to answer an ask with less
than was asked for, and routinely does:

  - an Entra admin consents to a subset, or a Conditional Access / app-consent
    policy trims the request before the user ever sees it;
  - a Workspace admin grants a DWD service account fewer scopes than the client
    requested;
  - a user un-ticks a permission on Google's granular consent screen;
  - a previously-issued token is still in the cache from an OLDER, narrower ask
    and is replayed silently by ``acquire_token_silent``.

In every one of those cases the request is unchanged and the ANSWER is smaller.
Reading capability off the request would report a send path that the account
never granted, so capability is read off the ANSWER, here, and nowhere else.

WHY THIS INVERTS THE CONNECTOR'S ARCHITECTURE
    The connector was capability-limited BY CONSTRUCTION: the code decided what
    it would do and the scope request followed. That makes the operator's
    control indirect -- to change what the connector can do, they change our
    config and trust our code. This module makes the connector
    capability-limited BY THE ACCOUNT'S OWN GRANT: an MSP who grants only
    Mail.ReadWrite gets a connector with no send path, and gets it because
    their tenant said so, not because we agreed to it. They set the answer in
    their own admin console and they can verify it there, against their own
    audit log, without reading a line of this package.

    The two limits are not in competition and this module removes neither. The
    static declarations in ``scopes`` still bound what is ever ASKED FOR, the
    absent methods on ``providers.base`` still bound what is ever IMPLEMENTED,
    and this bounds what is PERMITTED. A capability must clear all three.

FAIL CLOSED, AND WHY THAT IS THE ONLY SAFE DIRECTION HERE
    The dangerous failure of this module is not refusing a capability the
    account granted -- that is visible immediately, as a feature that does not
    work, and an operator fixes it in their console. The dangerous failure is
    INFERRING a capability the account did not grant, because that is invisible
    until something acts on a mailbox with permission nobody approved.

    So absence of evidence is never permission. An unrecognized scope grants
    nothing. A missing, empty, or unreadable scope list grants nothing AND says
    so at ERROR, because a silently empty grant and a genuinely read-only
    account look identical downstream, and only one of them is a bug.

THE MAPPING IS A TABLE, ONE PER PROVIDER
    Two tables, because the vocabularies share nothing: Google issues URIs and
    Microsoft issues permission names. Adding a capability is one row, which is
    the point -- a chain of conditionals is where a scope quietly acquires a
    capability nobody reviewed, and a row is a diff a reviewer can read.

NOT WIRED TO ANYTHING. This module is discovery only. Nothing in the connector
gates on it yet; introducing a gate is a separate, deliberate change.

NO SCOPE STRING IS DEFINED HERE. Every key in both tables is imported from
``scopes``, which is the security manifest and the only module permitted to
define a scope literal (``tests/test_security_surface.py`` enforces it).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from loguru import logger
from pydantic import ConfigDict, ValidationError

from inbox_zero_connector.config import settings
from inbox_zero_connector.models.mail import ProviderModel
from inbox_zero_connector.services.auth.scopes import (
    GMAIL_MODIFY,
    GMAIL_MODIFY_RESTRICTED,
    GMAIL_READONLY,
    GRAPH_MAIL_READWRITE,
)
from inbox_zero_connector.utils import write_text_atomic


class Capability(StrEnum):
    """Everything the connector could conceivably do to a mailbox.

    A CLOSED SET, not a bag of booleans, and the closure is the point: a
    capability that is not named here cannot be granted, cannot be spelled at a
    call site, and cannot be typo'd into existence. ``mypy`` rejects
    ``Capability.SEND_ALL`` where a dict of ``{"send_all": True}`` would happily
    carry it to a caller that reads it as permission.

    THIS ENUM IS NOT THE INTERFACE, AND THE DIFFERENCE IS DELIBERATE.
    ``providers.base`` implements ten operations and deliberately implements no
    send, delete, or forward. This enum names those absent ones ANYWAY, because
    it describes what a CREDENTIAL permits, not what this package implements. A
    token that carries send permission is a fact about the account, and a
    discovery layer that had no word for it could not report it. Reporting
    ``SEND`` here is not a send path appearing -- no method exists to use it.
    That gap is exactly what makes the report worth reading: it is how an
    operator sees that their grant is wider than the connector needs.
    """

    READ = "read"
    """Read message headers, bodies, threads, and container listings."""

    LABEL = "label"
    """Add a label or category in place. Never moves, never removes."""

    MOVE = "move"
    """Relocate a message between folders, or take it out of the inbox."""

    DRAFT = "draft"
    """Create an unsent draft."""

    SEND = "send"
    """Transmit mail. NOT IMPLEMENTED ANYWHERE IN THIS PACKAGE."""

    REPLY = "reply"
    """Transmit a reply on an existing thread. Sending, so it implies SEND."""

    FORWARD = "forward"
    """Transmit an existing message to a new recipient. Sending."""

    DELETE = "delete"
    """Trash or permanently remove a message."""

    MANAGE_RULES = "manage_rules"
    """Create or alter server-side mail rules that act without the owner."""


# ---------------------------------------------------------------------------
# The tables. One row per scope. Adding a capability is a row, never a branch.
# ---------------------------------------------------------------------------

GOOGLE_SCOPE_CAPABILITIES: Final[dict[str, frozenset[Capability]]] = {
    GMAIL_READONLY: frozenset({Capability.READ}),
    GMAIL_MODIFY_RESTRICTED: frozenset({Capability.READ, Capability.LABEL, Capability.MOVE}),
    GMAIL_MODIFY: frozenset({
        Capability.READ,
        Capability.LABEL,
        Capability.MOVE,
        Capability.DRAFT,
        Capability.SEND,
        Capability.REPLY,
        Capability.FORWARD,
        Capability.DELETE,
    }),
}
"""Gmail scope -> what a token bearing it is permitted to do.

Derived from the per-method "Authorization scopes" sections of the Gmail REST
reference, the same sources ``scopes`` cites, never from the summary guide.

``gmail.modify`` is the wide row and it is wide HONESTLY. Google's send methods
accept it, so a token bearing it CAN send, reply, and forward, and it is also
accepted by ``users.messages.trash``. The interactive consent flow requests
exactly this scope because Google publishes no interactive alternative that can
file mail without also being able to send it. Reporting only the capabilities
we intend to use would be reporting the ask instead of the answer, which is the
one thing this module exists not to do.

``gmail.modify.restricted`` is the narrow mutation row: label and placement
changes with no send method accepting it. The two names differ by a suffix and
NOT by a little -- misreading one as the other is the single most consequential
mistake available in this file, which is why they are separate rows rather than
a prefix match.

MANAGE_RULES appears in no row. Gmail settings scopes are never requested by
this connector, so no scope it can hold grants it.
"""

MICROSOFT_SCOPE_CAPABILITIES: Final[dict[str, frozenset[Capability]]] = {
    GRAPH_MAIL_READWRITE: frozenset({
        Capability.READ,
        Capability.LABEL,
        Capability.MOVE,
        Capability.DRAFT,
        Capability.DELETE,
    }),
}
"""Graph permission -> what a token bearing it is permitted to do.

``Mail.ReadWrite`` grants read, categories (LABEL), /move (MOVE), the
reply-draft action (DRAFT), and -- stated plainly because it is true and
unflattering -- DELETE, since Microsoft documents the permission as "create,
read, update, and delete email in user mailboxes". Its description ends with
the exclusion this kit is built on, verbatim: "Does not include permission to
send mail."

So there is no SEND, REPLY, or FORWARD row reachable here. That is not a
posture we chose in this file; it is the consequence of Microsoft's permission
boundary, and it is why a Microsoft account granting only Mail.ReadWrite
produces a connector with no send path no matter what this package requests.
Mail.Send is deliberately absent from this table AND from the connector's
request: an account that granted it would present a scope this table does not
recognise, and the fail-closed rule would report it as granting nothing rather
than this module inventing a send capability.
"""

PROVIDER_TABLES: Final[dict[str, dict[str, frozenset[Capability]]]] = {
    "google": GOOGLE_SCOPE_CAPABILITIES,
    "microsoft": MICROSOFT_SCOPE_CAPABILITIES,
}
"""Provider name -> its own table. The dispatch, so ``discover`` has no branch."""

_GRAPH_RESOURCE_SEPARATOR: Final = "/"
"""What separates an Entra resource URI from the permission name it qualifies.

Entra returns a granted delegated permission fully qualified --
``<resource-uri>/Mail.ReadWrite`` -- while the app registration and
``scopes.GRAPH_MAIL_READWRITE`` name it bare. Matching the bare tail is how the
table keys stay identical to the constants ``scopes`` defines, instead of this
module restating a scope string it is forbidden to define.
"""


@runtime_checkable
class ScopeSource(Protocol):
    """What ``discover`` needs from a provider, and the whole of what it needs.

    A Protocol rather than the ``MailProvider`` ABC because discovery depends on
    exactly two things -- which provider this is, and what its live credential
    reports as granted -- and a signature that demanded the full ten-method
    interface would be claiming a dependency it does not have. It also means a
    test can hand this function a two-attribute object instead of standing up an
    adapter, so the fail-closed paths are testable without a credential.

    NOTE: no provider currently satisfies this Protocol. See the module's report
    and ``granted_scopes`` below -- wiring it is a separate change, by design.
    """

    name: str

    def granted_scopes(self) -> Any:
        """Return what the live credential reports as GRANTED, not requested."""


class Grant(ProviderModel):
    """What one live credential is permitted to do, plus the evidence for it.

    Frozen and ``extra="forbid"``, inherited from ``ProviderModel``: this is a
    security verdict crossing a boundary, and a field nobody declared is a field
    nobody reviewed. Frozen because a verdict that a caller can edit after the
    fact is not a verdict.

    THE RAW SCOPES ARE HERE ON PURPOSE, AND THEY ARE NOT DECORATION. A verdict
    with no evidence cannot be audited: an operator looking at ``capabilities``
    alone can see WHAT this module concluded but has no way to check WHETHER it
    concluded correctly, and their only recourse is to trust our table. With
    ``raw_scopes`` beside it they can diff the answer against their own admin
    console and catch a wrong row in this file. Keeping the derivation and its
    input together is what makes the claim checkable rather than assertable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    """Which provider answered. ``"google"`` or ``"microsoft"``."""

    capabilities: frozenset[Capability] = frozenset()
    """The union of every recognised scope's row. Empty is a valid answer.

    Empty means "this credential is permitted nothing this connector
    understands", which is the correct report for an unrecognised scope set and
    the mandatory report for an unreadable one. It is never a placeholder for
    "not determined yet" -- ``discover`` always returns a determined answer.
    """

    raw_scopes: tuple[str, ...] = ()
    """Exactly what the credential reported, verbatim and unsorted. THE EVIDENCE.

    Unrecognised scopes are kept here even though they granted nothing, because
    a scope this module could not map is precisely the thing an operator needs
    to see -- it is either a grant wider than we understand or a stale row in
    our table, and both are invisible if the evidence is filtered to what we
    recognised.
    """

    unrecognized: tuple[str, ...] = ()
    """The subset of ``raw_scopes`` that matched no row. Granted nothing.

    Separated out rather than left for a reader to compute, because the
    fail-closed rule is only auditable if the thing it silently dropped is
    named. A non-empty value here is the signal to check this module's tables
    against the provider's current documentation.
    """

    readable: bool = True
    """Whether a scope list was obtained at all. False is a LOUD failure.

    False and an empty ``capabilities`` is the fail-closed answer to a missing
    or unreadable scope list, and it is deliberately distinguishable from a
    credential that legitimately reports no scopes: both grant nothing, but only
    one of them means something is broken. A caller that treats these as the
    same case loses the ability to alert on the broken one.
    """

    def allows(self, capability: Capability) -> bool:
        """Whether this grant permits ``capability``. The one read a caller needs."""
        return capability in self.capabilities


def _normalize(provider: str, scope: str) -> str:
    """Reduce one reported scope to the form this provider's table is keyed by.

    Microsoft returns a granted delegated permission fully qualified with its
    resource URI; the table (and the app registration, and ``scopes``) names it
    bare. Google returns its scopes exactly as ``scopes`` declares them, so
    nothing is stripped there -- normalising a Google URI on its last segment
    would collapse distinct scopes onto each other.
    """
    if provider != "microsoft":
        return scope
    return scope.rpartition(_GRAPH_RESOURCE_SEPARATOR)[2] or scope


def _read_scopes(provider: ScopeSource) -> tuple[str, ...] | None:
    """Pull the reported scope list off the credential, or ``None`` if unreadable.

    ``None`` is reserved for "no scope list could be obtained", which is a
    different fact from "the list was empty" and must not be collapsed into it.
    Every failure mode of asking a credential for its scopes lands here:
    the method missing, the credential absent, the SDK raising.

    The broad ``except`` is deliberate and is the fail-loudly rule applied
    correctly rather than skipped: the exception types are the union of two
    vendor SDKs' auth trees plus whatever a not-yet-written provider raises, and
    narrowing to today's list would silently let tomorrow's escape as a raw
    traceback out of a security check. Nothing is swallowed -- the failure is
    logged at ERROR with the exception, and it becomes a grant of NOTHING, which
    is the loudest possible outcome for a permission check.
    """
    try:
        reported = provider.granted_scopes()
    except Exception as exc:  # ruff: ignore[blind-except] - see docstring; becomes a closed grant
        logger.error(
            f"GRANTS: unreadable scope list provider={provider.name} error={exc!r}. "
            "Granting NOTHING. ACTION REQUIRED: re-run "
            "'uv run inbox-zero-connector auth <provider>' and confirm the "
            "credential is present and current.",
        )
        return None
    if reported is None:
        return None
    return tuple(str(scope) for scope in reported)


def _refuse(provider: str, reason: str) -> Grant:
    """Return the closed grant, loudly. The one place "nothing" is constructed.

    Single-sited so that every fail-closed path produces an identical shape and
    an identical log line, and so the count of ways to arrive at "grants
    nothing" is auditable by reading this file rather than by tracing callers.
    """
    logger.error(
        f"GRANTS: refusing all capabilities provider={provider} reason={reason}. "
        "This credential is treated as permitting NOTHING. ACTION REQUIRED: "
        "check the account's granted permissions in the provider's admin "
        "console, then re-authenticate.",
    )
    return Grant(provider=provider, capabilities=frozenset(), raw_scopes=(), readable=False)


def discover(provider: ScopeSource) -> Grant:
    """Ask ``provider``'s live credential what it is actually permitted to do.

    This is the whole public surface of the module. It reads the scope list the
    credential itself reports, maps each entry through THAT provider's table,
    and returns the union with the evidence attached.

    It performs NO extra network call by design. Both providers already hold
    the granted scope list as a by-product of authenticating -- Microsoft in the
    msal token response, Google on the credential object -- so a second API call
    would be asking the network for something already in hand, and would add a
    failure mode to a security check that must not acquire new ones.

    FAIL CLOSED, three ways, none of which grants anything:

    - the provider is not one this module has a table for -> nothing;
    - the scope list is missing or unreadable -> nothing, at ERROR;
    - the scope list is empty, or holds only scopes no row matches -> nothing,
      at ERROR, with the unmatched scopes named so the table can be fixed.

    Args:
        provider: Anything reporting its ``name`` and its credential's granted
            scopes. Not the full ``MailProvider`` interface -- see ``ScopeSource``.

    Returns:
        A ``Grant``. Always determined, never partial, and never wider than the
        scopes it names in ``raw_scopes``.
    """
    table = PROVIDER_TABLES.get(provider.name)
    if table is None:
        return _refuse(provider.name, f"unknown provider {provider.name!r}")

    reported = _read_scopes(provider)
    if reported is None:
        return _refuse(provider.name, "no scope list could be read from the credential")

    matched = {
        scope: table[_normalize(provider.name, scope)]
        for scope in reported
        if _normalize(provider.name, scope) in table
    }
    unrecognized = tuple(scope for scope in reported if scope not in matched)
    capabilities = frozenset[Capability]().union(*matched.values()) if matched else frozenset()

    _log_outcome(provider.name, reported, capabilities, unrecognized)
    return Grant(
        provider=provider.name,
        capabilities=capabilities,
        raw_scopes=reported,
        unrecognized=unrecognized,
        readable=True,
    )


def _log_outcome(
    provider: str,
    reported: tuple[str, ...],
    capabilities: frozenset[Capability],
    unrecognized: tuple[str, ...],
) -> None:
    """Emit the discovery receipt at the severity the outcome deserves.

    An empty capability set is an ERROR, not an INFO. A credential that permits
    nothing is either a genuinely locked-down account or a bug in this module's
    tables, and both deserve to be noisy -- the failure this whole module exists
    to prevent is a wrong answer that nobody noticed.

    Scope strings are logged. They are permission NAMES, not credentials: no
    token, no refresh token, and no address appears here.
    """
    scope_list = " ".join(reported) if reported else "<none>"
    if unrecognized:
        logger.error(
            f"GRANTS: unrecognized scope(s) provider={provider} "
            f"granting_nothing={' '.join(unrecognized)}. ACTION REQUIRED: if the "
            "account legitimately holds these, add a row to this provider's "
            "table in grants.py -- an unmapped scope grants NOTHING by design.",
        )
    if not capabilities:
        logger.error(
            f"GRANTS: no capabilities granted provider={provider} "
            f"scopes={scope_list}. The connector will be able to do NOTHING with "
            "this credential. ACTION REQUIRED: confirm the granted permissions "
            "in the provider's admin console.",
        )
        return
    logger.info(
        f"GRANTS: discovered provider={provider} "
        f"capabilities={','.join(sorted(capabilities))} scopes={scope_list}",
    )


# ---------------------------------------------------------------------------
# The grant RECORD. When the account said yes -- which the token cannot tell us.
# ---------------------------------------------------------------------------

GRANTS_FILENAME: Final = "grants.json"
"""Name under the data directory. Beside the health file, not inside it."""

GOOGLE_TESTING_REFRESH_WINDOW: Final = timedelta(days=7)
"""How long Google honours the refresh token of an app still in Testing.

Google's own limit, not a policy of this connector's: an OAuth client whose
publishing status is Testing has its refresh tokens expire seven days after
issue, and the connector cannot renew past it. That is the failure #64's
advance warning exists to predict, and it is predictable only because the
window is fixed -- an app in Production has no such expiry at all, which is why
``refresh_token_expiry`` answers ``None`` there rather than guessing a date.
"""


class GrantRecord(ProviderModel):
    """WHEN a provider's grant was made. The one fact the token does not carry.

    A stored Google credential reports the ACCESS token's expiry -- about an
    hour out, refreshed continuously, and completely silent about the refresh
    token underneath it. So ``creds.expired`` cannot answer "how long until
    this install stops working", and nothing else on the credential can either:
    the seven-day Testing window is measured from the moment consent was
    granted, and that moment is not recorded anywhere by the OAuth libraries.

    This record is that moment, and it is the whole reason the file exists.

    NOT A CREDENTIAL, AND DELIBERATELY NOT IN THE KEYCHAIN. It holds a
    timestamp and a provider name; there is no token here, so the keychain's
    cost (an unlock prompt, a machine-global namespace) would buy nothing. It
    lives beside the health file under the data directory, which is also what
    makes it readable by ``health.record_round`` without touching credentials.

    ``granted_at`` is OPTIONAL because installs authenticated before this field
    existed have a record with no timestamp -- or no record at all -- and both
    must keep loading. An absent grant time means the expiry is UNKNOWN, which
    ``refresh_token_expiry`` reports as ``None``: the same answer it gives a
    published app, because in both cases there is no date to warn about and
    inventing one would produce a false alarm on a healthy install.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    """Which provider this grant is for. ``"google"`` or ``"microsoft"``."""

    granted_at: datetime | None = None
    """UTC instant the account consented, or ``None`` for a pre-field record."""


def grants_path(data_directory: Path) -> Path:
    """Return where the grant record file lives under ``data_directory``."""
    return data_directory / GRANTS_FILENAME


def _read_records(data_directory: Path) -> dict[str, GrantRecord]:
    """Load every stored grant record, keyed by provider. Never raises.

    A missing, unreadable, or malformed file degrades to "no records", which is
    exactly the pre-field state and is already handled by every caller. The
    alternative -- raising -- would let a damaged advisory file break an auth
    run or a watch round, and this record is advisory by construction: it drives
    a warning, never a permission.
    """
    path = grants_path(data_directory)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Not an error and not rare: every install has no grant record until
        # its first auth, and one that authenticated before this file existed
        # never will. DEBUG, because promoting the ordinary case would train an
        # operator to ignore the line that reports the damaged one below.
        logger.debug(f"GRANTS: no grant record yet path={path}")
        return {}
    except OSError, ValueError:
        logger.opt(exception=True).error(f"GRANTS: unreadable grant record path={path}")
        return {}
    if not isinstance(payload, dict):
        logger.error(f"GRANTS: grant record is not an object path={path}")
        return {}
    records: dict[str, GrantRecord] = {}
    for provider, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        try:
            records[str(provider)] = GrantRecord(provider=str(provider), **entry)
        except ValidationError:
            logger.opt(exception=True).error(
                f"GRANTS: unusable grant record entry provider={provider}; treating as absent"
            )
    return records


def record_grant(
    data_directory: Path, provider: str, *, now: datetime | None = None
) -> GrantRecord:
    """Persist the instant ``provider``'s grant was made, and return the record.

    Called at successful authentication, which is the only moment the value is
    knowable. Other providers' records are preserved: two mailboxes on one
    install re-authenticate independently, and rewriting the file from one
    provider's view would silently erase the other's expiry warning.

    Args:
        data_directory: Where the grant record file lives.
        provider: The provider key -- ``google`` or ``microsoft``.
        now: Injectable clock, for tests. Defaults to the current UTC time.

    Returns:
        The record just written.
    """
    record = GrantRecord(provider=provider, granted_at=now or datetime.now(UTC))
    records = _read_records(data_directory)
    records[provider] = record
    write_text_atomic(
        grants_path(data_directory),
        json.dumps(
            {
                name: {"granted_at": entry.granted_at.isoformat() if entry.granted_at else None}
                for name, entry in sorted(records.items())
            },
            indent=2,
            sort_keys=True,
        ),
        what="GRANTS",
    )
    logger.info(f"GRANTS: grant time recorded provider={provider}")
    return record


def refresh_token_expiry(provider: str, data_directory: Path) -> datetime | None:
    """When ``provider``'s REFRESH token stops working, or ``None`` if it does not.

    The refresh token is what an operator actually cares about: the access
    token expires hourly and renews itself invisibly, while the refresh token
    expiring is the day the watcher stops. Only one case has a knowable date --
    a Google app still in Testing, seven days from the recorded grant -- so
    that case is computed and every other case answers ``None``.

    ``None`` is "no predictable expiry", NOT "expired" and not "unknown error".
    Four separate situations reach it, and merging them is correct because the
    caller's response to all four is identical -- issue no advance warning:

    - the provider is Microsoft, which has no equivalent window;
    - the buyer's Google app is Published, where refresh tokens do not expire
      on a clock;
    - this install authenticated before the grant time was recorded;
    - no grant has been recorded at all.

    Args:
        provider: The provider key -- ``google`` or ``microsoft``.
        data_directory: Where the grant record file lives.

    Returns:
        The UTC instant the refresh token expires, or ``None``.
    """
    if provider != "google":
        return None
    record = _read_records(data_directory).get(provider)
    if record is None or record.granted_at is None:
        return None
    # The settings read is LAST, after the two cheap facts have already ruled
    # out every install that cannot have an expiry. Building the settings
    # singleton is not free -- on a process that has not built it yet it also
    # configures the log sinks -- so it happens only for the one case whose
    # answer actually depends on it.
    if settings().google_app_status != "testing":
        return None
    return record.granted_at + GOOGLE_TESTING_REFRESH_WINDOW


def recorded_providers(data_directory: Path) -> tuple[str, ...]:
    """Every provider this install has a recorded grant for. Sorted, never raises.

    Exists so a caller can ask about the grants that WERE MADE rather than the
    one provider the config currently names. The two differ on a two-mailbox
    install, and the health file wants the grants: an expiry warning is about a
    credential going stale, which is a fact about the grant, not about which
    mailbox a config file happens to point at today.
    """
    return tuple(sorted(_read_records(data_directory)))
