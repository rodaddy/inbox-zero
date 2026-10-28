"""Domain services - the connector's business logic, one package per concern.

Everything under here is the WHAT of the product (watching, triaging,
drafting, auditing, authenticating, resetting); the HOW of talking to a
mailbox lives in ``providers`` and the runnable surfaces live in ``apps``.
A service may import providers and utils; nothing here imports apps.

Key Components:
    - watch: the unattended engine - one polling round, the loop, the
      cursor, and the mailbox-scope answer to "what may we read".
    - triage: the decision layer - classification, the folder taxonomy,
      learned rules, and the correction handler that feeds them.
    - voice: the Enhancements lane - voice profiles and reply drafting.
    - audit: the append-only ledger every action writes a receipt to.
    - auth: credentials (OS keychain only), OAuth scopes, live grants.
    - reset: the developer/test mailbox-reset fixture, both providers.

Pattern/Convention:
    One package per domain concern; helpers used by 2+ siblings inside a
    package promote to that package's own module, and domain-neutral
    mechanisms promote further up to ``utils`` (the promotion ladder -
    duplication triggers promotion, never a second copy).

Example:
    >>> from inbox_zero_connector.services.triage.taxonomy import validate_label_name
    >>> validate_label_name("Reference")
    'Reference'

See Also:
    - inbox_zero_connector.providers: mailbox transport these services drive.
    - inbox_zero_connector.apps: the CLI and MCP server that call in here.
"""

from __future__ import annotations
