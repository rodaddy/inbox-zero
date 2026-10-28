"""Adapter construction. One function, one reason to change.

Split out of ``providers/__init__.py`` because that file's job is the package
docstring and the re-exports (``_DOCS/STANDARDS-python.md`` ## Layout: an
``__init__.py`` holds a full module docstring, and ruff's ``RUF022`` family
refuses logic there). The factory is logic, so it lives in a module.
"""

from __future__ import annotations

from loguru import logger

from inbox_zero_connector.providers.base import MailProvider, ProviderError


def get_provider(name: str, drafts_enabled: bool = False) -> MailProvider:
    """Construct the adapter for ``name``.

    ``drafts_enabled`` comes from the installed config and reaches Gmail only,
    where it decides whether the compose scope is requested at all. Graph needs
    no extra permission for drafts, so it ignores the flag at the SCOPE level;
    the tool layer still honours the flag for both providers, so an installer
    can switch drafts off on Microsoft as a capability choice.

    Imports are deferred so that a missing optional SDK for one provider does
    not prevent the other from working.

    Args:
        name: ``"google"`` or ``"microsoft"``.
        drafts_enabled: Whether this deployment creates reply drafts.

    Returns:
        The adapter.

    Raises:
        ProviderError: On an unknown provider name. Never a default, because a
            typo silently falling back to one provider would mean a connector
            reading the wrong mailbox.
    """
    logger.debug(f"PROVIDER: constructing provider={name} drafts_enabled={drafts_enabled}")
    if name == "google":
        from inbox_zero_connector.providers.gmail.provider import GmailProvider

        return GmailProvider(drafts_enabled=drafts_enabled)
    if name == "microsoft":
        from inbox_zero_connector.providers.outlook.provider import OutlookProvider

        return OutlookProvider()
    raise ProviderError(f"Unknown provider {name!r}. Valid providers: google, microsoft.")
