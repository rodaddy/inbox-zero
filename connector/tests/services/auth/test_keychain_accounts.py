"""Two mailboxes on one provider hold two distinct keychain entries (#53).

THE FAILURE THIS PREVENTS
    ``_key`` used to name the stored token by PROVIDER alone. An install set
    up for two Gmail mailboxes therefore resolved one account name for both,
    so the second ``auth`` overwrote the first mailbox's token and the first
    mailbox started filing into the second one's inbox. Nothing failed loudly:
    the credential was valid, it was simply the wrong mailbox's. That is the
    #171 incident again one level in -- there the collision was between two
    INSTALLS, here it is between two ACCOUNTS inside one install.

WHY THE ASSERTIONS COMPARE STORED VALUES, NOT KEY STRINGS
    A test that only asserted ``_key(...) != _key(...)`` would pass against a
    key format that distinguishes the two names while some caller still passes
    neither. So the round-trip tests store a different payload per account and
    read each one back: the property under test is that a write for one
    mailbox cannot be observed by the other, which is the defect as the owner
    of the second mailbox experienced it.

WHY ``account=None`` MUST KEEP THE OLD NAME
    Every single-account install on disk today has a token under
    ``<provider>-token@<install>``. Defaulting to the install's first
    configured account and emitting exactly that name is what makes this change
    survivable without a forced re-auth for the ordinary install; the migration
    notice in the module docstring covers the case where it cannot.
"""

from __future__ import annotations

from typing import Any

import pytest

from inbox_zero_connector.config_paths import INSTALL_ID_ENV, reset_install_id_cache
from inbox_zero_connector.services.auth import keychain

FIRST = "first@example.com"
SECOND = "second@example.com"


@pytest.fixture(autouse=True)
def _fresh_install_id_cache() -> Any:
    """The layer-1 read is cached per process, so every test starts from cold."""
    reset_install_id_cache()
    yield
    reset_install_id_cache()


class _MemoryKeyring:
    """An in-memory keychain keyed exactly as the OS one is: service + account."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[service, account] = value

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        self.store.pop((service, account), None)


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> _MemoryKeyring:
    """Swap ``keyring`` inside the keychain module for an in-memory store."""
    fake = _MemoryKeyring()
    monkeypatch.setattr(keychain, "keyring", fake)
    return fake


@pytest.fixture
def initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """An install id exists, so the #171 refusal is not what is under test."""
    monkeypatch.setenv(INSTALL_ID_ENV, "sample-install")


def test_two_accounts_on_one_provider_get_distinct_keys(initialized: None) -> None:
    """The headline property: same provider, different mailbox, different name."""
    assert keychain._key("google", FIRST) != keychain._key("google", SECOND)


def test_the_second_account_does_not_overwrite_the_first(
    memory_keyring: _MemoryKeyring, initialized: None
) -> None:
    """The defect as the owner of the first mailbox experienced it."""
    keychain.store_token("google", {"token": "first"}, account=FIRST)
    keychain.store_token("google", {"token": "second"}, account=SECOND)
    assert keychain.load_token("google", account=FIRST) == {"token": "first"}
    assert keychain.load_token("google", account=SECOND) == {"token": "second"}


def test_deleting_one_account_leaves_the_other(
    memory_keyring: _MemoryKeyring, initialized: None
) -> None:
    """Logging one mailbox out must not log the other one out too."""
    keychain.store_token("google", {"token": "first"}, account=FIRST)
    keychain.store_token("google", {"token": "second"}, account=SECOND)
    assert keychain.delete_token("google", account=FIRST) is True
    assert keychain.load_token("google", account=FIRST) is None
    assert keychain.load_token("google", account=SECOND) == {"token": "second"}


def test_the_same_address_on_two_providers_still_differs(initialized: None) -> None:
    """One person's address at both providers is two credentials, not one."""
    assert keychain._key("google", FIRST) != keychain._key("microsoft", FIRST)


def test_an_omitted_account_keeps_the_legacy_name(
    monkeypatch: pytest.MonkeyPatch, initialized: None
) -> None:
    """Every single-account install on disk today keeps finding its token."""
    monkeypatch.setattr(keychain, "default_account", lambda provider: "")
    assert keychain._key("google") == "google-token@sample-install"


def test_the_default_account_is_the_installs_first_configured_mailbox(
    memory_keyring: _MemoryKeyring, monkeypatch: pytest.MonkeyPatch, initialized: None
) -> None:
    """An omitted account resolves to the same entry naming it explicitly does.

    This is what lets the existing single-account CALLERS keep working while
    the multi-account ones pass an address: both reach one entry per mailbox.
    """
    monkeypatch.setattr(keychain, "default_account", lambda provider: FIRST)
    keychain.store_token("google", {"token": "first"}, account=FIRST)
    assert keychain.load_token("google") == {"token": "first"}
