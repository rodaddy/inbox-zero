"""Silent auth signs in as the install's OWN account, never ``accounts[0]`` (#149).

The msal token cache is a LIST, and its order is msal's, not ours. A machine
where two Microsoft accounts have signed in through the same registration --
a personal account and a work account, or two tenants -- hands
``get_accounts()`` both entries, and the old code took element zero. The
install then read, filed, and drafted in whichever mailbox msal happened to
list first, with no error and a label that changed underneath the operator.

The pin is the UPN this install stored on its own first successful sign-in.
It is written into the keychain payload beside the serialized cache, so it
survives exactly as long as the credential does and is scoped per install_id
like every other credential fact.
"""

from __future__ import annotations

from typing import Any

import pytest

from inbox_zero_connector.providers.base import AuthError
from inbox_zero_connector.providers.outlook import reauth as outlook_reauth
from inbox_zero_connector.providers.outlook.provider import OutlookProvider

FIRST_UPN = "someone.else@other-tenant.onmicrosoft.com"
BOUND_UPN = "owner@example-tenant.onmicrosoft.com"


class _FakeCache:
    has_state_changed = False

    def deserialize(self, blob: str) -> None:
        return None

    def serialize(self) -> str:
        return "{}"


class _TwoAccountApp:
    """msal stand-in whose cache holds two accounts, the bound one SECOND."""

    def __init__(self) -> None:
        self.authenticated_as: dict[str, Any] | None = None

    def get_accounts(self) -> list[dict[str, Any]]:
        return [
            {"username": FIRST_UPN, "home_account_id": "a.1"},
            {"username": BOUND_UPN, "home_account_id": "b.2"},
        ]

    def acquire_token_silent(self, scopes: Any, account: Any) -> dict[str, Any]:
        self.authenticated_as = account
        return {"access_token": "t", "scope": "Mail.ReadWrite"}


class _OtherAccountOnlyApp(_TwoAccountApp):
    """The cache holds accounts, but NOT the one this install is pinned to."""

    def get_accounts(self) -> list[dict[str, Any]]:
        return [{"username": FIRST_UPN, "home_account_id": "a.1"}]


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> _TwoAccountApp:
    return _install(monkeypatch, _TwoAccountApp())


def _install(monkeypatch: pytest.MonkeyPatch, fake: _TwoAccountApp) -> _TwoAccountApp:
    import msal

    monkeypatch.setattr(msal, "PublicClientApplication", lambda *a, **k: fake)
    monkeypatch.setattr(msal, "SerializableTokenCache", _FakeCache)
    monkeypatch.setattr(outlook_reauth, "store_token", lambda provider, payload: None)
    monkeypatch.setattr(
        outlook_reauth, "microsoft_client_config", lambda: ("client-id", "https://login.example")
    )
    return fake


def test_two_account_cache_selects_the_installs_own_account(
    monkeypatch: pytest.MonkeyPatch, app: _TwoAccountApp
) -> None:
    """The pinned UPN wins over cache order, even when it is listed last."""
    monkeypatch.setattr(
        outlook_reauth, "load_token", lambda provider: {"cache": "{}", "account": BOUND_UPN}
    )

    assert OutlookProvider(token=None).authenticate(interactive=False) == BOUND_UPN
    assert app.authenticated_as is not None
    assert app.authenticated_as["username"] == BOUND_UPN


def test_pinned_account_absent_from_the_cache_refuses_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache that no longer holds the pinned account fails loud, not silently."""
    _install(monkeypatch, _OtherAccountOnlyApp())
    monkeypatch.setattr(
        outlook_reauth, "load_token", lambda provider: {"cache": "{}", "account": BOUND_UPN}
    )

    with pytest.raises(AuthError) as caught:
        OutlookProvider(token=None).authenticate(interactive=False)

    message = str(caught.value)
    assert BOUND_UPN in message
    assert FIRST_UPN in message


def test_unpinned_install_still_uses_the_single_cached_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stored pin (pre-#149 credential) keeps working off a one-account cache."""
    fake = _install(monkeypatch, _OtherAccountOnlyApp())
    monkeypatch.setattr(outlook_reauth, "load_token", lambda provider: {"cache": "{}"})

    assert OutlookProvider(token=None).authenticate(interactive=False) == FIRST_UPN
    assert fake.authenticated_as is not None
    assert fake.authenticated_as["username"] == FIRST_UPN


def test_unpinned_install_with_an_ambiguous_cache_refuses(
    monkeypatch: pytest.MonkeyPatch, app: _TwoAccountApp
) -> None:
    """Two accounts and no pin is a guess, so it refuses instead of guessing."""
    monkeypatch.setattr(outlook_reauth, "load_token", lambda provider: {"cache": "{}"})

    with pytest.raises(AuthError) as caught:
        OutlookProvider(token=None).authenticate(interactive=False)

    assert app.authenticated_as is None
    assert FIRST_UPN in str(caught.value)
    assert BOUND_UPN in str(caught.value)


def test_first_sign_in_stores_the_account_beside_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin is written on the sign-in that establishes it, or nothing pins later."""
    fake = _install(monkeypatch, _OtherAccountOnlyApp())
    monkeypatch.setattr(outlook_reauth, "load_token", lambda provider: None)
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(
        outlook_reauth,
        "store_token",
        lambda provider, payload, account=None: written.append(payload),
    )

    class _Changed(_FakeCache):
        has_state_changed = True

    import msal

    monkeypatch.setattr(msal, "SerializableTokenCache", _Changed)

    fake.get_accounts = lambda: []  # type: ignore[method-assign]
    monkeypatch.setattr(
        OutlookProvider,
        "_interactive_token",
        lambda self, app, interactive: {
            "access_token": "t",
            "scope": "Mail.ReadWrite",
            "id_token_claims": {"preferred_username": BOUND_UPN},
        },
    )

    assert OutlookProvider(token=None).authenticate(interactive=True) == BOUND_UPN
    assert written == [{"cache": "{}", "account": BOUND_UPN}]


# ---------------------------------------------------------------------------
# #190 -- the refusal has to name a recovery that WORKS. Both messages used to
# prescribe 'auth microsoft', which consults the same ambiguous cache before it
# reaches the interactive branch and re-raises the identical AuthError. An
# operator following the instruction loops forever.
# ---------------------------------------------------------------------------


LOGOUT_COMMAND = "logout microsoft"
"""The step that clears the cache. Neither message named it before #190."""


def _refusal_for(accounts: list[dict[str, Any]], pin: object) -> str:
    """The AuthError text ``pinned_account`` raises for this cache and pin.

    Called directly rather than through ``OutlookProvider.authenticate`` so the
    assertion is about the MESSAGE and nothing else -- the provider path adds a
    msal stand-in, a keychain stub, and a client-config stub between the test
    and the string, none of which the prose depends on.
    """
    with pytest.raises(AuthError) as caught:
        outlook_reauth.pinned_account(accounts, pin)
    return str(caught.value)


def test_the_pin_mismatch_refusal_prescribes_logout_before_auth() -> None:
    """RED (#190): the message says 'auth microsoft' and nothing about logout.

    ``provider.py`` restores the stored credential BEFORE it reaches the
    interactive branch, so the prescribed command runs ``pinned_account``
    against the same cache and raises this same error. The escape is to clear
    the credential first, and the message is the only place the operator can
    learn that.

    ORDER IS ASSERTED, not just presence. "run auth, or logout" would contain
    both words and still send the operator into the loop, so the logout has to
    come first in the prose the way it comes first in the sequence.

    RED-FIRST, observed before the fix::

        AssertionError: the refusal must name 'logout microsoft' as the first
        recovery step
    """
    message = _refusal_for([{"username": FIRST_UPN}], BOUND_UPN)

    assert LOGOUT_COMMAND in message, (
        f"the refusal must name {LOGOUT_COMMAND!r} as the first recovery step; got {message!r}"
    )
    assert message.index(LOGOUT_COMMAND) < message.index("auth microsoft"), (
        "logout comes before auth in the prose because it comes before it in the sequence"
    )


def test_the_ambiguous_cache_refusal_prescribes_logout_before_auth() -> None:
    """RED (#190): the second message had the same defect and the same fix.

    Two accounts and no pin is the OTHER way into the loop, and it is the more
    likely one -- it is what every pre-#149 credential upgrades into once a
    second Microsoft account signs in on the machine.
    """
    message = _refusal_for([{"username": FIRST_UPN}, {"username": BOUND_UPN}], None)

    assert LOGOUT_COMMAND in message, (
        f"the refusal must name {LOGOUT_COMMAND!r} as the first recovery step; got {message!r}"
    )
    assert message.index(LOGOUT_COMMAND) < message.index("auth microsoft"), (
        "logout comes before auth in the prose because it comes before it in the sequence"
    )


def test_both_refusals_still_offer_the_install_id_escape() -> None:
    """The OTHER working escape, kept. Naming logout must not delete it.

    A fresh ``INBOX_ZERO_INSTALL_ID`` namespaces a new keychain key, which is
    the right move for an operator who wants BOTH mailboxes watched rather than
    one replaced. The logout sequence throws the old credential away; this one
    does not, so the two are not substitutes and both belong in the prose.
    """
    mismatch = _refusal_for([{"username": FIRST_UPN}], BOUND_UPN)
    ambiguous = _refusal_for([{"username": FIRST_UPN}, {"username": BOUND_UPN}], None)

    assert "INBOX_ZERO_INSTALL_ID" in mismatch
    assert "INBOX_ZERO_INSTALL_ID" in ambiguous


@pytest.mark.parametrize("pin", ["", "   ", "\t\n", "  \n "])
def test_a_whitespace_only_pin_is_treated_as_no_pin(pin: str) -> None:
    """RED (#190): a blank-but-truthy pin refused a cache with ONE account.

    ``"   "`` is a string and it is truthy, so the pin branch took it, compared
    it case-folded against the one cached username, matched nothing, and raised
    -- refusing a credential that is unambiguous and works. A pin that names no
    mailbox is not a pin, and the single-account fallback below is what an
    unpinned install has always been entitled to.

    The empty string is included for the same reason it is harmless: it proves
    the ``strip()`` guard subsumes the truthiness check rather than sitting
    beside it.

    RED-FIRST, observed before the fix::

        inbox_zero_connector.providers.base.AuthError: The stored Microsoft
        credential for this install names    , but the token cache holds ...
    """
    account = {"username": FIRST_UPN, "home_account_id": "a.1"}

    assert outlook_reauth.pinned_account([account], pin) is account


def test_a_whitespace_only_pin_still_refuses_an_ambiguous_cache() -> None:
    """The guard downgrades a blank pin to UNPINNED, never to a guess.

    Treating whitespace as absent is only correct if absent still means the
    two-account cache refuses. Otherwise the fix would trade a false refusal
    for the silent wrong-mailbox pick that #149 exists to prevent.
    """
    accounts = [{"username": FIRST_UPN}, {"username": BOUND_UPN}]

    with pytest.raises(AuthError) as caught:
        outlook_reauth.pinned_account(accounts, "   ")

    assert "2 accounts" in str(caught.value)


def test_a_pin_with_surrounding_whitespace_still_matches() -> None:
    """A pin that names a real mailbox with stray padding is still that mailbox.

    The strip is applied to the COMPARISON, not only to the emptiness test. A
    credential written with a trailing newline -- which is what a value read
    from a file or pasted from a console carries -- names an account the cache
    holds, and refusing it would be the same false refusal in a different coat.
    """
    account = {"username": BOUND_UPN, "home_account_id": "b.2"}

    assert outlook_reauth.pinned_account([account], f"  {BOUND_UPN}\n") is account
