"""The ACCOUNT LIST: which mailboxes this install was set up for, and how each is posted.

The wizard asks for the whole list UP FRONT, before any browser consent window
opens, so a human who owns three mailboxes answers three short questions once
instead of discovering after the first sign-in that they have to run the
installer again. The auth pass then walks the list in one go, and ``watch``
polls every entry once per round (#69).

WHAT STILL BOUNDS THE LIST is the keychain, not this module: an install stores
one credential per PROVIDER (``services/auth/keychain.py``), so two entries
naming the same provider would share one token. ``apps/cli/watch_accounts.py``
refuses that config rather than running it, and ``INSTALL-RUNBOOK.md`` § 6b is
the operator-facing statement of the same limit.

WHAT IS PER ACCOUNT AND WHAT IS SHARED IS A DECISION, NOT AN OVERSIGHT
(D11, Rico, 2026-09-01). The TAXONOMY is shared across every account on an
install: it is the folder vocabulary the owner agreed to, and two mailboxes
belonging to one person filing into two different vocabularies is a support
call, not a feature. ``posture`` and the ``drafts`` flag are PER ACCOUNT,
because they are the security answer for one mailbox and a firm routinely has
a different one for each -- a personal Gmail that may draft and a client-facing
Exchange mailbox that files only is the ordinary case, and a single shared
answer forces one of those two to be wrong in the widening direction.

WHY PROVIDER IS INFERRED AND NOT ASKED. The mailbox address already states the
answer for the overwhelming majority of installs, and a question whose answer
is sitting in the previous answer is a question that gets typed wrong.
``infer_provider`` returns ``None`` rather than a guess for every domain it
does not KNOW -- a custom domain on Google Workspace and a custom domain on
Microsoft 365 are indistinguishable from the address alone, so the honest
answer is to ask. Guessing there would send the install at the wrong consent
screen and produce an auth failure with no obvious cause.

THE FIRST ACCOUNT IS THE DEFAULT, and that is what keeps every existing
single-account caller working unchanged. ``Settings.provider``,
``Settings.posture``, and ``Settings.drafts_enabled`` continue to answer for
the install as a whole; this list is additive record-keeping the watch loop
does not yet read. When the multi-account loop lands it reads THIS list, and
nothing above it has to move.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, Field, StrictBool, field_validator

from inbox_zero_connector.utils.sections import Section

GOOGLE_DOMAINS: Final[frozenset[str]] = frozenset({"gmail.com", "googlemail.com"})
"""Consumer Gmail domains. ``googlemail.com`` is the same mailbox as ``gmail.com``.

Google served the German and UK markets under ``googlemail.com`` for years and
still delivers both to one account, so an address at either is a Google mailbox
with certainty. A Workspace domain is NOT in this set on purpose -- see
``infer_provider``.
"""

MICROSOFT_DOMAINS: Final[frozenset[str]] = frozenset({
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
})
"""Consumer Microsoft domains, all of which are one Microsoft account.

Regional spellings (``outlook.co.uk``, ``hotmail.co.uk``, ``live.co.uk``) are
deliberately absent rather than enumerated: the list would never be complete,
and an unknown domain already has a correct outcome -- the wizard asks. A
half-complete guess table is worse than no table, because the misses look like
the hits.
"""


def infer_provider(address: str) -> str | None:
    """The provider implied by a mailbox address, or ``None`` when it must be asked.

    Args:
        address: The mailbox address as the human typed it. Case and surrounding
            whitespace are irrelevant; both are normalised here rather than at
            the caller, so a wizard prompt and a config file get one answer.

    Returns:
        ``"google"``, ``"microsoft"``, or ``None``.

    ``None`` IS A REAL ANSWER, not a failure. A custom domain
    (``someone@example-business.com``) is served by Google Workspace, Microsoft 365, or
    neither, and the address cannot distinguish them. Returning a guess would
    open the wrong consent screen and fail at sign-in with nothing pointing at
    the cause; returning ``None`` makes the wizard ask one extra question in
    exactly the cases where the answer is genuinely unknown.
    """
    _, _, domain = address.strip().lower().rpartition("@")
    if not domain:
        return None
    if domain in GOOGLE_DOMAINS:
        return "google"
    if domain in MICROSOFT_DOMAINS:
        return "microsoft"
    return None


class AccountSettings(Section):
    """One mailbox the installer set this install up for.

    Frozen and extra-forbidding like every ``Section``, so a hand-edited
    ``accounts`` entry with a typo'd key is a startup error naming the key
    rather than a silently dropped setting -- which matters here because one of
    those keys is the drafts flag that decides a send-capable Gmail scope.
    """

    address: str
    """The mailbox address. Normalised, and it is what names the account."""

    provider: str
    """``google`` or ``microsoft``, inferred from the address or answered.

    A plain ``str`` validated against ``PROVIDER_NAMES``' two values rather than
    the ``ProviderName`` Literal from the keystone, because importing the
    keystone here would make the dependency a cycle: ``config`` re-exports this
    module's names so callers keep one import site.
    """

    owner_domain: str
    """The owner's own mail domain FOR THIS ACCOUNT -- the internal/external line.

    Per account rather than per install, because it is the routing question and
    two mailboxes at one install belong to two different organisations often
    enough that a shared answer would route an outsider into the internal
    voice. Normalised exactly as ``VoiceRouting.owner_domain`` is, since the
    two are compared against the same recipient domains.
    """

    posture: str = "file_only"
    """What this ACCOUNT's deployment may do (D11). ``file_only`` by default.

    Per account, and the default is the narrow answer for the same reason
    ``ConnectorConfig.posture`` defaults narrow: an install that did not answer
    must not be composing drafts.
    """

    drafts: StrictBool = False
    """Whether reply drafts are enabled for THIS account. ``StrictBool``, deliberately.

    Same security choice ``ConnectorConfig.drafts_enabled`` records at length:
    an ordinary pydantic ``bool`` coerces ``"yes"`` and ``"1"`` to ``True``, and
    on Google this flag is what requests a scope Gmail's send methods also
    accept. A truthy string where somebody meant a note must be a load error.
    """

    @field_validator("address", "owner_domain")
    @classmethod
    def _fold(cls, value: str) -> str:
        """Lowercase and trim, so a typed answer and a file value compare equal.

        Mail domains are case-insensitive per RFC 5321 and may arrive fully
        qualified with a trailing root dot. Folding once here rather than at
        each comparison is what keeps the internal/external decision from
        depending on how the installer happened to type it.
        """
        folded = value.strip().lower().rstrip(".")
        if not folded:
            raise ValueError(
                "an account needs both a mailbox address and an owner domain. "
                "ACTION REQUIRED: re-run 'uv run inbox-zero-connector setup'."
            )
        return folded

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        """Refuse a provider name nothing can authenticate against.

        An unknown value would otherwise sit in the file until the auth pass
        reached it and failed with a message about a missing adapter rather than
        about a bad config entry.
        """
        folded = value.strip().lower()
        if folded not in {"google", "microsoft"}:
            raise ValueError(
                f"accounts entry names unknown provider {value!r}. "
                "ACTION REQUIRED: use 'google' or 'microsoft'."
            )
        return folded

    @field_validator("posture")
    @classmethod
    def _known_posture(cls, value: str) -> str:
        """Refuse a posture outside the closed set, at LOAD rather than at first draft.

        Mirrors what the ``Posture`` enum does for ``ConnectorConfig``: a
        hand-typed ``file_read_drafts`` must be a load error, not a value that
        reads as "not file_only" to every comparison downstream.
        """
        folded = value.strip().lower()
        if folded not in {"file_only", "file_read_draft"}:
            raise ValueError(
                f"accounts entry names unknown posture {value!r}. "
                "ACTION REQUIRED: use 'file_only' or 'file_read_draft'."
            )
        return folded


MAX_ACCOUNTS: Final = 20
"""Guard against a corrupted config turning the list into an unbounded one.

Twenty is far beyond any real install and small enough that a file claiming
hundreds is obviously wrong. Same instinct as ``MAX_VOICES`` in the keystone.
"""


def validate_accounts(value: tuple[AccountSettings, ...]) -> tuple[AccountSettings, ...]:
    """Refuse a duplicate address or an oversized list. Order is PRESERVED.

    Order is load-bearing: the FIRST entry is the install's default account, so
    a validator that sorted or deduplicated by rewriting the sequence would
    silently change which mailbox an existing single-account install serves.

    Args:
        value: The accounts as configured, in the order the installer gave them.

    Returns:
        The same tuple, unchanged, when it is legal.

    Raises:
        ValueError: A repeated address, or more entries than ``MAX_ACCOUNTS``.
    """
    if len(value) > MAX_ACCOUNTS:
        raise ValueError(
            f"accounts has {len(value)} entries; the limit is {MAX_ACCOUNTS}. "
            "ACTION REQUIRED: shorten the list."
        )
    addresses = [account.address for account in value]
    duplicates = sorted({name for name in addresses if addresses.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"accounts lists the same mailbox twice: {', '.join(duplicates)}. "
            "ACTION REQUIRED: give each mailbox exactly one entry -- two entries "
            "for one mailbox would be two watchers on it, which nothing detects."
        )
    return value


class AccountsMixin(BaseModel):
    """Carries the ``accounts`` field that ``Settings`` inherits.

    A MIXIN RATHER THAN A FIELD ON THE KEYSTONE, and the reason is mechanical
    rather than aesthetic: ``config.py`` sits AT its 500-code-line ceiling
    (``scripts/check_code_size.py``), which its own module docstring already
    records, so declaring this field there breaches the check -- measured at 509
    lines before this moved. The field and its validator are one unit of
    meaning and they live together here with the model they hold, and
    ``Settings`` pays one base-class name for the whole of it.

    Not a ``Section``: ``Section`` is frozen and forbids extras, which is right
    for a settings SECTION and wrong for a base of ``BaseSettings`` -- freezing
    would fight ``validate_assignment`` on the settings object itself.
    """

    accounts: Annotated[tuple[AccountSettings, ...], AfterValidator(validate_accounts)] = Field(
        default_factory=tuple
    )
    """Every mailbox the wizard was told about, FIRST ONE FIRST. Record, not behaviour.

    The setup wizard asks for the whole list before any consent window opens and
    writes it here; the auth pass then walks it in one go, and ``watch`` POLLS
    EVERY ENTRY once per round (#69) -- see the module docstring for what is per
    account (posture, drafts) and what stays shared (the taxonomy), and
    ``INSTALL-RUNBOOK.md`` § 6b for the one credential-per-provider limit that
    still bounds how many entries an install can carry.

    EMPTY IS THE HISTORICAL SHAPE AND KEEPS WORKING EXACTLY. Every install
    written before this field existed has no ``accounts`` key, and the
    install-level ``provider``, ``posture``, and ``drafts_enabled`` remain the
    whole answer for it. The first entry is the default account, so a list whose
    head matches those install-level fields is the same deployment described
    twice rather than a second one -- which is why nothing here overwrites them.

    The duplicate-address and length refusals are attached as an
    ``AfterValidator`` rather than restated as a validator body, so the
    one-watcher-per-mailbox guarantee has exactly one implementation.
    """
