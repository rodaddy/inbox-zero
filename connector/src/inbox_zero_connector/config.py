"""THE KEYSTONE. Every setting, every environment read, and logging setup.

Configuration runs first, before anything else in the connector. This module
reads the environment and the installed JSON layer, validates both, sets up
logging, and hands the result down. **Nothing else in this package reads an
environment variable, loads a config file, or configures a logging sink.**

That single rule is what makes the connector testable -- every component's
inputs arrive through a constructor or a settings object and can be replaced in
a test -- and it is also a security property here, because the environment is
where the buyer's OAuth client identifiers live. One module reads them, one
module can be audited for what it does with them, and the answer is "passes
them to the provider SDK and never logs them".

---------------------------------------------------------------------------
PRECEDENCE, AND WHY IT IS TRUE RATHER THAN CLAIMED
---------------------------------------------------------------------------

Highest to lowest:

    1. init kwargs                       tests only
    2. INBOX_ZERO_* environment          operator/host overrides
    3. secrets/config.json               credentials only, gitignored
    4. <config dir>/secrets.json         the human's own credential VALUES
    5. connector-config.json (installed) the installer's decisions
    6. config/connector-config.{env}     committed per-environment overrides
    7. config/connector-config.json      committed defaults

Layers 3-7 are ONE ``JsonConfigSettingsSource`` over a list with
``deep_merge=True``, listed lowest-first, so ``secrets/`` overlays the
``credentials`` group rather than replacing it. Missing files are skipped, which
is what lets a fresh clone run from the committed files with no install step.

LAYERS 3 AND 4 MAY STATE ``credentials`` AND NOTHING ELSE. They outrank the
connector-config file a buyer audits, so a ``posture``, ``drafts_enabled``, or
``classifier`` key there would change what the deployment DOES without appearing
in the file anybody reads -- and it did, until ``checked_credential_files``
made it a startup refusal naming the file. The same split is why
``_file_posture`` reads ``audited_config_files()`` rather than every layer: the
question it answers is what the AUDITED config records, and an answer sourced
from a credential file was maskable from one.

Layer 4 exists so the installer never has to touch a secret. The wizard writes
IDENTIFIERS into layer 5 and rewrites that file freely; the client secret the
human types goes into layer 4, which no code path writes a value into, so a
rewrite of the installed file cannot clobber it and a support copy of the
installed file cannot leak it.

That order is not a description -- it is the tuple returned by
``settings_customise_sources`` below, so it cannot drift from this docstring.
Loading the JSON by hand and passing it as ``Settings(**values)`` would invert
it silently, because init kwargs are pydantic's HIGHEST-priority source; that
exact defect is recorded in ``_DOCS/STANDARDS-python.md``.

---------------------------------------------------------------------------
1. DRAFTS POSTURE (per provider)
---------------------------------------------------------------------------

``drafts_enabled`` is the installer's answer to a question with a real cost on
Google: turning Gmail drafts on requires ``gmail.compose``, which the Gmail
send methods also accept, so it drops the Gmail no-send guarantee from
scope-layer to code-layer. See ``scopes``. Leaving it false keeps the Stage 1
structural posture and gives up reply drafts on that mailbox.

The flag gates BOTH the auth request and the tool. A deployment with Google
drafts off never asks for the compose scope, so a buyer can verify the posture
from their own admin console rather than taking our word for it.

Microsoft pays nothing for drafts (``Mail.ReadWrite`` already covers
``createReply``), so the flag there is a capability switch, not a trade.

---------------------------------------------------------------------------
2. VOICE ROUTING (deterministic, never AI-decided)
---------------------------------------------------------------------------

Security invariant, spec Stage 2: the AI writes the words, a script picks the
voice. ``resolve_voice`` is a pure function of (owner domain, recipients,
thread labels) and the installed rule TABLE -- an ordered tuple of
predicate/voice rows, not a conditional ladder, so adding a rule is a row and
the branch count stays at one. It performs no I/O, calls no model, and cannot
be influenced by message BODY text -- which matters, because message bodies are
untrusted input and a hostile email that could talk the AI into "use the
internal voice" would be harvesting the owner's private register for a phishing
template.

The rules, in the order the spec fixed them:

    1. every recipient domain == owner domain  -> internal
    2. any thread label in the sales label set -> sales
    3. otherwise                               -> external

Ambiguity defaults to EXTERNAL, deliberately and in every direction: no
recipients, unparseable recipients, a mix of internal and external recipients,
an unknown label. External is the most guarded register, so the failure mode
of every uncertain case is being slightly too formal with a colleague rather
than too familiar with a stranger.

---------------------------------------------------------------------------
3. MAILBOX SCOPE (which folders the watcher may read)
---------------------------------------------------------------------------

``MailboxScope`` answers one question -- may the watcher read this folder --
and ``covers`` is the only place that question is answered. It exists because
Inbox-only watching is wrong in a real tenant (server-side Exchange rules move
mail out of Inbox before we see it) and because widening it without a client's
explicit exclusions is worse: shared mailboxes, matter files, and privileged
material are folders a law firm needs to be able to say NEVER about.

The default is ``inbox_only``, which is the behaviour every install already
has, so an upgrade cannot widen what a deployment reads without a human
editing the config. Matching is case-insensitive; ``covers`` documents why
that is a data-exposure guard rather than a nicety.

Scope is stored PER PROVIDER, keyed the same way ``drafts_enabled`` is, because
Gmail and Microsoft do not share a folder tree and a firm routinely wants a
different answer for each. A provider with no key falls back to the narrow
default in ``DEFAULT_SCOPES``, so naming one provider never widens the other.
That table is also where each mailbox's nesting convention is recorded: Gmail
label names encode hierarchy with a slash and Graph folder names do not, so an
exclusion naming a parent means different things in the two mailboxes and the
default has to say which one it is answering about.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

from loguru import logger
from pydantic import (
    Field,
    SecretStr,
    StrictBool,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# The accounts MIXIN only -- not a re-export block. This file sits AT its
# 500-code-line ceiling (`scripts/check_code_size.py`), so the three-block
# `as`-form re-export shape used for the sections above does not fit: measured,
# it takes the file to 509. `AccountSettings` and `infer_provider` are imported
# from `inbox_zero_connector.accounts` directly by the one caller that needs
# them (`apps/cli/setup.py`), which is the cheaper half of the trade.
from inbox_zero_connector.accounts import AccountsMixin

# The keystone's PATHS HALF, split into ``config_paths`` for the 500-code-line
# ceiling and re-exported here so ``config`` stays the single import site for
# every caller. Same privilege and same audit surface, not a second reader --
# see that module's docstring and ``CONFIG_MODULES`` in the standards test.
# Only the names CALLERS import through ``config`` are re-exported. The env-var
# NAMES and the cache reset are imported from ``config_paths`` directly by the
# tests that need them: a re-export exists to keep a caller working, and one
# with no caller is surface this file pays for in its line ceiling.
from inbox_zero_connector.config_constants import (
    APP_AUTHOR as APP_AUTHOR,
)
from inbox_zero_connector.config_constants import (
    APP_NAME as APP_NAME,
)
from inbox_zero_connector.config_constants import (
    ENV_PREFIX as ENV_PREFIX,
)
from inbox_zero_connector.config_constants import (
    PROJECT_ROOT as PROJECT_ROOT,
)
from inbox_zero_connector.config_paths import (
    ENV_NAME_PATTERN,
    INSTALL_ID_PATTERN,
    audited_config_files,
    checked_credential_files,
    env_name,
    refuse_unusable_prefix,
)
from inbox_zero_connector.config_paths import (
    config_dir as config_dir,
)
from inbox_zero_connector.config_paths import (
    config_path as config_path,
)
from inbox_zero_connector.config_paths import (
    data_dir as data_dir,
)
from inbox_zero_connector.config_paths import (
    install_id as install_id,
)
from inbox_zero_connector.config_paths import (
    ledger_path as ledger_path,
)
from inbox_zero_connector.config_paths import (
    secrets_path as secrets_path,
)
from inbox_zero_connector.config_paths import (
    taxonomy_path as taxonomy_path,
)
from inbox_zero_connector.config_paths import (
    voice_profiles_path as voice_profiles_path,
)
from inbox_zero_connector.errors import OperatorRefusalError

# ONE statement rather than the four single-name blocks this was: identical
# re-export surface (the `as` form is what ruff reads as intentional), three
# fewer code lines, and this file is AT its 500-line ceiling.
from inbox_zero_connector.services.watch.scope import (
    MAX_SCOPE_FOLDERS as MAX_SCOPE_FOLDERS,
)
from inbox_zero_connector.services.watch.scope import (
    SCOPE_MODES as SCOPE_MODES,
)
from inbox_zero_connector.services.watch.scope import (
    MailboxScope as MailboxScope,
)
from inbox_zero_connector.services.watch.scope import (
    default_scope as default_scope,
)
from inbox_zero_connector.utils import logging_config
from inbox_zero_connector.utils.sections import Section

DEFAULT_VOICES: Final[tuple[str, ...]] = ("internal", "external", "sales")
"""The three profiles the spec fixes. The installer may extend this list.

Each becomes a separate reviewable plain-text style doc under invariant 5.
"""

FALLBACK_VOICE: Final = "external"
"""Where every ambiguous case lands. Must always be present in ``voices``."""

MAX_VOICES: Final = 12
"""Guard against a corrupted config turning a closed list into an open one."""


MAX_DRAFT_BODY_CHARS: Final = 8000
"""Upper bound on a generated draft body, enforced before any API call.

Not primarily an abuse control -- it is a blast-radius bound. A runaway
generation that pasted an entire thread history (or an entire other mailbox)
into a draft would put that content somewhere the owner might forward without
reading. 8000 characters is a long human reply and a short data dump.
"""

DEFAULT_FOLDER_PREFIX: Final = ""
"""No prefix. A taxonomy folder named ``Reference`` becomes ``Reference``.

The connector used to hardcode a prefix per adapter -- ``InboxZero/`` on Gmail,
``InboxZero-`` on Graph -- so that its filings stayed visibly separate from the
owner's own folders. Verified against a live Exchange Online mailbox on
2026-08-18: what the owner actually sees is a sidebar of folders nobody named,
in a mailbox they have filed mail in for years. The separation was OUR
convenience, not theirs, and it is the wrong default for a connector whose whole
pitch is that it files into the taxonomy the owner agreed to.

An operator who wants the old separation sets the prefix back and gets the old
names byte-for-byte; ``InboxZero-`` restores Graph, ``InboxZero/`` restores the
Gmail label hierarchy. Making it a SETTING rather than a per-adapter constant is
what lets those two differ, because they are not the same product decision --
a Gmail slash nests a label, a Graph hyphen only spells one.
"""

MAX_FOLDER_PREFIX_CHARS: Final = 40
"""Upper bound on the prefix, enforced at construction.

A prefix is a handful of characters by construction. The ceiling exists for the
same reason the taxonomy caps folder names: a corrupted or hand-edited config
must not be able to push an unbounded string into every folder name the
connector creates in the owner's mailbox.
"""

PROVIDER_NAMES: Final[tuple[str, ...]] = ("google", "microsoft")

ProviderName = Literal["google", "microsoft"]

DEFAULT_SCOPES: Final[dict[str, MailboxScope]] = {
    "google": default_scope(names_encode_nesting=True),
    "microsoft": default_scope(names_encode_nesting=False),
}
"""The scope a provider starts at when the installed config does not name one.

A TABLE, NOT A VALIDATOR, and that distinction is the whole point. Inferring
``names_encode_nesting`` from the provider key inside a validator would put a
provider-name comparison back in the model layer -- the exact coupling
``MailboxScope`` was written to avoid, since the field records a property of
the MAILBOX and nothing there may re-derive it from a vendor string. As a
default it is a value chosen once, at the seam that already knows which
provider it is keying, and an explicit answer from the installer or the
environment simply lands on top of it in BOTH directions. A client who says
"no nesting on Gmail" gets ``False``; nothing here can overrule them.

WHY GOOGLE IS TRUE AND MICROSOFT IS FALSE is a difference in the two APIs, not
a preference. Gmail's ``Label`` resource carries no parent field, so
``Clients/Smith`` is an independent label whose only relationship to
``Clients`` is the slash a human typed -- leaving the flag False there means an
exclusion naming ``Clients`` reads every matter beneath it, which is the
data-exposure defect this kit exists to not have. A Graph folder genuinely
nests through ``parentFolderId`` and its display name does NOT encode that, so
a folder literally named ``Clients/Smith`` is ONE folder with a slash in its
name, and turning the flag on there would invent a parent Graph never stated.

Adding a provider is one row. It is a ``dict`` keyed by the same names
``PROVIDER_NAMES`` holds, and every entry is a frozen ``MailboxScope``, so
sharing one instance across every caller is safe by construction rather than
by discipline -- a mutable default here would let one deployment's scope
answer leak into another's.
"""


class ConfigError(OperatorRefusalError):
    """The installed configuration is missing, malformed, or violated.

    An ``OperatorRefusalError``, which is what earns it the one-line treatment at
    the CLI boundary and the traceback-free failure point in the logging
    wrapper. The base is the shared answer with ``providers.base.AuthError``;
    see ``errors`` for why the distinction is a type rather than a tuple the
    two seams each have to remember.
    """


class UnknownVoiceError(ConfigError):
    """A caller named a voice that is not in the installed list.

    Carries the legal set so the host AI can retry with a valid value rather
    than guessing again -- same contract as ``UnknownFolderError``.
    """

    def __init__(self, requested: str, allowed: tuple[str, ...]) -> None:
        """Keep both halves of the rejection addressable, then build the message.

        ``requested`` and ``allowed`` are stored as attributes as well as being
        interpolated into the message, so a caller can branch on the legal set
        without parsing prose. The message states the list is install-time fixed
        because otherwise the host AI's obvious next move is to try to add one.

        Args:
            requested: The voice name the caller asked for.
            allowed: The installed voice list, in configured order.
        """
        self.requested = requested
        self.allowed = allowed
        super().__init__(
            f"Voice {requested!r} is not configured for this mailbox. "
            f"Allowed voices: {', '.join(allowed)}. "
            "The voice list is fixed at install time and cannot be extended at runtime."
        )


# ---------------------------------------------------------------------------
# Sections -- Pydantic models, never bare dataclasses.
#
# Pydantic validates and coerces at construction, so a bad value fails at the
# boundary with a message naming the field, instead of surfacing much later as
# an AttributeError on None. _DOCS/STANDARDS-python.md ## LAW: Pydantic for
# every model.
# ---------------------------------------------------------------------------


class LoggingSettings(Section):
    """Logging sinks and levels. The sole input to ``logging_config.setup``.

    Defaults are the safe shape: INFO, all three sinks on, verbose tracebacks
    OFF. ``verbose_tracebacks`` widens a traceback to the full call chain. It
    does NOT turn on loguru's ``diagnose``, which renders local variable VALUES
    beside each frame: a frame in this package holds mail bodies and OAuth
    tokens, so ``diagnose`` is hard-off on every sink and is not settable.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    console_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None
    """Stderr sink level, when it should differ from the disk sinks.

    ``None`` -- the default -- means the console follows ``level``, so a config
    that names only ``level`` behaves exactly as it did before this key existed.
    Set it to raise the console alone: ``level="DEBUG"`` with
    ``console_level="WARNING"`` keeps the full support trace on disk while the
    person running the CLI sees a quiet terminal. It cannot move the sink off
    stderr -- that is fixed by the MCP stdout rule in ``logging_config``.
    """

    file_sink: bool = True
    """Plain-text rotating sink -- the one a human tails."""

    json_sink: bool = True
    """Structured JSON sink, one object per line, for aggregation."""

    directory: Path = Field(default_factory=lambda: data_dir() / "logs")

    rotation: str = "1 MB"
    """loguru size or time spec. 1 MB per file, per the observability standard."""

    retention: int = Field(default=3, ge=1, le=100)
    """Rotated files retained. 3, per the observability standard."""

    verbose_tracebacks: bool = False
    """Full call-chain tracebacks. Off by default; never renders variable values."""


class VoiceRouting(Section):
    """The deterministic routing table. Immutable once loaded."""

    owner_domain: str
    voices: tuple[str, ...] = DEFAULT_VOICES
    sales_labels: tuple[str, ...] = ()
    internal_voice: str = "internal"
    sales_voice: str = "sales"
    default_voice: str = FALLBACK_VOICE

    @field_validator("owner_domain", "internal_voice", "sales_voice", "default_voice")
    @classmethod
    def _strip(cls, value: str) -> str:
        """Trim surrounding whitespace so a hand-edited JSON value still matches.

        These four fields are compared by equality against voice names and
        recipient domains; a trailing space in the installed file would fail
        every comparison silently rather than raising.
        """
        return value.strip()

    @field_validator("owner_domain")
    @classmethod
    def _normalize_owner_domain(cls, value: str) -> str:
        """Fold the owner domain to the form recipient addresses are matched in.

        Routing compares this against the domain part of a recipient address,
        which is case-insensitive per RFC 5321 and may arrive fully qualified
        with a trailing root dot. Normalizing once here rather than at each
        comparison keeps the internal/external decision from depending on how
        the installer happened to type it -- a miss would route an outsider
        into the owner's internal voice.
        """
        return value.lower().rstrip(".")

    @field_validator("voices")
    @classmethod
    def _check_voices(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject an empty, oversized, or duplicated voice list at construction."""
        names = tuple(item.strip() for item in value)
        if not names or any(not name for name in names):
            raise ValueError(
                "voice_routing.voices must be a non-empty list of non-empty names. "
                "ACTION REQUIRED: re-run 'inbox-zero-connector init-config', or fix "
                "the 'voices' list in the installed connector config."
            )
        if len(names) > MAX_VOICES:
            raise ValueError(
                f"voice_routing.voices has {len(names)} entries; the limit is {MAX_VOICES}. "
                "ACTION REQUIRED: shorten the list -- a closed vocabulary that large is "
                "effectively open."
            )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"voice_routing.voices has a duplicate voice: {', '.join(duplicates)}. "
                "ACTION REQUIRED: give every voice exactly one spelling."
            )
        return names

    @model_validator(mode="after")
    def _roles_exist(self) -> VoiceRouting:
        """Every named role must be in ``voices``, checked at LOAD not first use.

        A config whose ``default_voice`` is a typo would otherwise pass install,
        pass every internal-thread draft, and fail only on the first external
        one -- in front of a customer.
        """
        for role in ("internal_voice", "sales_voice", "default_voice"):
            value = getattr(self, role)
            if value not in self.voices:
                raise ValueError(
                    f"voice_routing.{role} is {value!r}, which is not in voices "
                    f"({', '.join(self.voices)}). ACTION REQUIRED: name one of those "
                    "voices, or add this one to the 'voices' list."
                )
        return self

    def validate_voice(self, voice: str) -> str:
        """Return ``voice`` if it is in the installed list, else raise."""
        if voice not in self.voices:
            raise UnknownVoiceError(voice, self.voices)
        return voice


class Posture(StrEnum):
    """What this client's deployment is allowed to DO with mail it has read.

    A ``StrEnum`` rather than a bare string for the reason ``watcher.RoundState``
    gives (``watcher.py:100-107``): the set is closed, so a hand-typed
    ``"file_read_drafts"`` must be a load error rather than a value that ships
    silently and reads as "not file_only" to every comparison downstream.

    =========================================================================
    POSTURE CAN ONLY NARROW. IT CANNOT WIDEN.
    =========================================================================

    This is the load-bearing rule and it is stated here because it is the one
    thing a reader could get wrong. ``FILE_READ_DRAFT`` does not ENABLE drafts;
    it declines to forbid them. ``drafts_enabled`` is the only thing that
    enables them, and it is per PROVIDER because on Gmail it decides an OAuth
    scope: ``drafts_enabled`` true is what makes the connector request
    ``gmail.compose`` (``scopes.scopes_for``), and that request is what moves
    the no-send guarantee from the SCOPE layer to the CODE layer
    (``scopes.enforcement_for``). A posture that could turn drafts on would be
    a SECOND, WEAKER path to a scope decision the security manifest owns --
    two sources for one auditable claim, which is exactly the shape
    ``_env_may_not_flip_the_posture`` already refuses for this same flag.

    So the two fields compose in one direction only:

    ===================  ==================  ==================================
    posture              drafts_enabled      result
    ===================  ==================  ==================================
    ``file_only``        false               no drafts
    ``file_only``        true                NO DRAFTS -- posture narrows
    ``file_read_draft``  false               NO DRAFTS -- and it says so loudly
    ``file_read_draft``  true                drafts
    ===================  ==================  ==================================

    The ``file_read_draft`` + drafts-off row is the one that gets announced,
    because it is the only one where the operator asked for a capability and
    will not get it. ``file_only`` + drafts-on is silent by contrast: the
    operator asked for LESS than the scope permits, which is the direction this
    package defaults to anyway.

    WHAT THIS TOGGLE DOES NOT DO: it does not mark messages read. Filing does
    not change read state anywhere in this connector, under either member, and
    ``FILE_READ_DRAFT`` names no capability beyond filing and drafting -- the
    "read" in the name is the reading of mail the connector already does in
    order to classify it, not a mutation of the unread flag. An unread count is
    how many owners triage, and taking it away is a distinct decision with its
    own consequences that nothing here has made.
    """

    FILE_ONLY = "file_only"
    """Classify and file. No draft is composed, whatever the folder says and
    whatever ``drafts_enabled`` permits. The default, and the narrowest state
    this connector has."""

    FILE_READ_DRAFT = "file_read_draft"
    """Classify, file, and -- where ``drafts_enabled`` also permits it -- leave
    a reply draft. Never sends; no code in this package can."""


class ConnectorConfig(Section):
    """Everything the installer decided. Read-only at runtime.

    Kept as its own model rather than folded into ``Settings`` because it is
    the shape the installer WRITES: ``write_config`` serialises exactly this,
    and ``Settings`` layers the environment over it.
    """

    drafts_enabled: dict[str, StrictBool] = Field(default_factory=dict)
    """Per-provider draft posture. ``StrictBool``, and that is a SECURITY choice.

    Pydantic's ordinary ``bool`` coerces strings: measured 2026-08-17 against
    this package's own model, ``"yes"`` and ``"1"`` both become ``True``. On
    Google, ``drafts_enabled`` true is what makes the connector request
    ``gmail.compose``, a scope Gmail's send methods also accept -- so a config
    holding ``"yes"`` where somebody meant a note would silently move this
    deployment from a scope-layer no-send guarantee to a code-layer one, with
    no error anywhere. ``StrictBool`` refuses anything that is not a real JSON
    ``true``/``false``.
    """

    posture: Posture = Posture.FILE_ONLY
    """What this CLIENT's deployment may do. Per client, not per provider.

    ``file_only`` by default, and never ``file_read_draft``, for the reason
    ``default_config`` states about ``drafts_enabled``: the default is the
    narrow answer rather than the capability the install happens to allow for
    free. An installer who wants drafts says so twice -- once here for the
    client and once per provider for the scope -- and the two questions stay
    separate because they are different questions with different auditors.

    See ``Posture`` for the precedence rule. Read it through ``drafts_allowed``
    rather than comparing this field by hand, so the narrowing lives in one
    place.
    """

    routing: VoiceRouting | None = None

    scope: dict[str, MailboxScope] = Field(default_factory=lambda: dict(DEFAULT_SCOPES))
    """Which folders the watcher may read, PER PROVIDER. Never None, unlike ``routing``.

    THE DEFAULT IS THE FILLED TABLE, not an empty dict, and the difference is
    the whole per-provider answer. Pydantic does not run a field validator over
    a ``default_factory``, so an unset ``scope`` never reached
    ``_known_providers_only_for_scope`` and stayed ``{}`` -- every provider then
    answered through the ``mailbox_scope`` fallback, which is the UNIVERSAL
    default and knows no provider. That is invisible while the two defaults are
    identical and becomes the Gmail nesting hole the moment they are not:
    ``default_config()`` sets only ``drafts_enabled``, so the config the
    installer writes is exactly the one that took that path.

    ``routing`` is optional because a deployment with no voice table simply
    cannot draft, and ``require_routing`` says so loudly. Scope has no such
    escape: a watcher always reads SOMETHING, so the absent case has to be a
    real answer rather than None, and the real answer is the narrowest one --
    Inbox only, no system folders.

    KEYED BY PROVIDER, exactly like ``drafts_enabled``, because scope is the
    setting MOST likely to differ between the two mailboxes rather than least.
    Gmail and Microsoft do not share a folder tree: Graph folders are exclusive
    containers with a well-known Junk/Deleted/Archive set, while Gmail labels
    are non-exclusive and a single message carries several at once. A firm that
    says "watch everything except Client Matters" in Outlook and "Inbox only"
    in Gmail is stating two different, both-legitimate answers, and a flat field
    forced one of them to be wrong. Making it flat also meant the narrower
    answer could never be kept: whichever provider was configured second
    silently rewrote the other's scope.

    A MISSING KEY IS ``inbox_only``, not "unset". The validator fills every name
    in ``PROVIDER_NAMES`` from ``DEFAULT_SCOPES`` before anything reads it, so a
    config written before this model existed -- or one naming only the provider
    the buyer actually installed -- leaves the other provider at the narrowest
    scope rather than at whatever the first provider happened to answer. An
    upgrade must not widen what a deployment reads.

    THE FILL IS PER PROVIDER, because one field of that default is a property
    of the mailbox rather than a preference. ``DEFAULT_SCOPES`` gives Gmail
    ``names_encode_nesting`` true and Graph false; both entries are still
    ``inbox_only`` with every system folder off, so the fill narrows exactly as
    before and the flag only changes what a LATER exclusion means. An explicit
    answer in the file or the environment overrides the row, in both
    directions -- the merge below puts ``value`` last on purpose.
    """

    @field_validator("drafts_enabled", mode="before")
    @classmethod
    def _reject_non_bool(cls, value: object) -> object:
        """Refuse a non-boolean draft flag with the message that names the risk."""
        if not isinstance(value, dict):
            return value
        for name, flag in value.items():
            if not isinstance(flag, bool):
                # ValueError, NOT TypeError, despite this being a type check.
                # Pydantic converts a ValueError raised inside a validator into
                # a ValidationError naming the field; a TypeError propagates
                # raw and is reported as a crash in the validator rather than a
                # bad value in the config. The message is what the operator
                # reads, so it has to arrive attached to the field.
                raise ValueError(  # ruff: ignore[type-check-without-type-error]
                    f"drafts_enabled.{name} must be true or false, got {flag!r}. "
                    "A truthy string would silently request a send-capable Gmail scope. "
                    "ACTION REQUIRED: use a JSON boolean."
                )
        return value

    @field_validator("drafts_enabled")
    @classmethod
    def _known_providers_only(cls, value: dict[str, bool]) -> dict[str, bool]:
        """Reject an unknown provider key rather than ignoring it.

        A typo'd provider name would otherwise read as "drafts off" for the
        provider the installer meant to enable, which is a silent capability
        loss rather than an error.
        """
        unknown = sorted(set(value) - set(PROVIDER_NAMES))
        if unknown:
            raise ValueError(
                f"drafts_enabled names unknown provider(s): {', '.join(unknown)}. "
                f"ACTION REQUIRED: use one of {', '.join(PROVIDER_NAMES)}."
            )
        return {**dict.fromkeys(PROVIDER_NAMES, False), **value}

    @field_validator("scope")
    @classmethod
    def _known_providers_only_for_scope(
        cls, value: dict[str, MailboxScope]
    ) -> dict[str, MailboxScope]:
        """Reject an unknown provider key, then fill the absent ones with the safe default.

        Same two jobs as ``_known_providers_only`` does for ``drafts_enabled``,
        and deliberately the same shape: a typo'd provider name must be an error
        rather than a silently ignored key, because ignoring it would leave the
        provider the installer meant to configure sitting on a default the
        installer never saw. Filling the absent names happens AFTER that check,
        so every read of this dict finds a real answer for every provider and
        no caller has to remember the fallback.
        """
        unknown = sorted(set(value) - set(PROVIDER_NAMES))
        if unknown:
            raise ValueError(
                f"scope names unknown provider(s): {', '.join(unknown)}. "
                f"ACTION REQUIRED: use one of {', '.join(PROVIDER_NAMES)}."
            )
        return {**DEFAULT_SCOPES, **value}

    def drafts_on(self, provider: str) -> bool:
        """Whether reply drafts are enabled for ``provider`` in this deployment."""
        return bool(self.drafts_enabled.get(provider, False))

    def drafts_allowed(self, provider: str) -> bool:
        """Whether a draft may actually be composed. BOTH gates, one answer.

        The AND of the client posture and the provider scope flag, and the only
        sanctioned way to ask the question. A caller comparing ``posture`` by
        hand would be a second implementation of the narrowing rule, and the
        two would diverge the first time a member is added.

        ``drafts_on`` remains separate and is NOT this: it answers the SCOPE
        question -- whether this deployment asks Google for ``gmail.compose``
        -- and posture must never reach that answer. Widening a scope from a
        client-level toggle is the failure this split exists to make
        impossible.

        Args:
            provider: The provider name whose scope flag applies.

        Returns:
            True only when posture permits drafting AND the provider's
            ``drafts_enabled`` flag is set.
        """
        return self.posture is Posture.FILE_READ_DRAFT and self.drafts_on(provider)

    def mailbox_scope(self, provider: str) -> MailboxScope:
        """The installed scope for ``provider``. Always present, so nothing raises.

        An unknown or unconfigured provider name returns ``default_scope()``
        rather than raising. The validator has already refused an unknown key at
        load time, so the only way to reach this fallback is a caller asking
        about a name nobody installed -- and the answer to "what may I read in a
        mailbox nobody configured" is Inbox only, never an exception a caller
        might handle by widening.

        The fallback is the UNIVERSAL default rather than a ``DEFAULT_SCOPES``
        lookup, and deliberately so: this arm is reached only by a name that is
        not a known provider, so there is no mailbox whose nesting convention
        could be described. ``inbox_only`` names no folders, so the flag has
        nothing to qualify here in any case.
        """
        return self.scope.get(provider, default_scope())

    def require_routing(self) -> VoiceRouting:
        """Return the routing table, or raise with the command that installs one."""
        if self.routing is None:
            raise ConfigError(
                "No voice routing is configured, so create_draft cannot run. "
                "ACTION REQUIRED: run 'uv run inbox-zero-connector init-config "
                "--owner-domain example.com', or install the kit's voice interview "
                "output at the path 'inbox-zero-connector paths' reports."
            )
        return self.routing


def default_config() -> ConnectorConfig:
    """Drafts off everywhere, no routing. The safest possible starting state.

    Drafts default to FALSE on both providers rather than to the capability
    each provider happens to allow for free. A default that silently requests
    a send-capable Gmail scope because somebody installed without reading is
    exactly the failure this kit exists to not have.

    ``posture`` follows the same instinct and is ``file_only``. It is passed
    EXPLICITLY rather than left to the field default so this function reads as
    the complete statement of the safe starting state -- somebody auditing what
    a fresh install does should not have to go read a field default to find out
    whether drafting is on.
    """
    return ConnectorConfig(
        drafts_enabled=dict.fromkeys(PROVIDER_NAMES, False),
        posture=Posture.FILE_ONLY,
    )


class ClassifierSettings(Section):
    """WHERE mail text is sent for classification, and what answers there.

    THIS SECTION IS THE VENDOR SWITCH, and it is the whole of it. The
    classifier speaks the OpenAI chat-completions PROTOCOL, which OpenRouter,
    OpenAI, Azure OpenAI, and local servers (MLX, Ollama, LM Studio, vLLM) all
    serve, so pointing this deployment at a different model backend is these
    three fields and nothing else -- no second class, no code change, no
    redeploy. That is the vendor-agnostic doctrine made mechanical: rTech does
    not host the client's inference and does not pick their model.

    IT IS ALSO THE CONNECTOR'S ONLY CONFIGURED EGRESS DESTINATION, which is why
    the fields carry the warnings they do rather than being three plain
    settings. ``base_url`` is the address the sender, subject, and snippet of
    each classified message are sent to. Every OTHER host this connector can
    reach is a compiled-in literal held to the allowlist in
    ``tests/test_containment.py``; this one arrives at runtime, from the
    operator, by design. See ``base_url`` for the posture that follows from
    that, and ``tests/test_containment.py`` ``ALLOWED_HOST_LITERALS`` for why
    that scan is not weakened by this section existing.
    """

    base_url: str | None = None
    """The chat-completions endpoint. ``INBOX_ZERO_CLASSIFIER__BASE_URL``.

    NO DEFAULT, DELIBERATELY, and the reason is not caution about a value --
    it is that this connector must not ship anyone else's infrastructure.
    ``classifiers.openai_compatible.DEFAULT_BASE_URL`` is ``None`` for the same
    reason and records the argument in full: a module that ships a working URL
    invites every deployment to inherit one mailbox's server, and a hardcoded
    endpoint is precisely what a configurable vendor switch exists to not be.

    A FRESH INSTALL IS EXPECTED TO POINT AT A LOCAL SERVER the owner controls,
    because the workload -- nine-way filing over three short fields -- runs
    fine on a small instruct model, and once it does, mail content never leaves
    the client's network at all. For a buyer whose objection is exfiltration,
    a network boundary is a stronger answer than any vendor's retention policy,
    because there is no third party in the path to make a promise about.

    ``None`` leaves the OpenAI SDK on its own default endpoint, which is
    OpenAI's. That is a deliberate failure mode rather than an oversight: the
    cost of forgetting to configure this is a 401 from a documented vendor,
    never a silent connection to an unexpected host.

    THE OPERATOR CAN POINT THIS ANYWHERE, INCLUDING SOMEWHERE HOSTILE, and that
    is accepted rather than blocked -- a connector that refuses to talk to the
    model backend its owner chose is not a product. The mitigation is
    VISIBILITY, not prohibition: this value is operator-set, operator-auditable,
    and belongs in the posture output an auditor reads. See
    ``tests/test_containment.py`` ``ALLOWED_HOST_LITERALS``, which argues the
    full posture and records what this does and does not leave open.
    """

    model: str = "Qwen3-4B-Instruct-2507-4bit"
    """The model id at that endpoint. ``INBOX_ZERO_CLASSIFIER__MODEL``.

    A small, fast, tool-calling instruct model, matching
    ``classifiers.openai_compatible.DEFAULT_MODEL``. Classification runs once
    PER MESSAGE across a whole mailbox, so latency multiplies by inbox volume
    while the task itself stays a nine-way choice the taxonomy descriptions
    already make explicit. A model id is not an endpoint and names no host, so
    unlike ``base_url`` it carries a real default.
    """

    requires_key: bool = False
    """Whether that endpoint demands a credential. ``INBOX_ZERO_CLASSIFIER__REQUIRES_KEY``.

    CONFIGURATION, NEVER INFERENCE. ``False`` matches the local-server default;
    ``True`` makes an absent key a startup failure naming the variable rather
    than a 401 on the first message of a run.

    The rejected alternative was guessing from the URL -- treating loopback or
    an RFC-1918 address as keyless. That is a heuristic deciding a
    security-relevant question, it is wrong for an authenticated server on a
    private network, and it would silently stop sending a credential the day
    someone put a local gateway behind auth.
    """


# ---------------------------------------------------------------------------
# Credentials -- names are committed config, values never touch disk.
#
# These are the buyer's own app-registration identifiers, created in the
# buyer's own tenant. They are read from the environment so they never land in
# this repo, and they are never logged (the field names match the redaction
# table in logging_config).
# ---------------------------------------------------------------------------


class CredentialSettings(Section):
    """OAuth CLIENT identifiers for the buyer's own app registrations.

    NOT user credentials. The user's token lives in the OS keychain and never
    passes through this model -- see ``auth``. These identify the app
    registration the buyer created in their OWN Google Cloud project or Entra
    tenant, which is what keeps rTech out of the token chain entirely
    (vendor-agnostic doctrine, 2026-08-02).
    """

    google_client_id: str = ""

    google_client_secret: SecretStr = SecretStr("")
    """The Google OAuth client secret. ``INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_SECRET``.

    ``SecretStr``, NOT ``str``, for the reason ``classifier_api_key`` below
    states at length: a plain ``str`` renders in full from ``repr()``, from
    ``model_dump_json()``, and from any exception that quotes the settings
    object, and all three end up in a log file. THE ODD ONE OUT AMONG THE
    IDENTIFIERS -- ``google_client_id``, ``microsoft_client_id``, and
    ``microsoft_tenant_id`` name a registration and are correct as plain
    ``str``; this one is the paired SECRET, and Google's installed-app flow
    treats it as a credential even though it cannot be kept truly confidential
    in a desktop client.

    Read the real value with ``.get_secret_value()`` at the boundary that builds
    the request -- ``require_google`` does that unwrap, so ``auth.keychain``
    receives the plain string the Google SDK needs and no caller changed.
    """

    microsoft_client_id: str = ""
    """The Entra application (client) ID. Shipped, not blank, in practice.

    The code default here is empty, but ``config/connector-config.json`` ships
    rTech's own MULTI-TENANT PUBLIC-CLIENT registration, so a fresh install
    signs in without registering anything. That is safe because a public client
    holds no secret: the value is an identifier the same way a bundle id is, the
    consent screen is the user's own, and the resulting token is minted straight
    into the user's OS keychain. rTech never sees it -- the no-token-chain
    guarantee is about where the TOKEN lives, not about who owns the app id.

    A client who wants the registration inside their own tenant overrides this
    (and usually ``microsoft_tenant_id``) via the config file or
    ``INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID``; nothing else changes.
    """

    microsoft_tenant_id: str = "common"
    """The authority segment: ``common``, ``organizations``, or a tenant GUID.

    ``common`` rather than ``organizations`` because rTech's registration is
    audience ``AzureADandPersonalMicrosoftAccount``, and ``organizations``
    refuses a personal Microsoft account outright -- it would narrow the app
    below what its own registration allows, for no security gain. Left unset in
    the shipped config so this default applies; a single-tenant client sets
    their Directory (tenant) ID here.
    """

    classifier_api_key: SecretStr | None = None
    """The model backend's key. ``INBOX_ZERO_CREDENTIALS__CLASSIFIER_API_KEY``.

    ``SecretStr``, NOT ``str``, and the difference is the only thing standing
    between this value and a log sink. A plain ``str`` prints in full from
    ``repr()``, from ``model_dump_json()``, and from any exception whose message
    quotes the settings object -- three surfaces that all end up in a log file
    and none of which anybody remembers to check. Enforced by
    ``_DOCS/STANDARDS-python.md`` ``credential-not-secretstr``; read the real
    value with ``.get_secret_value()``, at the boundary that builds the request.

    THE ODD ONE OUT IN THIS SECTION, and deliberately so. The four fields above
    are OAuth CLIENT IDENTIFIERS -- they name the buyer's own app registration
    and are correct as plain ``str``, the same way a vault item name is. This
    one is a bearer credential for a third-party API: whoever holds it can spend
    the client's money against that endpoint. Different kind of value, different
    annotation.

    ``None`` rather than ``""`` because absent and empty are different answers
    here. A local server that wants no credential is the default posture, and
    ``classifier.requires_key`` -- not the emptiness of this field -- is what
    states whether one is mandatory.
    """

    def require_google(self) -> tuple[str, str]:
        """Return ``(client_id, client_secret)``, or raise naming the variables.

        THE UNWRAP SITE for ``google_client_secret``. The field is a
        ``SecretStr`` so it cannot render itself into a log; the Google SDK
        needs the plain string, and this is the one boundary that builds the
        client config for it (``auth.keychain.google_client_config``). Keeping
        the unwrap here rather than at the caller means the return type is
        unchanged and no caller had to learn about the wrapper.
        """
        if not self.google_client_id or not self.google_client_secret.get_secret_value():
            raise ConfigError(
                f"Google OAuth client is not configured. ACTION REQUIRED: set "
                f"{ENV_PREFIX}CREDENTIALS__GOOGLE_CLIENT_ID and {ENV_PREFIX}CREDENTIALS__GOOGLE_CLIENT_SECRET to "
                "the OAuth client you created in your own Google Cloud project. See the "
                "connector README, section 'Bring your own app registration'."
            )
        return self.google_client_id, self.google_client_secret.get_secret_value()

    def require_microsoft(self) -> tuple[str, str]:
        """Return ``(client_id, tenant)``, or raise naming the variable."""
        if not self.microsoft_client_id:
            raise ConfigError(
                f"Microsoft app registration is not configured. ACTION REQUIRED: set "
                f"{ENV_PREFIX}CREDENTIALS__MICROSOFT_CLIENT_ID to an Entra application (client) ID. "
                "The shipped config/connector-config.json carries rTech's multi-tenant public "
                "client, so this normally only appears when that file is missing or was "
                "overridden. See the connector README, section 'Bring your own app registration'."
            )
        return self.microsoft_client_id, self.microsoft_tenant_id


# ---------------------------------------------------------------------------
# The root settings object.
# ---------------------------------------------------------------------------


class Settings(AccountsMixin, BaseSettings):
    """Root configuration. Built ONCE at startup, passed down explicitly.

    Prefer ``load_settings``, which also configures logging. Constructing this
    directly is a test affordance.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="forbid",
        validate_assignment=True,
    )

    # Read by settings_customise_sources, which is a classmethod with no
    # per-call channel. `load_settings` is the only sanctioned constructor
    # precisely so this coupling has exactly one place to go wrong.
    _config_file: ClassVar[Path | None] = None

    env: str = Field(default_factory=env_name, pattern=ENV_NAME_PATTERN.pattern)
    """Names the log files, so two deployments on one machine never share one.

    Seeded from ``INBOX_ZERO_ENV`` through ``env_name``, which is the same
    function ``_layered_files`` calls to choose the per-environment config
    layer -- so the layer read and the value loaded cannot disagree about which
    environment this is. That used to be a separate ``_env_name`` ClassVar
    evaluated once at import; per-call resolution is what lets an operator (and
    a test) set the variable and have BOTH follow it.

    The ``pattern`` is not redundant with ``env_name``'s own refusal. That
    function guards the ENVIRONMENT variable; this field is also settable from
    the JSON layers and from init kwargs, and it reaches ``logging_config.setup``
    which interpolates it into the log file names -- so the constraint has to sit
    on the FIELD to cover every source (#163).
    """

    install_id: str = ""
    """Names THIS install, so two installs on one machine never share state.

    When set, it becomes the last segment of the config and data directories and
    the suffix on every keychain account, so a second mailbox on the same laptop
    gets its own taxonomy, ledger, learned rules, and stored token instead of
    silently overwriting the first one's. ``INBOX_ZERO_INSTALL_ID``.

    Every custom zip carries one by default (``build_custom.py`` sets it to the
    profile name), so managed installs are isolated without anybody choosing to
    be. Empty is the un-namespaced historical layout and keeps working exactly.

    Path resolution reads this value through ``install_id()`` rather than
    through this field, because directories must resolve before a ``Settings``
    instance can exist; the field is here so the value is visible, validated,
    and settable like every other setting.
    """

    @field_validator("install_id")
    @classmethod
    def _usable_install_id(cls, value: str) -> str:
        """Refuse an id that cannot be a directory name or a keychain account."""
        if value and not INSTALL_ID_PATTERN.match(value):
            raise ValueError(
                f"install_id {value!r} is not usable: it must match "
                f"{INSTALL_ID_PATTERN.pattern}. ACTION REQUIRED: use only letters, "
                "digits, dot, dash, or underscore, 64 characters or fewer."
            )
        return value

    provider: ProviderName = "google"
    """Which mailbox this deployment talks to. ``INBOX_ZERO_PROVIDER``."""

    google_app_status: Literal["testing", "published"] = "testing"
    """Whether the buyer's OWN Google Cloud app is still in Testing. ``INBOX_ZERO_GOOGLE_APP_STATUS``.

    This is not cosmetic and it is not something the connector can discover.
    Google expires the REFRESH token of an app in Testing after seven days,
    which is the connector's single most predictable failure: the watcher runs
    fine all week and then stops, and the operator has no warning because the
    ACCESS token's own hour-long expiry (which ``creds.expired`` reports) says
    nothing about it. ``services.auth.grants.refresh_token_expiry`` turns this
    field plus the recorded grant time into the advance warning.

    Defaults to ``testing`` because that is where every freshly-created Cloud
    project starts and because the two wrong answers are not symmetric:
    defaulting to ``published`` on an app that is actually in Testing produces
    NO warning before the exact failure this exists to predict, while the
    reverse produces a warning that turns out to be unnecessary. Only the buyer
    knows which state their own project is in -- pushing an app to Production
    happens in their Cloud console, leaves no trace on the token, and is
    therefore theirs to declare here.

    Microsoft has no equivalent window, so this field names Google explicitly
    rather than pretending to be a provider-neutral setting.
    """

    folder_prefix: str = DEFAULT_FOLDER_PREFIX
    """String pasted in front of every taxonomy folder name. ``INBOX_ZERO_FOLDER_PREFIX``.

    Empty by default -- see ``DEFAULT_FOLDER_PREFIX``. Both adapters read this
    one value and concatenate it identically, so a mailbox cannot end up with
    prefixed folders on one provider and bare ones on the other.
    """

    @field_validator("folder_prefix")
    @classmethod
    def _bounded_prefix(cls, value: str) -> str:
        """Refuse an oversized prefix at LOAD, not at the first filing.

        NOT stripped. Trailing punctuation is the entire point of a prefix
        (``InboxZero-``), and a separator is just as load-bearing when it is a
        space, so normalising whitespace here would silently rename every folder
        an operator asked for.
        """
        if len(value) > MAX_FOLDER_PREFIX_CHARS:
            raise ValueError(
                f"folder_prefix is {len(value)} characters; the limit is "
                f"{MAX_FOLDER_PREFIX_CHARS}. ACTION REQUIRED: shorten it -- the prefix "
                "is pasted in front of every folder this connector creates in the "
                "owner's mailbox."
            )
        refuse_unusable_prefix(value)
        return value

    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    credentials: CredentialSettings = Field(default_factory=CredentialSettings)

    classifier: ClassifierSettings = Field(default_factory=ClassifierSettings)
    """Where mail text is sent for classification. ``INBOX_ZERO_CLASSIFIER__*``.

    NO FIELD VALIDATOR ON THIS FIELD, and that is a correctness requirement
    rather than an omission. Pydantic does not run a field validator over a
    ``default_factory``, so a validator here would silently never fire for an
    unset ``classifier`` -- exactly the defect recorded on ``ConnectorConfig.scope``,
    where an unset value skipped ``_known_providers_only_for_scope`` and stayed
    ``{}``. Every rule this section needs is a per-field constraint declared on
    ``ClassifierSettings`` itself, which pydantic DOES apply when the factory
    constructs it, so there is no rule here that a skipped validator could drop.
    """

    drafts_enabled: dict[str, StrictBool] = Field(
        default_factory=lambda: dict.fromkeys(PROVIDER_NAMES, False)
    )

    posture: Posture = Posture.FILE_ONLY
    """The client-level posture. ``INBOX_ZERO_POSTURE``.

    Layered like ``voice_routing`` and NOT exempted the way ``drafts_enabled``
    is, because the environment cannot use this field to widen anything: the
    only thing posture can do is refuse drafts the scope flag already
    permitted. An operator narrowing a live deployment from the environment is
    a legitimate override and the widening direction does not exist here.

    A value outside the ``Posture`` members is a load-time ValidationError,
    which is the whole reason it is an enum -- see ``Posture``.
    """

    voice_routing: VoiceRouting | None = None

    mailbox_scope: dict[str, MailboxScope] = Field(default_factory=lambda: dict(DEFAULT_SCOPES))
    """Per-provider folder scope. ``INBOX_ZERO_MAILBOX_SCOPE__GOOGLE__MODE`` etc.

    Layered exactly like ``voice_routing``: the installer's JSON is the lowest
    source and the environment can override it, per the precedence tuple in
    ``settings_customise_sources``. Unlike ``drafts_enabled`` this is NOT
    exempted from that order -- an operator narrowing scope from the
    environment is a legitimate override, and the widening direction is bounded
    by the same validators either source has to satisfy.

    The env path gained one nesting level with the per-provider key, so the
    variable naming a Gmail mode is now ``..._MAILBOX_SCOPE__GOOGLE__MODE``.
    The provider segment is the same name ``drafts_enabled`` uses, so an
    operator who can set one can set the other without a second vocabulary.
    """

    @field_validator("drafts_enabled", mode="before")
    @classmethod
    def _validate_like_connector_config(cls, value: object) -> object:
        """Delegate to ``ConnectorConfig`` so both models enforce ONE rule.

        Two copies of the strict-bool and known-provider checks would be two
        places for the send-capable-scope guard to drift.
        """
        return ConnectorConfig(drafts_enabled=value).drafts_enabled  # type: ignore[arg-type]

    @field_validator("mailbox_scope")
    @classmethod
    def _validate_scope_like_connector_config(
        cls, value: dict[str, MailboxScope]
    ) -> dict[str, MailboxScope]:
        """Delegate to ``ConnectorConfig`` so both models enforce ONE rule.

        Same reason the ``drafts_enabled`` delegation above exists: the
        unknown-provider refusal and the fill-with-``default_scope`` step are a
        data-exposure guard, and a second copy is a second thing to drift. In
        particular this is what makes an env layer that names only one provider
        leave the other at the narrow default instead of dropping it.
        """
        return ConnectorConfig(scope=value).scope

    @classmethod
    def _layered_files(cls) -> list[Path]:
        """Every JSON layer, lowest precedence first. The order IS the rule.

        Audited config first, then the credential files on top -- the order the
        module docstring states, unchanged. What changed is that the two halves
        are nameable separately, because they answer different questions and
        exactly one of them is evidence of the deployment's posture (#156).

        REFUSES HERE, before returning, when a credential layer states anything
        but ``credentials``. This is the one function every load path goes
        through to reach those files, so the check cannot be bypassed by a
        construction route that skips a validator -- and the reasoning for the
        rule lives with it in ``config_paths.checked_credential_files``.
        """
        root = PROJECT_ROOT
        return [*audited_config_files(root, cls._config_file), *checked_credential_files(root)]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Declare the source chain, highest precedence first.

        This tuple IS the precedence order stated in the module docstring. It
        cannot drift from the prose, because it is not a description of the
        behaviour -- it is the behaviour.

        Args:
            settings_cls: The settings class being built, passed to the JSON
                source, which needs it to read ``model_config``.
            init_settings: Keyword arguments passed to ``Settings(...)``. Tests.
            env_settings: ``INBOX_ZERO_``-prefixed environment variables.
            dotenv_settings: Unused. This connector keeps no ``.env``; a real
                one would be a credential file on disk, which ``auth`` refuses
                by design.
            file_secret_settings: Unused. Same reason.

        Returns:
            Sources in descending precedence.
        """
        return (
            init_settings,
            env_settings,
            # The installer's decisions are the LOWEST layer: an operator's
            # environment override must win over a stale installed file, never
            # the reverse. A missing file is skipped rather than fatal --
            # absence means the Stage 1 posture (drafts off, no routing), which
            # is strictly narrower than any configured state.
            #
            # ONE FIELD IS EXEMPT FROM THAT ORDER: `drafts_enabled`. It is not a
            # preference, it is the SECURITY POSTURE, and it is validated
            # against the file by `_env_may_not_flip_the_posture` below. See
            # that validator for why silent precedence is the wrong answer for
            # this field specifically.
            #
            # Five layers, one source, lowest first: committed config/, committed
            # config/ per-env, the installed per-user file, the per-user
            # secrets.json the human fills in, then repo secrets/. deep_merge so
            # the credential files overlay their siblings instead of replacing
            # the group -- which is what lets the installed file hold the client
            # ID while secrets.json holds only the secret. A fresh clone runs
            # from the committed files alone.
            JsonConfigSettingsSource(settings_cls, json_file=cls._layered_files(), deep_merge=True),
        )

    @model_validator(mode="after")
    def _env_may_not_flip_the_posture(self) -> Settings:
        """Refuse to start when the environment CONTRADICTS the installed posture.

        ``drafts_enabled`` decides whether Gmail asks Google for
        ``gmail.compose``, which is what moves the no-send guarantee from the
        SCOPE layer to the CODE layer (see ``scopes``). Every other setting here
        is a preference where "the operator's environment wins" is the right
        rule. This one is a claim the buyer can audit from their own admin
        console and read in their own config file -- so if the environment could
        quietly outrank the file, the file a human reads would say scope-layer
        while the connector actually ran code-layer, and nothing anywhere would
        report the difference.

        The fix is NOT to make the environment lose silently, which would strand
        an operator who set the variable deliberately and got no effect. It is
        to make DISAGREEMENT loud: agreeing values load fine, an absent file
        leaves the environment as the only statement anybody made, and a genuine
        conflict fails at startup naming both sources so whoever set them
        decides which one is true.

        Returns:
            Self, unchanged, when the two sources agree or only one exists.

        Raises:
            ValueError: The environment and the installed file disagree. Wrapped
                as ``ConfigError`` by ``load_settings``.
        """
        env_value = os.environ.get(f"{ENV_PREFIX}DRAFTS_ENABLED")
        if env_value is None:
            return self

        stated = _file_posture()
        if stated is None:
            # Nothing persisted in ANY layer, so there is no source of truth to
            # contradict.
            return self
        installed_value, target = stated

        persisted = ConnectorConfig(drafts_enabled=installed_value).drafts_enabled  # type: ignore[arg-type]
        if persisted == self.drafts_enabled:
            return self

        raise ValueError(
            f"drafts_enabled disagrees between the environment and the installed "
            f"config. {ENV_PREFIX}DRAFTS_ENABLED asks for {self.drafts_enabled!r}, "
            f"but {target} records {persisted!r}. This field is the no-send "
            "SECURITY POSTURE, not a preference: enabling Gmail drafts requests a "
            "send-capable compose scope and moves the guarantee from the scope layer "
            "to the code layer, so the file a human can read must not silently "
            "disagree with what the connector does. ACTION REQUIRED: unset "
            f"{ENV_PREFIX}DRAFTS_ENABLED to keep the installed posture, or re-run "
            "'uv run inbox-zero-connector init-config --force' to persist the new one."
        )

    @model_validator(mode="after")
    def _announce_a_posture_that_will_not_get_what_it_asked_for(self) -> Settings:
        """Say LOUDLY at load when ``file_read_draft`` will produce no drafts.

        WARNING, not a raise, and the difference is the narrowing rule. The
        ``drafts_enabled`` env-versus-file check above RAISES because the two
        sources CONTRADICT each other about a security posture and neither can
        be preferred without lying to somebody. This is not that. Posture and
        ``drafts_enabled`` do not contradict: they compose, and they compose in
        exactly one direction, so the connector already knows the answer --
        no drafts. Refusing to start would take a deployment down over a state
        whose outcome is the SAFE one, which is the wrong trade for a kit whose
        product is filing mail; the drafts are the extra.

        What it must not do is be SILENT. An installer who set
        ``file_read_draft`` asked for a capability and is not getting it, and
        the only thing worse than not getting it is not being told. So the
        message names both fields, both values, the outcome, and the exact
        command that resolves it -- the same shape the disagreement check uses
        for the same reason: the operator reading a log line is the person who
        has to fix it.

        Fires per PROVIDER, because ``drafts_enabled`` is per provider: a
        deployment with Gmail off and Graph on has one real gap and one working
        mailbox, and a single collapsed message would hide which is which.
        """
        if self.posture is not Posture.FILE_READ_DRAFT:
            return self
        blocked = sorted(name for name, flag in self.drafts_enabled.items() if not flag)
        if not blocked:
            return self
        for provider in blocked:
            logger.warning(
                f"CONFIG: posture is {Posture.FILE_READ_DRAFT.value!r} but "
                f"drafts_enabled.{provider} is false, so NO DRAFTS will be composed "
                f"for {provider}. Posture can only NARROW: it never enables drafts, "
                f"because drafts_enabled is what decides whether this deployment "
                f"requests a send-capable Gmail scope and that decision has one "
                f"home. ACTION REQUIRED: re-run "
                f"'uv run inbox-zero-connector init-config --force "
                f"--enable-{provider}-drafts' to enable them, or set posture to "
                f"'{Posture.FILE_ONLY.value}' to record that filing is all this "
                f"deployment does."
            )
        return self

    def connector_config(self) -> ConnectorConfig:
        """Project the installer-owned slice back out as a ``ConnectorConfig``."""
        return ConnectorConfig(
            drafts_enabled=self.drafts_enabled,
            posture=self.posture,
            routing=self.voice_routing,
            scope=dict(self.mailbox_scope),
        )


def _file_posture() -> tuple[object, Path] | None:
    """``drafts_enabled`` as the layered files state it, with the layer that said it.

    Returns the RAW value plus its source path, not a bool: in this connector
    ``drafts_enabled`` is per provider (``{"google": false, ...}``), and the
    caller both validates it through ``ConnectorConfig`` and names the file in
    its refusal. None means no layer stated it at all.

    Reads the AUDITED layers only (``audited_config_files``), lowest-first,
    which is the same order and the same files the loader reads for every
    non-credential field. It deliberately does NOT read the credential layers:
    this function's answer is "what the deployment's own config file records",
    and ``secrets.json`` is a file nobody audits. Sourcing the answer from there
    let a hand-edited credential file AGREE with the environment and silence a
    disagreement the connector-config file was still stating (#156) -- the guard
    would report "the two sources agree" about a file it had never read.

    The credential layers cannot smuggle the value back in either: they are held
    to ``credentials``-only keys by ``checked_credential_files``, so
    ``drafts_enabled`` never appears in one. Last audited layer to state it wins,
    which is exactly what ``deep_merge`` does to the loaded value.
    """
    posture: tuple[object, Path] | None = None
    for layer in audited_config_files(PROJECT_ROOT, Settings._config_file):
        if not layer.exists():
            continue
        try:
            raw = json.loads(layer.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            # DOCUMENTED FAIL-OPEN. A malformed or unreadable file is not this
            # helper's error to report. `load_settings` already raises a message
            # naming the file and the fix, and duplicating it here would mask
            # that one.
            #
            # Logged at DEBUG rather than WARNING because this path is NOT yet
            # evidence of a problem: on the normal route `load_settings` is about
            # to raise the real error, and a warning here would cry wolf ahead of
            # it every time. What the line buys is the case where the posture
            # cross-check was skipped for a reason nothing else reports --
            # without it, the validator returns "the two sources agree" having
            # never read one of them.
            logger.opt(exception=True).debug(
                f"CONFIG: posture cross-check skipped, config layer unreadable "
                f"path={layer} (load_settings reports the real error)"
            )
            continue
        if isinstance(raw, dict) and "drafts_enabled" in raw:
            posture = (raw["drafts_enabled"], layer)
    return posture


_settings: Settings | None = None


def load_settings(path: Path | None = None, *, configure_logging: bool = True) -> Settings:
    """Build the settings object and configure logging. Call once, at startup.

    Args:
        path: Override the JSON layer's location. Tests and the CLI use it.
        configure_logging: Set the sinks up. False in tests that only want the
            values, so a suite does not rebuild sinks hundreds of times.

    Returns:
        The validated settings.

    Raises:
        ConfigError: If the installed JSON layer or the environment is invalid.
            The message names the file, the field, and the fix.
    """
    target = path or config_path()
    Settings._config_file = target
    try:
        settings = Settings()
    except json.JSONDecodeError as exc:
        # Distinguished from a validation failure on purpose: "the file is not
        # JSON" and "the file is JSON but says something wrong" have different
        # fixes, and one message covering both sends the operator to the wrong
        # one.
        raise ConfigError(
            f"{target}: invalid JSON -- {exc}. ACTION REQUIRED: fix the syntax, or "
            "re-run 'uv run inbox-zero-connector init-config --force' to rewrite it."
        ) from exc
    except ValueError as exc:
        raise ConfigError(
            f"Invalid connector configuration (file: {target}, plus "
            f"{ENV_PREFIX}* environment variables): {exc}. ACTION REQUIRED: fix the "
            "field named above, or re-run 'uv run inbox-zero-connector init-config "
            "--force' to write a valid file."
        ) from exc
    if configure_logging:
        logging_config.setup(settings.logging, env=settings.env)
        logging_config.install_crash_handler()
    return settings


def settings() -> Settings:
    """Return the process-wide settings, building them on first use."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def folder_prefix() -> str:
    """The string both adapters paste in front of a taxonomy folder name.

    Reached from the adapters the same way ``auth.google_client_config`` reaches
    the OAuth identifiers: they call the keystone at use time rather than
    reading the environment themselves. Both adapters take an override in their
    constructor, so a test fixes the prefix without an environment variable and
    this function is never on a test path that did not ask for it.
    """
    return settings().folder_prefix


def reset_settings() -> None:
    """Drop the cached settings. Tests use this; no MCP tool exposes it."""
    global _settings
    _settings = None
    Settings._config_file = None


def load_config(path: Path | None = None) -> ConnectorConfig:
    """Load the installer-owned configuration, or the safe default when absent.

    Absence is NOT loud here, unlike a missing taxonomy. The safe default is
    drafts-off and no routing, which is strictly narrower than any configured
    state: a deployment that never wrote this file behaves exactly like
    Stage 1. A missing taxonomy would silently widen behaviour; a missing
    config cannot.
    """
    return load_settings(path, configure_logging=False).connector_config()


def _dump_json(payload: dict[str, Any]) -> str:
    """Serialize the installer payload. Pydantic owns the shape; json owns the bytes."""
    return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# The write surface, re-exported.
# ---------------------------------------------------------------------------
#
# AT THE BOTTOM ON PURPOSE, and it is the standard cycle break rather than an
# oversight. `config_writer` imports the names above (`ConfigError`,
# `config_path`, `secrets_path`, `_dump_json`), so importing it at the top would
# ask Python for a module that is asking for this one before this one exists.
# By the last line of the file every name it wants is bound, so the import
# resolves. `config` stays the single import site for every caller, which is
# what keeps the split -- forced by the 500-code-line ceiling, see
# `config_writer`'s own docstring -- an implementation detail rather than an
# API change.
# Bound off the MODULE rather than as three `from ... import X as X` blocks.
# Identical public surface -- `config.write_config` and the rest still resolve,
# which is all any caller uses -- in three code lines instead of six. isort
# splits the `as`-form blocks apart (it will not merge them), so the module
# import is the only shape that does not pay a line per name, and this file is
# AT its 500-code-line ceiling.
from inbox_zero_connector import (  # ruff: ignore[module-import-not-at-top-of-file]
    config_writer as _writer,
)

# `IDENTIFIER_FIELDS` comes through the `as` RE-EXPORT rather than a module
# attribute binding, unlike the two functions below. It is declared `Final`
# without a type argument in `config_writer`, and `config_writer` imports THIS
# module -- so through the cycle mypy --strict cannot determine its type from a
# rebinding (`Cannot determine type of "IDENTIFIER_FIELDS"`), while a re-export
# it can. The functions carry their own signatures and bind fine.
from inbox_zero_connector.config_writer import (  # ruff: ignore[module-import-not-at-top-of-file]
    IDENTIFIER_FIELDS as IDENTIFIER_FIELDS,
)

write_config = _writer.write_config
write_secrets_skeleton = _writer.write_secrets_skeleton

# ---------------------------------------------------------------------------
# Voice routing, re-exported.
# ---------------------------------------------------------------------------
#
# AT THE BOTTOM for the same cycle reason as the write surface above:
# `voice_routing` imports `ConfigError` and `VoiceRouting` from this module, so
# by the last line every name it wants is bound. Only the three names src
# callers import through `config` are re-exported -- `RoutingInputs`,
# `VOICE_RULES`, `VoiceRole`, and `voice_for_thread` are imported from
# `voice_routing` directly by the one test that uses them, because a re-export
# with no caller is surface this file pays for in its line ceiling.
from inbox_zero_connector.voice_routing import (  # ruff: ignore[module-import-not-at-top-of-file]
    extract_domain as extract_domain,
)
from inbox_zero_connector.voice_routing import (  # ruff: ignore[module-import-not-at-top-of-file]
    normalize_address as normalize_address,
)
from inbox_zero_connector.voice_routing import (  # ruff: ignore[module-import-not-at-top-of-file]
    resolve_voice as resolve_voice,
)
