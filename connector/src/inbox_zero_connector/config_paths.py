"""Where config and data live on disk, and which install they belong to.

SPLIT OUT OF ``config.py``, NOT OUT FROM UNDER ITS LAW. The keystone rule is
that ONE module reads the environment and the config files; this module is
that module's paths half, re-exported from ``config`` so every existing import
site is unchanged and no third module gains the privilege. It lives in its own
file because ``config.py`` sits against the repo's 500-code-line ceiling and
path resolution is the one part of the keystone with no dependency on
``Settings`` -- it must run BEFORE any settings instance exists, since it
decides where the layered files that build one are read from.

That ordering is the whole reason ``install_id()`` reads a single key straight
out of layer 1 instead of going through ``Settings``: resolving it through
``settings()`` would be a cycle. See ``install_id`` and ``utils/paths.py``.

THE ISOLATION PROPERTY
    An install id namespaces the config directory, the data directory, and
    (via ``services/auth/keychain``) the OS keychain account. Without one, two
    installs on a single machine share all three, and the second ``auth``
    overwrites the first mailbox's token -- which surfaces as mail filed into
    the wrong client's folders. Every custom zip carries an id by default.

    An EMPTY id resolves the historical paths byte-for-byte. Every install that
    predates this module has no id, and an upgrade that relocated them would
    look like a factory reset holding somebody's live mailbox.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Final

from loguru import logger
from platformdirs import user_config_path, user_data_path

from inbox_zero_connector.config_constants import (
    APP_AUTHOR,
    APP_NAME,
    ENV_PREFIX,
    PROJECT_ROOT,
)

# ---------------------------------------------------------------------------
# Paths -- resolved from the environment, HERE, because this is the one module
# permitted to read it.
# ---------------------------------------------------------------------------

CONFIG_DIR_ENV: Final = f"{ENV_PREFIX}CONFIG_DIR"
DATA_DIR_ENV: Final = f"{ENV_PREFIX}DATA_DIR"

INSTALL_ID_ENV: Final = f"{ENV_PREFIX}INSTALL_ID"

INSTALL_ID_PATTERN: Final = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,64}$")
"""What an install id may contain. It becomes a directory name and a keychain
account suffix, so it stays boring on purpose: anything a path separator, a
shell, or a keychain account name would read as structure is refused.

The leading ``(?!\\.{1,2}$)`` refuses the two ids that are path STRUCTURE rather
than names. A dot is not merely useless here -- ``base / "."`` resolves to
``base`` itself, so an install that asked to be isolated silently shares the
un-namespaced install's config, data, and keychain account. ``..`` is worse: it
collapses both directories onto the Application Support root, putting this
install's files beside every other application's. Deeper traversals (``../x``,
``a/b``) were already refused because the character class admits no separator;
these two slip through it precisely because they are made only of dots, which a
dotted id like ``client.2`` legitimately needs.
"""

ENV_NAME_ENV: Final = f"{ENV_PREFIX}ENV"

ENV_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
"""What ``INBOX_ZERO_ENV`` may contain, because it becomes a FILENAME.

The value names the committed ``config/connector-config.{env}.json`` layer and
both log files (``{service}-{env}.log``). Unvalidated it was string-interpolated
straight into those paths, so ``../escaped`` walked the log sink out of the log
directory and the config layer out of ``config/``. Dot is excluded deliberately
rather than incidentally: it is the only character in the traversal, and no
legitimate environment name (``prod``, ``test``, ``staging-eu``) needs one.
"""


def env_name() -> str:
    """Which environment this process is, validated because it becomes a filename.

    ``INBOX_ZERO_ENV`` selects the committed per-environment config layer and
    names both log files. It lives HERE beside ``install_id`` because it is the
    same kind of value: an environment read whose answer becomes PATH structure,
    needed before any ``Settings`` instance exists. ``config`` re-exports it, so
    the keystone LAW ("nothing outside it reads the environment") holds.

    Read per call rather than once at import for the reason the keystone exists
    at all: a value frozen at import time is one no test and no operator can
    change afterwards, and the layer actually loaded would then disagree with
    the variable actually set.

    Returns:
        The environment name, defaulting to ``prod`` when unset or empty.

    Raises:
        ValueError: The value is not a safe token. Refused rather than
            sanitised -- a silently-rewritten name would load a different config
            layer than the operator asked for, the same class of defect
            ``install_id`` refuses for the same reason. ``load_settings`` wraps
            it as ``ConfigError``.
    """
    value = os.environ.get(ENV_NAME_ENV)
    if not value:
        return "prod"
    if not ENV_NAME_PATTERN.match(value):
        raise ValueError(
            f"{ENV_NAME_ENV} {value!r} is not usable: it must match "
            f"{ENV_NAME_PATTERN.pattern}. This value names a config layer and a "
            f"log file, so anything a path reads as structure is refused. "
            f"ACTION REQUIRED: use only letters, digits, dash, or underscore "
            f"(32 characters or fewer) -- for example 'prod' or 'test'."
        )
    return value


_install_id_cache: str | None = None


def _install_id_from_layer_one() -> str:
    """Read ``install_id`` out of the committed ``config/connector-config.json``.

    THE ONE SANCTIONED PEEK OUTSIDE ``Settings``, and it is deliberate rather
    than a shortcut. Path resolution has to answer "which install am I" BEFORE
    a ``Settings`` instance can exist, because ``Settings`` itself loads layers
    whose location this function decides -- reading it through ``settings()``
    would be a cycle. It reads exactly one key, from layer 1 only, and it is
    still inside ``config.py``, so the keystone LAW ("nothing else reads config
    files or the environment") holds unchanged.

    Layer 1 is the right layer because ``build_custom.py`` writes the id there:
    an install's identity travels INSIDE the zip, so it is fixed at build time
    and cannot be lost by a wizard rewrite of the installed per-user layer.

    A missing, unreadable, or malformed file returns ``""`` -- the un-namespaced
    default. A fresh clone has no id and must keep working byte-for-byte.
    """
    global _install_id_cache
    if _install_id_cache is not None:
        return _install_id_cache
    _install_id_cache = ""
    try:
        with (PROJECT_ROOT / "config" / "connector-config.json").open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # Fail-open by design: a missing, unreadable, or malformed layer-1 file
        # yields the un-namespaced default so a fresh clone keeps working
        # byte-for-byte. Logged at DEBUG because absence is the normal case.
        logger.debug("install_id: layer-1 read failed, using un-namespaced default: {}", exc)
        return _install_id_cache
    if isinstance(raw, dict):
        value = raw.get("install_id")
        if isinstance(value, str):
            _install_id_cache = value
    return _install_id_cache


def reset_install_id_cache() -> None:
    """Drop the cached layer-1 read. Tests use this; no MCP tool exposes it."""
    global _install_id_cache
    _install_id_cache = None


def install_id() -> str:
    """The name that isolates THIS install from every other one on the machine.

    ``INBOX_ZERO_INSTALL_ID`` wins; otherwise the ``install_id`` key in the
    committed layer-1 config, which is what every custom zip carries. Empty
    means un-namespaced, which is the historical layout and stays exact.

    Raises ``ValueError`` for anything outside ``INSTALL_ID_PATTERN`` rather
    than sanitising it, because a silently-rewritten id would point one install
    at another install's config directory and keychain account -- the precise
    collision this whole mechanism exists to prevent.
    """
    value = os.environ.get(INSTALL_ID_ENV) or _install_id_from_layer_one()
    if not value:
        return ""
    if not INSTALL_ID_PATTERN.match(value):
        raise ValueError(
            f"install_id {value!r} is not usable: it must match "
            f"{INSTALL_ID_PATTERN.pattern}. ACTION REQUIRED: set a plain name "
            f"(letters, digits, dot, dash, underscore; 64 characters or fewer) "
            f"in {INSTALL_ID_ENV} or in config/connector-config.json."
        )
    return value


def _namespaced(base: Path) -> Path:
    """``base`` with the install id appended, or ``base`` itself when there is none."""
    resolved = install_id()
    return base / resolved if resolved else base


def config_dir() -> Path:
    """Directory holding the taxonomy and the connector config.

    ``INBOX_ZERO_CONFIG_DIR`` overrides the OS-standard location outright, with
    no install-id segment appended; tests rely on that override so they never
    touch the real user config, and an operator who names a directory means
    that directory. Otherwise the OS-standard path gains ``install_id()`` as a
    final segment when one is set, so two installs on one machine never share a
    taxonomy, a config, or a learned-rules store. With no id the path is
    byte-for-byte what it has always been.
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override)
    return _namespaced(user_config_path(APP_NAME, APP_AUTHOR))


def data_dir() -> Path:
    """Directory holding the audit ledger and the logs.

    ``INBOX_ZERO_DATA_DIR`` overrides it, same rationale as ``config_dir``.
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    return _namespaced(user_data_path(APP_NAME, APP_AUTHOR))


KIT_LINKS_URL_ENV: Final = "KIT_LINKS_URL"
"""Override for the kit-links host ``update-check`` asks (#67, D13).

UNPREFIXED, unlike every other variable here, and deliberately: it names an
EXTERNAL SERVICE rather than a setting of this install, and the same name is
what the kit-links repo's own tooling uses. Reading it belongs in this module
because this module is the keystone -- the package's LAW is that one auditable
file touches the environment, and an update checker is not the exception that
earns its own read (``tests/test_standards_enforcement.py``).
"""

KIT_LINKS_URL_DEFAULT: Final = "https://rtech-consulting.com"
"""The public kit-links host. Serves a read-only version endpoint; no zip."""


def kit_links_base_url() -> str:
    """Return the kit-links base URL, honouring ``KIT_LINKS_URL``, no trailing slash."""
    return os.environ.get(KIT_LINKS_URL_ENV, KIT_LINKS_URL_DEFAULT).rstrip("/")


CONTROL_CHAR_LIMIT: Final = " "
"""Everything below a space is a C0 control character. DEL is checked with it."""


def refuse_unusable_prefix(value: str) -> None:
    """Refuse a folder prefix that would push STRUCTURE into the owner's mailbox.

    The prefix is pasted in front of every folder name this connector creates,
    and on Gmail a slash is real HIERARCHY (``a/b`` is a label nested under
    ``a``). ONE TRAILING SLASH IS LEGITIMATE AND STAYS LEGAL: ``InboxZero/`` is
    the documented value that restores the historical Gmail label hierarchy, and
    ``rTech/`` is what a real install ships (``DEFAULT_FOLDER_PREFIX`` records
    the whole trade). Refusing it outright would break a shipped, documented
    capability to fix a bug it is not.

    What is refused is UNBOUNDED structure (#163): a leading slash, an interior
    slash (``A/B/`` re-parents every taxonomy folder two levels deep under
    labels nobody named, and because ``names_encode_nesting`` is True for Google
    an exclusion naming a parent then means something the installer never
    intended), a backslash, and any control character -- all of which reach the
    Gmail and Graph APIs concatenated and unvalidated.

    Args:
        value: The candidate prefix.

    Raises:
        ValueError: The prefix nests more than one level, starts with a
            separator, or holds a backslash or control character.
    """
    control = sorted({char for char in value if char < CONTROL_CHAR_LIMIT or char == "\x7f"})
    if control:
        raise ValueError(
            f"folder_prefix contains control characters {control!r}, which reach the "
            "Gmail and Graph APIs and the owner's folder list exactly as given. "
            "ACTION REQUIRED: use printable characters only."
        )
    if "\\" in value:
        raise ValueError(
            "folder_prefix contains a backslash. Use '/' if you mean a Gmail label "
            "level; a backslash is not a separator in either mailbox and reaches "
            "Windows paths in the logs. ACTION REQUIRED: remove it."
        )
    if value.startswith("/") or "/" in value.rstrip("/") or value.endswith("//"):
        raise ValueError(
            f"folder_prefix {value!r} nests more than one level. A prefix may end in "
            "at most ONE '/', which is the documented Gmail form ('InboxZero/'); a "
            "leading or interior slash re-parents every taxonomy folder under labels "
            "the owner never named, and on Gmail an exclusion naming a parent then "
            "covers folders the installer did not intend. ACTION REQUIRED: use a "
            "single level, like 'InboxZero/' or 'InboxZero-'."
        )


def audited_config_files(root: Path, installed: Path | None = None) -> list[Path]:
    """The config layers a buyer can AUDIT, lowest precedence first.

    These three state the deployment's DECISIONS -- posture, drafts, voice
    routing, mailbox scope. They are the files an auditor is pointed at, and the
    files the posture cross-check reads, which is the whole reason they are
    nameable apart from the credential layers below (#156).

    Args:
        root: The checkout root the committed layers are read from. Passed in
            rather than read from this module's ``PROJECT_ROOT`` because the
            keystone's copy is the one the suite patches to a tmp checkout, and
            a second reader here would silently load the REAL ``config/`` while
            a test believed it had isolated it.
        installed: The installed per-user ``connector-config.json``. ``None``
            resolves ``config_path()``; the keystone passes an explicit path
            when a test or the CLI overrode it.

    Returns:
        ``[committed default, committed per-env, installed]``.
    """
    return [
        root / "config" / "connector-config.json",
        root / "config" / f"connector-config.{env_name()}.json",
        installed or config_path(),
    ]


def credential_files(root: Path) -> list[Path]:
    """The credential layers, lowest precedence first. VALUES ONLY.

    ``secrets.json`` is the human's own file and ``secrets/config.json`` is the
    developer's; neither is read by anybody auditing the deployment, which is
    exactly why ``refuse_non_credential_keys`` holds them to credentials -- a
    posture decision must not acquire a second home in a file nobody reads.

    Args:
        root: The checkout root, passed in for the reason
            ``audited_config_files`` states.
    """
    return [secrets_path(), root / "secrets" / "config.json"]


def checked_credential_files(root: Path) -> list[Path]:
    """Refuse a credential layer that states anything but ``credentials`` (#156).

    ``secrets.json`` and the repo's ``secrets/config.json`` exist for one
    purpose: to keep a credential VALUE out of the file the installer rewrites.
    They sit at the TOP of the file precedence so that purpose works -- and that
    precedence is exactly what made them a full config layer by accident. A
    hand-edited one carrying ``drafts_enabled`` flipped the Gmail compose-scope
    decision from the layer nobody audits, and one carrying ``classifier``
    redirected every mail body to a host of the file author's choosing. Both
    outrank the connector-config file a buyer is told to read, so the audited
    file and the running behaviour could disagree with nothing reporting it.

    A refusal rather than a silent drop, for the reason the keystone refuses
    rather than sanitises everywhere else: somebody put that key there on
    purpose, and a value that quietly does nothing strands them exactly as badly
    as one that quietly does something. The message names the file and the keys.

    An absent, unreadable, malformed, or non-object layer is skipped: that is
    not this function's error to report, because the JSON source is about to
    fail on the same file with a message naming the syntax problem and a second
    one here would mask it.

    Args:
        root: The checkout root, passed in for the reason
            ``audited_config_files`` states.

    Returns:
        The credential layers, lowest precedence first -- the same list
        ``credential_files`` returns, once it has been proven clean. Returning
        them rather than only refusing keeps ONE call at the use site, so a
        caller cannot list the files without also checking them.

    Raises:
        ValueError: A layer states a non-credential top-level key.
            ``load_settings`` wraps it as a ``ConfigError``.
    """
    layers = credential_files(root)
    for layer in layers:
        if not layer.exists():
            continue
        try:
            raw = json.loads(layer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # DOCUMENTED FAIL-OPEN: this helper only screens for non-credential
            # keys; an unreadable or malformed layer is reported by
            # `load_settings`, which raises the operator-facing error naming
            # the file. Duplicating that failure here would mask it.
            logger.debug(
                f"CONFIG: credential-layer screen skipped unreadable file error_type={type(exc).__name__}"
            )
            continue
        if not isinstance(raw, dict):
            continue
        offending = sorted(key for key in raw if key != "credentials")
        if offending:
            named = ", ".join(offending)
            raise ValueError(
                f"{layer} may contain CREDENTIALS ONLY, but it states {named}. This "
                "file is the per-user credential values file and it outranks the "
                "connector-config file a buyer audits, so a posture, drafts, or "
                "classifier key here would change what this deployment does without "
                f"appearing in the file anyone reads. ACTION REQUIRED: move {named} "
                "into connector-config.json (or re-run 'uv run inbox-zero-connector "
                "init-config --force') and leave only a 'credentials' object here."
            )
    return layers


def taxonomy_path() -> Path:
    """Where the closed folder vocabulary lives."""
    return config_dir() / "taxonomy.json"


def config_path() -> Path:
    """The installer's connector config: draft posture and voice routing.

    Separate from the taxonomy on purpose. The taxonomy is the mailbox's
    filing vocabulary and is expected to be re-interviewed; this file records
    a SECURITY POSTURE decision (whether Gmail drafts, and therefore the
    compose scope, are enabled) and should not be rewritten by a taxonomy
    refresh.
    """
    return config_dir() / "connector-config.json"


def secrets_path() -> Path:
    """The per-user credential VALUES file, filled in by the human.

    Separate from ``config_path()`` for one reason: that file is the
    INSTALLER'S, rewritten whenever the wizard runs, and a secret sitting in a
    file a script rewrites is a secret one bad rewrite away from being lost or
    one support request away from being pasted into a ticket. This file is
    NEVER committed and NEVER written by any prompt -- ``write_secrets_skeleton``
    creates it empty, the human types the values in, and no code path in this
    connector puts a real value here.
    """
    return config_dir() / "secrets.json"


def voice_profiles_path() -> Path:
    """The owner's writing-style profiles, one row per voice.

    In the CONFIG directory beside the taxonomy and the learned rules, not the
    data directory: a profile is an installed artifact the owner reads and
    approves, like the taxonomy, rather than an accumulating record like the
    ledger or the logs.
    """
    return config_dir() / "voice-profiles.json"


def ledger_path() -> Path:
    """The append-only audit ledger."""
    return data_dir() / "ledger.jsonl"
