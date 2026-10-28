"""The installer's WRITE surface: the only code that puts config on disk.

``config.py`` next door READS. This module WRITES, and the split is at its
code-line ceiling for a reason recorded in ``CHANGELOG.md``: compressing working
code to make room is what caused a regression the last time it was tried, so a
whole unit moves out along a real seam instead. Read/write is that seam, and it
is a security seam as much as a size one -- everything that can modify the
installed posture, the OAuth identifiers, or the secrets file is in this one
file, so "what can rewrite my config" has a single answer to audit.

NOT REACHABLE FROM ANY MCP TOOL. The host AI cannot call anything here, so it
can neither enable drafts, nor rewrite the routing table it is subject to, nor
write a credential anywhere.

THE WRITERS DIVIDE BY WHAT THE VALUE IS:

* ``write_config`` owns the INSTALLED ``connector-config.json`` -- posture,
  routing, scope, the vendor switch, and OAuth IDENTIFIERS held to
  ``IDENTIFIER_FIELDS``. The installer rewrites this file freely, so a secret
  VALUE must never enter it.
* ``write_secrets_skeleton`` creates ``secrets_path()`` EMPTY and never touches
  it again. The human types the values in. No code path here writes a real
  credential to disk.
* ``ensure_install_id`` owns ONE key in the COMMITTED layer-1 config, and is
  the only writer that touches a file inside the install folder rather than the
  per-user config directory. It has to: the id it writes is what RESOLVES that
  directory, so it cannot live there. It writes once per install and never
  again -- see its own docstring for why re-minting would orphan a mailbox.

``config.py`` re-exports the first two, so callers keep importing them from the
keystone and this split stays an implementation detail. ``ensure_install_id``
is imported from HERE by its two callers, because the keystone sits one line
under its 500-code-line ceiling and a third re-export block would breach it.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from loguru import logger

from inbox_zero_connector import config as config_keystone
from inbox_zero_connector.config import (
    ENV_PREFIX,
    ConfigError,
    _dump_json,
    config_path,
    secrets_path,
)
from inbox_zero_connector.config_paths import reset_install_id_cache

if TYPE_CHECKING:
    from collections.abc import Sequence

    from inbox_zero_connector.accounts import AccountSettings
    from inbox_zero_connector.config import ClassifierSettings, ConnectorConfig


IDENTIFIER_FIELDS: Final = ("google_client_id", "microsoft_client_id", "microsoft_tenant_id")
"""The ONLY credential-group keys ``write_config`` will put in the installed file.

An allowlist, not a denylist, and that direction is the point. These three name
the buyer's own app registration -- they are identifiers, like a vault item name,
and belong in the file the installer rewrites and a support request may quote. A
denylist would let the next credential field added to ``CredentialSettings``
default into the writable file; this way it defaults OUT, and adding it here is a
deliberate act with this docstring in front of it.
"""


def write_config(
    config: ConnectorConfig,
    path: Path | None = None,
    *,
    identifiers: dict[str, str] | None = None,
    classifier: ClassifierSettings | None = None,
    accounts: Sequence[AccountSettings] | None = None,
) -> Path:
    """Write configuration to disk. Installation-time only.

    Not reachable from any MCP tool: the host AI cannot call this, so it can
    neither enable drafts nor rewrite the routing table it is subject to.

    Args:
        config: The posture, routing table, and per-provider mailbox scope.
        path: Override the target. Defaults to ``config_path()``.
        identifiers: OAuth CLIENT IDENTIFIERS to record in the ``credentials``
            group, keys held to ``IDENTIFIER_FIELDS``. Merged OVER whatever the
            existing file already states, per key, so a wizard pass that knows
            only the Microsoft IDs does not blank the Google one. Empty-string
            values are skipped rather than written, because "the human pressed
            enter" is not an instruction to erase what is there. ``None`` leaves
            an existing block untouched -- the wizard rewrites this file for
            unrelated reasons and a rewrite that dropped the block would
            silently un-configure the OAuth client.
        classifier: The vendor switch, written whole. ``None`` LEAVES an
            installed block alone rather than dropping it -- ``init-config
            --force`` re-runs with a narrower flag set than the ``setup``
            wizard used, and a rebuild-from-scratch write silently reverted
            those installs to rule-only classification. Carries NO key field:
            ``ClassifierSettings`` has none, and the API key lives only in
            ``secrets_path()`` or the environment.
        accounts: The mailbox list the wizard collected, FIRST ONE FIRST.
            ``None`` LEAVES an installed list alone rather than dropping it, for
            the same reason ``classifier`` does: ``init-config`` re-runs with a
            narrower flag set than the ``setup`` wizard used, and a
            rebuild-from-scratch write would silently forget every account after
            the first. An EMPTY sequence is a different instruction and does
            clear the key -- that is a wizard pass that asked and was told none.

    Returns:
        The path written.

    Raises:
        ConfigError: ``identifiers`` names a key outside ``IDENTIFIER_FIELDS``.
    """
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = _existing_payload(target)
    payload["drafts_enabled"] = dict(config.drafts_enabled)
    payload["posture"] = config.posture.value
    if config.routing is not None:
        payload["voice_routing"] = config.routing.model_dump(mode="json")
    payload["mailbox_scope"] = {
        name: scope.model_dump(mode="json") for name, scope in config.scope.items()
    }
    if classifier is not None:
        payload["classifier"] = classifier.model_dump(mode="json")
    if accounts is not None:
        payload["accounts"] = [account.model_dump(mode="json") for account in accounts]
    credentials = _merged_identifiers(payload, identifiers)
    if credentials:
        payload["credentials"] = credentials
    else:
        payload.pop("credentials", None)
    target.write_text(_dump_json(payload), encoding="utf-8")
    return target


def ensure_install_id() -> str:
    """Give this install a name, once, and never change it afterwards (#171).

    THE OTHER HALF OF #171. The keychain refuses to build an account name from
    an empty ``install_id``, because the bare ``<provider>-token`` account is
    not "no namespace" -- it is the namespace every legacy install on the
    machine already occupies, so an uninitialized copy would silently adopt
    whichever live token got there first. That refusal names ``setup`` as the
    way forward, and this function is what makes that sentence true: nothing
    else in the connector ever minted an id, so a self-installed copy could
    only ever be refused.

    WHY IT WRITES LAYER 1 AND NOT THE INSTALLED CONFIG. ``install_id()`` reads
    exactly two sources: ``INBOX_ZERO_INSTALL_ID`` and the ``install_id`` key of
    the committed layer-1 file. It cannot read the installed per-user config,
    because that file's own DIRECTORY is what the id resolves -- reading it
    would be a cycle. So the mint targets layer 1, the same key and the same
    file ``build_custom.py`` stamps at build time for managed zips.

    WHY IT NEVER RE-MINTS. A changed id relocates the config directory, the
    data directory, and the keychain account in one step, so the install loses
    its taxonomy, its learned rules, its ledger, and its stored token and comes
    up looking like a fresh install holding a live mailbox. Every early return
    below is that property: a stamped zip, an operator-set environment
    variable, or a previous run of this function all mean the question is
    already answered.

    THE ID IS RANDOM, NOT DERIVED. A value derived from the hostname, the user,
    or the mailbox would collide with the second install on the same machine --
    the exact collision the namespace exists to prevent -- and would put an
    identifying string into a config file a support request may quote. A
    ``uuid4`` hex prefix collides with nothing and says nothing.

    THE ENVIRONMENT IS CONSULTED THROUGH ``install_id()``, NOT ``os.environ``.
    This module is not the config keystone, and the standards check that
    enforces that (``test_only_the_config_keystone_reads_the_environment``) is
    the reason as well as the receipt: a second reader of
    ``INBOX_ZERO_INSTALL_ID`` could disagree with the resolver about which
    install this is, and mint over an id the operator had already named.

    Returns:
        The install id now in force: the existing one when there was one, the
        newly minted and persisted one otherwise. Never empty.

    Raises:
        ConfigError: The mint could not be written to disk. Raised rather than
            returning an unpersisted value, because an id that exists only in
            this process would namespace the keychain write and then be gone
            before the next run went looking for it.
    """
    reset_install_id_cache()
    existing = config_keystone.install_id()
    if existing:
        return existing

    minted = f"install-{uuid.uuid4().hex[:12]}"
    target = _layer_one_path()
    payload = _existing_payload(target)
    payload["install_id"] = minted
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_dump_json(payload), encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"Could not write the new install id to {target}: {exc}. That file "
            "is what names this install, and without it the OS keychain "
            "account cannot be built -- so authentication would refuse. "
            f"ACTION REQUIRED: make that path writable, or set "
            f"{ENV_PREFIX}INSTALL_ID to name this install explicitly."
        ) from exc
    reset_install_id_cache()
    logger.info(f"CONFIG: minted install id for this install path={target}")
    return minted


def _layer_one_path() -> Path:
    """The committed layer-1 config file: the one place an install id persists.

    ``PROJECT_ROOT`` is read off the KEYSTONE MODULE at call time rather than
    imported as a name or taken from ``config_paths``. Both halves are load
    bearing. ``config_paths`` documents the hazard itself
    (``audited_config_files``): the keystone's copy is the one the suite
    patches to a tmp checkout, so a second reader silently writes THIS
    checkout's real ``config/connector-config.json`` while a test believes it
    is isolated -- which is not a hypothetical here, it is what a first draft of
    this function did, stamping a minted id into the repo's own tracked config.
    Reading at call time rather than binding at import is what makes the patch
    take effect at all.

    Returns:
        The absolute path to ``config/connector-config.json`` under the root
        currently in force.
    """
    return Path(config_keystone.PROJECT_ROOT) / "config" / "connector-config.json"


def _existing_payload(target: Path) -> dict[str, Any]:
    """The installed file's own JSON, as the BASE this write merges over.

    The installer is the only writer of this file, so every key in it was put
    there by a previous installer pass -- and a pass that does not STATE a key
    is not thereby asking for it to be deleted. Rebuilding from a fixed payload
    made ``init-config --force`` erase the ``classifier`` block ``setup`` had
    written, dropping the install to rule-only with nothing said about it.

    Args:
        target: The file about to be rewritten.

    Returns:
        The parsed object, or ``{}`` when the file is absent, unreadable, or
        not a JSON object.
    """
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        # DOCUMENTED FAIL-OPEN, same shape as `_file_posture`. An unreadable or
        # malformed target has nothing to preserve, and the write below replaces
        # it wholesale. DEBUG, not WARNING: the rewrite that follows is the
        # repair.
        logger.opt(exception=True).debug(
            f"CONFIG: merge base unreadable, rewriting whole path={target}"
        )
        return {}
    return raw if isinstance(raw, dict) else {}


def _merged_identifiers(
    payload: dict[str, Any], identifiers: dict[str, str] | None
) -> dict[str, str]:
    """The ``credentials`` block to write: what the file says, overlaid by the caller.

    ``IDENTIFIER_FIELDS`` is enforced in BOTH directions. Inbound, a bad caller
    key raises before anything is written. Outbound, a key the installed file
    already held is DROPPED -- because the only way a non-identifier got in
    there is a human typing a secret VALUE into the file the installer rewrites
    and support requests quote, and a rewrite is exactly the moment to evict it.
    Leaving it was the #148 defect: the secret survived every wizard pass and
    then shipped in the diagnostics bundle.

    Args:
        payload: The installed file's parsed contents, read for its existing
            ``credentials`` block.
        identifiers: The caller's keys, or ``None`` to preserve only.

    Returns:
        The merged block, empty when neither source states anything.

    Raises:
        ConfigError: A key outside ``IDENTIFIER_FIELDS``, refused BEFORE any
            write, so the offending value never reaches the disk at all.
    """
    for key in identifiers or {}:
        if key not in IDENTIFIER_FIELDS:
            raise ConfigError(_not_an_identifier(key))

    existing: dict[str, str] = {}
    block = payload.get("credentials")
    if isinstance(block, dict):
        for key, value in block.items():
            if not isinstance(value, str):
                continue
            if key in IDENTIFIER_FIELDS:
                existing[key] = value
            else:
                # By KEY only. The value is exactly what must not be repeated
                # into a log file that a diagnostics bundle then carries away.
                logger.warning(
                    f"CONFIG: dropping non-identifier key={key} -- {_not_an_identifier(key)}"
                )

    for key, value in (identifiers or {}).items():
        if value:
            existing[key] = value
    return existing


def _not_an_identifier(key: str) -> str:
    """The one refusal sentence, shared by the raise and the drop WARNING.

    Names where the value belongs instead, because "we removed it" without
    "put it here" is how an operator ends up pasting it back.
    """
    return (
        f"{key} is not an identifier and will not be written to "
        f"connector-config.json. Secret VALUES never go in that file -- it is "
        f"rewritten by the installer and quoted in support requests. Put it in "
        f"{secrets_path()} instead, or set "
        f"{ENV_PREFIX}CREDENTIALS__{key.upper()}. Writable identifiers: "
        f"{', '.join(IDENTIFIER_FIELDS)}."
    )


def write_secrets_skeleton() -> tuple[Path, bool]:
    """Create ``secrets_path()`` EMPTY, once, and never touch it again.

    The installer calls this so the human has a file to open with the right
    shape and the right permissions already on it. Every value in the skeleton
    is empty by construction: NO CODE PATH IN THIS CONNECTOR EVER WRITES A REAL
    CREDENTIAL HERE, which is what makes "your secret is in a file we do not
    write" a claim an operator can check by reading this function.

    An existing file is left completely alone -- not merged, not reformatted --
    because the only thing that can be in it is something a human typed, and the
    second run of an idempotent installer must not be the thing that eats it.

    Returns:
        ``(path, created)``. ``created`` is False when the file already existed.
    """
    target = secrets_path()
    if target.exists():
        return target, False

    target.parent.mkdir(parents=True, exist_ok=True)
    skeleton: dict[str, Any] = {
        "credentials": {"google_client_secret": "", "classifier_api_key": None}
    }
    target.write_text(_dump_json(skeleton), encoding="utf-8")
    target.chmod(0o600)
    return target, True
