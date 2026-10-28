"""The Microsoft defaults the kit SHIPS: authority ``common``, rTech's client id.

Two claims, and both are product decisions rather than incidental values:

* The default authority is ``https://login.microsoftonline.com/common``, not
  ``/organizations``. ``organizations`` refuses a personal Microsoft account
  outright, and rTech's registration is audience
  ``AzureADandPersonalMicrosoftAccount`` -- so ``organizations`` narrowed the
  app below what its own registration allows.
* ``config/connector-config.json`` ships rTech's multi-tenant PUBLIC client id.
  A public-client application id is an identifier, not a secret: it carries no
  secret, and the user's token is minted to the user's own keychain, so rTech
  is still not in the token chain. A client who wants their own registration
  overrides ``credentials.microsoft_client_id``.

These assert the SHIPPED file deliberately, so ``_isolate_repo_layers`` is
opted out of by pointing ``PROJECT_ROOT`` back at the real checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inbox_zero_connector.config import (
    CredentialSettings,
    load_settings,
    reset_settings,
)
from inbox_zero_connector.services.auth.keychain import microsoft_client_config

RTECH_CLIENT_ID = "4be80172-cace-49cd-bbee-cef34841c8fb"
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_tenant_default_is_common() -> None:
    """``common``, so a personal Microsoft account can sign in at all.

    Constructed directly, so this is the CODE default with no file or env layer
    in play -- the value ``keychain.microsoft_client_config`` falls back to.
    """
    assert CredentialSettings().microsoft_tenant_id == "common"


def test_default_authority_is_common(monkeypatch: pytest.MonkeyPatch) -> None:
    """The authority the MSAL app is built with, end to end from the default.

    The tenant variable is cleared rather than assumed absent: env outranks
    every file layer, so a developer shell that exported one for a live sign-in
    would otherwise silently answer this test with their own tenant.
    """
    monkeypatch.setenv("INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID", "app-under-test")
    monkeypatch.delenv("INBOX_ZERO_CREDENTIALS__MICROSOFT_TENANT_ID", raising=False)
    reset_settings()
    _client_id, authority = microsoft_client_config()
    assert authority == "https://login.microsoftonline.com/common"


def test_shipped_config_carries_the_rtech_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_settings`` over the real committed files yields rTech's app id."""
    monkeypatch.setattr("inbox_zero_connector.config.PROJECT_ROOT", REPO_ROOT)
    monkeypatch.delenv("INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID", raising=False)
    monkeypatch.delenv("INBOX_ZERO_CREDENTIALS__MICROSOFT_TENANT_ID", raising=False)
    reset_settings()
    creds = load_settings().credentials
    assert creds.microsoft_client_id == RTECH_CLIENT_ID
    # Tenant is left unset in the shipped file so the ``common`` default applies.
    assert creds.microsoft_tenant_id == "common"
