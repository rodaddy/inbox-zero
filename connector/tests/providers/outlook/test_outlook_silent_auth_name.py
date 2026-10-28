"""Silent ``auth microsoft`` names the bound mailbox, not ``unknown`` (#114).

``acquire_token_silent`` hands back a token response that carries NO
``id_token_claims`` (observed live 2026-08-22 on the trial tenant), so the
claims-only label read fell through to ``"unknown"`` on every re-run after
the first consent. The msal account cache already knows the bound UPN
(``get_accounts()[0]["username"]``); the silent path must fall back to it.
"""

from __future__ import annotations

from typing import Any

import pytest

from inbox_zero_connector.providers.outlook import reauth as outlook_reauth
from inbox_zero_connector.providers.outlook.provider import OutlookProvider
from inbox_zero_connector.providers.outlook.reauth import account_name

BOUND_UPN = "owner@contoso.onmicrosoft.com"


class _FakeCache:
    has_state_changed = False

    def deserialize(self, blob: str) -> None:
        return None

    def serialize(self) -> str:
        return "{}"


class _SilentOnlyApp:
    """msal stand-in: one cached account, a silent result with no id claims."""

    def get_accounts(self) -> list[dict[str, Any]]:
        return [{"username": BOUND_UPN, "home_account_id": "h.t"}]

    def acquire_token_silent(self, scopes: Any, account: Any) -> dict[str, Any]:
        return {"access_token": "t", "scope": "Mail.ReadWrite"}


def test_silent_path_reports_the_cached_account_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import msal

    monkeypatch.setattr(msal, "PublicClientApplication", lambda *a, **k: _SilentOnlyApp())
    monkeypatch.setattr(msal, "SerializableTokenCache", _FakeCache)
    monkeypatch.setattr(outlook_reauth, "load_token", lambda provider: None)
    monkeypatch.setattr(outlook_reauth, "store_token", lambda provider, payload: None)
    monkeypatch.setattr(
        outlook_reauth, "microsoft_client_config", lambda: ("client-id", "https://login.example")
    )
    assert OutlookProvider(token=None).authenticate(interactive=False) == BOUND_UPN


def test_claims_still_win_over_the_cached_account() -> None:
    result = {"id_token_claims": {"preferred_username": "claims@example.com"}}
    assert account_name(result, account={"username": BOUND_UPN}) == "claims@example.com"


def test_no_claims_and_no_account_is_still_unknown() -> None:
    assert account_name({}, account=None) == "unknown"
