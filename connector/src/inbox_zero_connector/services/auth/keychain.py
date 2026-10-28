"""Credential storage -- OS keychain only.

Nothing token-like is ever written to a file in this package, and nothing
token-like is ever logged. Tokens go into the OS keychain through ``keyring``
(macOS Keychain, Windows Credential Locker, Secret Service on Linux) and come
back out only into memory.

Consequences that are load-bearing, not incidental:

- No token file on disk means a stolen laptop backup, a synced config folder,
  or a support bundle cannot carry the mailbox credential out with it.
- The OAuth CLIENT configuration (client id / client secret) is NOT a user
  credential; it identifies the app registration the buyer created in their
  OWN tenant. It comes from ``config.CredentialSettings``, which is the one
  module permitted to read the environment, so it never lands in this repo and
  the client secret is never persisted here either.
- Errors from this module never interpolate a token value into the message,
  and no log line here carries one either: every field name involved
  (``token``, ``client_secret``, ``credential``) matches the redaction table in
  ``logging_config``, so even a future careless call site is caught.

THE KEYCHAIN IS GLOBAL; THE FILESYSTEM IS NOT (#171)
    ``INBOX_ZERO_CONFIG_DIR`` and ``INBOX_ZERO_DATA_DIR`` relocate this
    connector's FILES, and it is easy to read them as a sandbox for a whole
    run. They are not. The OS keychain has no per-directory scope: it is one
    store per user account on the machine, and this module reaches it through
    ``SERVICE_NAME`` plus an account name that no environment variable
    redirects. Pinning both directories at a scratch path therefore isolates
    the config, the state, and the ledger while leaving the CREDENTIAL shared
    with every other install on that machine.

    The 0.2.9 release smoke proved what that gap costs. A fresh unzip with both
    directories pinned to scratch had no ``install_id``, so ``_key`` fell back
    to the un-namespaced account ``google-token`` -- which is exactly what a
    legacy install on the same Mac had already stored a live token under. The
    run authenticated, listed 26 containers, filed a real message out of a real
    INBOX, and wrote a refreshed token back to the borrowed account. Nothing
    failed; the filesystem sandbox held perfectly and was simply the wrong
    boundary.

    The boundary that does hold is the account NAME. ``_key`` refuses to
    produce an un-namespaced one, so an uninitialized install has no account to
    read and no account to write, and the credential call never happens. That
    is why the refusal lives in the key constructor rather than in a caller: a
    check at a call site protects that call site, while a constructor that
    cannot return a degraded value protects every path that needs a key,
    including ones not written yet.

ONE ENTRY PER MAILBOX, NOT PER PROVIDER (#53)
    The account name carries the PROVIDER and the MAILBOX ADDRESS, because an
    install set up for two Gmail mailboxes has two credentials and a name keyed
    by provider alone can only hold one. Before this, the second ``auth``
    overwrote the first mailbox's token under a shared name and the first
    mailbox began filing into the second one's inbox -- valid credential, wrong
    mailbox, nothing failing loudly. That is #171 one level in: there the
    collision was between two INSTALLS on one machine, here between two
    ACCOUNTS inside one install.

UPGRADE PATH FOR AN EXISTING SINGLE-ACCOUNT ENTRY
    An install that authenticated before this change has its token under
    ``<provider>-token@<install_id>``, with no address in the name. That name
    is PRESERVED, not migrated: it is the name the install's DEFAULT account --
    the first entry in ``settings().accounts``, which ``accounts.py`` documents
    as load-bearing order -- resolves to, so the ordinary single-account
    install keeps reading and writing its existing entry and never re-auths.

    A SECOND account on that same provider gets an address-qualified name of
    its own and must complete ``auth`` once; the connector cannot mint a
    credential for a mailbox that has never consented, so there is nothing to
    migrate for it and a re-auth is the only honest answer. Nothing is copied,
    renamed, or deleted in the keychain by this module, and no entry changes
    name behind the operator's back: a silent migration would move a live
    credential out from under any other process still reading the old name,
    which is the failure mode #171 was about. When the default account cannot
    be resolved at all, ``_key`` falls back to the legacy un-addressed name for
    that install rather than inventing one -- the same refusal-over-guess rule
    the install id follows.

Vendor-agnostic doctrine (2026-08-02): the buyer's tenant, the buyer's app
registration, the buyer's keychain. rTech hosts nothing and holds nothing.
"""

from __future__ import annotations

import json
from typing import Any

import keyring
from keyring.errors import KeyringError
from loguru import logger

from inbox_zero_connector.config import ENV_PREFIX, ConfigError, install_id, settings

SERVICE_NAME = "inbox-zero-connector"

GOOGLE_CLIENT_ID_ENV = f"{ENV_PREFIX}CREDENTIALS__GOOGLE_CLIENT_ID"
GOOGLE_CLIENT_SECRET_ENV = f"{ENV_PREFIX}CREDENTIALS__GOOGLE_CLIENT_SECRET"
MICROSOFT_CLIENT_ID_ENV = f"{ENV_PREFIX}CREDENTIALS__MICROSOFT_CLIENT_ID"
MICROSOFT_TENANT_ENV = f"{ENV_PREFIX}CREDENTIALS__MICROSOFT_TENANT_ID"
"""Variable NAMES, for error messages and docs. Values are read by ``config``.

A name is not a secret (``_DOCS/STANDARDS-python.md`` ## LAW: secrets are
fetched, and the NAME is committed config), so naming them here is what lets an
error message tell the operator exactly what to set.
"""


class CredentialError(Exception):
    """A credential could not be stored or retrieved.

    Never carries a token value in its message.
    """


def default_account(provider: str) -> str:
    """The address of this install's default mailbox for ``provider``, or ``""``.

    The FIRST configured entry naming that provider, because ``accounts.py``
    documents list order as load-bearing: the first entry is the install's
    default account, and a helper that sorted or searched by any other rule
    would change which mailbox an existing single-account install serves.

    Returns ``""`` rather than raising when there is no accounts list or no
    entry for the provider. That is the pre-multi-account install, and it is
    exactly the case whose token sits under the legacy un-addressed name, so
    the empty answer is what routes ``_key`` to that name (see the module
    docstring's upgrade path).

    Args:
        provider: The provider key -- ``google`` or ``microsoft``.

    Returns:
        The mailbox address, or ``""`` when this install has not named one.
    """
    try:
        accounts = settings().accounts
    except ConfigError:
        logger.trace("no settings; mailbox address unknown")
        return ""
    for account in accounts:
        if account.provider == provider:
            return account.address
    return ""


def _key(provider: str, account: str | None = None) -> str:
    """The keychain account name for one provider's token.

    Every read, write, and delete in this module goes through here, so a stored
    token and a retrieved token cannot drift apart -- the store/fetch key pair
    is the classic place that silently breaks. Namespacing lives in
    ``SERVICE_NAME``; this is only the per-provider account within it.

    The install id is REQUIRED and is appended as ``<provider>-token@<id>``.
    Two installs on one machine share one OS keychain and therefore one
    ``SERVICE_NAME``, so without the suffix the second mailbox's ``auth`` would
    overwrite the first mailbox's token under the same account and the first
    would start filing into the wrong inbox. The id comes from
    ``config.install_id()`` -- the SAME resolver the config and data directories
    use, imported rather than recomputed, so a token can never be stored under
    one namespace and looked for under another.

    An absent id used to fall back to the bare ``<provider>-token``, and that
    fallback is the #171 incident: the bare name is not "no namespace", it is
    the namespace every legacy install already occupies, so an uninitialized
    connector silently ADOPTED whichever live credential happened to be sitting
    there. Refusing is the only answer that is safe on a machine that has ever
    authenticated -- and on a machine that has not, there is no token to find
    either way, so nothing correct is lost by refusing.

    The MAILBOX ADDRESS is appended as ``<provider>-token@<id>#<address>`` when
    one is known, because an install set up for two mailboxes on one provider
    holds two credentials and a provider-only name can only hold one (#53). An
    omitted ``account`` resolves to this install's default mailbox for the
    provider, and when that resolves to nothing the legacy un-addressed name is
    produced unchanged -- which is what an existing single-account install's
    token is already stored under. See the module docstring's upgrade path: no
    entry is ever renamed or copied here.

    Args:
        provider: The provider key -- ``google`` or ``microsoft``.
        account: The mailbox address this token belongs to. ``None`` asks for
            the install's default account for ``provider``.

    Returns:
        The namespaced account name within ``SERVICE_NAME``.

    Raises:
        ConfigError: This install has no ``install_id``. Raised BEFORE any
            keychain call, so an uninitialized install provably never reads or
            writes the machine's credential store. The message names the setup
            command and carries nothing token-like.
    """
    resolved = install_id()
    if not resolved:
        raise ConfigError(
            "This install has no install_id, so its credential account name "
            "cannot be built. An un-namespaced name would collide with any "
            "other install on this machine, which is a mailbox-level mix-up, "
            "so it is refused rather than guessed. Note that "
            f"{ENV_PREFIX}CONFIG_DIR and {ENV_PREFIX}DATA_DIR relocate files "
            "only -- the OS keychain is shared machine-wide and they do not "
            "isolate it. ACTION REQUIRED: run "
            "'uv run inbox-zero-connector setup' to initialize this install, "
            f"or set {ENV_PREFIX}INSTALL_ID to name it explicitly."
        )
    address = default_account(provider) if account is None else account
    if not address:
        return f"{provider}-token@{resolved}"
    return f"{provider}-token@{resolved}#{address}"


def store_token(provider: str, payload: dict[str, Any], account: str | None = None) -> None:
    """Persist a token payload for ``provider`` in the OS keychain.

    The log line records the provider and the fact of the write, never the
    payload -- ``payload`` is not passed to any logging call in this module.

    Args:
        provider: The provider key -- ``google`` or ``microsoft``.
        payload: The token material to serialize. Never logged.
        account: The mailbox this token belongs to. ``None`` asks for the
            install's default account (see ``_key``).

    Raises:
        ConfigError: This install is not initialized (see ``_key``). The
            keychain is not touched, so a refreshed token cannot be written
            back into another install's account.
        CredentialError: The OS keychain refused the write.
    """
    try:
        keyring.set_password(SERVICE_NAME, _key(provider, account), json.dumps(payload))
    except KeyringError as exc:
        logger.opt(exception=True).error(
            f"AUTH: keychain write failed provider={provider} error_type={type(exc).__name__}"
        )
        raise CredentialError(
            f"Could not write the {provider} credential to the OS keychain: {exc}. "
            "This connector does not fall back to file storage by design. "
            "ACTION REQUIRED: unlock the OS keychain and re-run "
            f"'uv run inbox-zero-connector auth {provider}'."
        ) from exc
    logger.info(f"AUTH: credential stored provider={provider} store=os-keychain")


def load_token(provider: str, account: str | None = None) -> dict[str, Any] | None:
    """Return the stored token payload for ``provider``, or None.

    ``None`` means "this install has no credential". It does NOT cover the
    uninitialized case: that raises instead, because a miss and a refusal are
    different facts and only the refusal must stop the run (#171).

    Args:
        provider: The provider key -- ``google`` or ``microsoft``.
        account: The mailbox whose token to read. ``None`` asks for the
            install's default account (see ``_key``).

    Returns:
        The stored payload, or ``None`` when nothing is stored.

    Raises:
        ConfigError: This install is not initialized (see ``_key``). The
            keychain is not read, so no other install's live token can be
            adopted.
        CredentialError: The keychain refused the read, or the stored value is
            unparseable.
    """
    try:
        raw = keyring.get_password(SERVICE_NAME, _key(provider, account))
    except KeyringError as exc:
        logger.opt(exception=True).error(
            f"AUTH: keychain read failed provider={provider} error_type={type(exc).__name__}"
        )
        raise CredentialError(
            f"Could not read the {provider} credential from the OS keychain: {exc}. "
            "ACTION REQUIRED: unlock the OS keychain, then retry."
        ) from exc
    if not raw:
        logger.debug(f"AUTH: no stored credential provider={provider}")
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.opt(exception=True).error(f"AUTH: stored credential unparseable provider={provider}")
        raise CredentialError(
            f"The stored {provider} credential is unreadable. "
            f"ACTION REQUIRED: re-run 'uv run inbox-zero-connector auth {provider}'."
        ) from exc
    return parsed if isinstance(parsed, dict) else None


def delete_token(provider: str, account: str | None = None) -> bool:
    """Remove the stored credential. Returns False if there was none.

    Args:
        provider: The provider key -- ``google`` or ``microsoft``.
        account: The mailbox whose token to remove. ``None`` asks for the
            install's default account (see ``_key``), so logging one mailbox
            out cannot log another one out.

    Returns:
        ``True`` when a credential was removed, ``False`` when there was none.

    Raises:
        ConfigError: This install is not initialized (see ``_key``). Deleting
            an account this install does not own is the #171 defect in reverse,
            so the keychain is not touched.
        CredentialError: The keychain refused the delete.
    """
    try:
        if keyring.get_password(SERVICE_NAME, _key(provider, account)) is None:
            logger.info(f"AUTH: no credential to remove provider={provider}")
            return False
        keyring.delete_password(SERVICE_NAME, _key(provider, account))
    except KeyringError as exc:
        logger.opt(exception=True).error(
            f"AUTH: keychain delete failed provider={provider} error_type={type(exc).__name__}"
        )
        raise CredentialError(
            f"Could not delete the {provider} credential: {exc}. "
            "ACTION REQUIRED: unlock the OS keychain, then retry."
        ) from exc
    logger.info(f"AUTH: credential removed provider={provider}")
    return True


def google_client_config() -> dict[str, Any]:
    """Build the installed-app OAuth client config for the buyer's own project.

    The buyer creates this app registration in their own Google Cloud project.
    The identifiers come from ``config.CredentialSettings`` -- the keystone is
    what reads the environment -- and are never cached to disk here.
    """
    client_id, client_secret = settings().credentials.require_google()
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def microsoft_client_config() -> tuple[str, str]:
    """Return ``(client_id, authority)`` for the Entra app registration in use.

    By default that is rTech's multi-tenant PUBLIC client, shipped in
    ``config/connector-config.json``, on the ``common`` authority -- which
    admits work/school and personal Microsoft accounts alike. A public client
    carries no secret and the resulting token is written to this machine's own
    keychain, so shipping the id does not put rTech in the token chain. A buyer
    who registers in their own tenant overrides both identifiers in config.
    """
    client_id, tenant = settings().credentials.require_microsoft()
    return client_id, f"https://login.microsoftonline.com/{tenant}"
