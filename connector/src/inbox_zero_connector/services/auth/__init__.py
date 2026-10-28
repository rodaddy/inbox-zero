"""Identity and permission - who we are, what we asked for, what we hold.

Three separable questions, one module each: where credentials LIVE
(keychain - the OS keychain only, never a file), what the app REQUESTS
(scopes - the OAuth manifest per provider and posture), and what the
account actually GRANTED (grants - discovered from the live credential,
because requested and granted are not the same thing).

Key Components:
    - keychain: token storage in the OS keychain via ``keyring``; nothing
      token-like is written to disk or logged, ever.
    - scopes: OAuth scope constants and the enforcement-layer answer
      (scope-layer vs code-layer no-send) per provider and posture.
    - grants: what the mailbox actually granted, read from the credential.

Pattern/Convention:
    The scope set is constructed ONLY in ``scopes`` and consumed by
    providers; a capability change starts there, not in an adapter. The
    keychain module is the one place tokens are touched.

    The keychain ACCOUNT NAME is built in exactly one function, and it
    refuses to produce an un-namespaced one. The OS keychain is global to
    the machine -- the ``INBOX_ZERO_CONFIG_DIR`` / ``INBOX_ZERO_DATA_DIR``
    variables move files and reach it not at all -- so an uninitialized
    install with no ``install_id`` would otherwise key the bare
    ``<provider>-token`` and adopt whatever live credential a legacy
    install left there (#171). It raises ``config.ConfigError`` instead,
    before any keychain call, and the CLI renders that as one line.

Example:
    >>> from inbox_zero_connector.services.auth.scopes import scopes_for
    >>> "gmail.readonly" in " ".join(scopes_for("google", drafts_enabled=False))
    True

See Also:
    - inbox_zero_connector.providers: consumers of every credential here.
    - config/README.md and secrets/README.md: the per-key config split;
      user tokens belong to neither folder.
"""

from __future__ import annotations
