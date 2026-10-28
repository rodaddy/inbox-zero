"""The keychain account name refuses to exist without an install id (#171).

THE FAILURE THIS PREVENTS
    ``INBOX_ZERO_CONFIG_DIR`` and ``INBOX_ZERO_DATA_DIR`` sandbox the
    FILESYSTEM. The OS keychain is GLOBAL to the machine and those variables
    do not reach it. So an uninitialized install -- a fresh unzip with no
    ``install_id`` -- used to build the account name ``google-token``, which is
    exactly the un-namespaced name a legacy install on the same machine had
    already written a live token under. The 0.2.9 release smoke proved the
    consequence end to end: a scratch-pinned ``watch --once`` authenticated
    against somebody's real mailbox, filed real mail, and wrote a refreshed
    token back to the borrowed account.

    The fix is that the account name cannot degrade. No id means no key, and
    no key means the credential call never happens at all.

WHY THE ASSERTIONS ARE ABOUT CALLS, NOT RETURN VALUES
    "Returns ``None``" would be satisfied by a lookup that MISSED. The
    property that matters is that the keychain was never asked, because on
    the machine in the incident the lookup would have HIT. So the fake here
    records every call and the assertion is a count of zero -- the one shape
    that could have caught the original bug.
"""

from __future__ import annotations

from typing import Any

import pytest

from inbox_zero_connector.config import ConfigError
from inbox_zero_connector.config_paths import INSTALL_ID_ENV, reset_install_id_cache
from inbox_zero_connector.services.auth import keychain


@pytest.fixture(autouse=True)
def _fresh_install_id_cache() -> Any:
    """The layer-1 read is cached per process, so every test starts from cold."""
    reset_install_id_cache()
    yield
    reset_install_id_cache()


class _RecordingKeyring:
    """A keychain that records every call instead of touching the OS one.

    Unlike the in-memory store used by the namespacing tests, this one exists
    to answer "was it asked at all" -- ``calls`` is the assertion surface.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, value: str) -> None:
        self.calls.append(("set", service, account))
        self.store[service, account] = value

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append(("get", service, account))
        return self.store.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        self.calls.append(("delete", service, account))
        self.store.pop((service, account), None)


@pytest.fixture
def recording_keyring(monkeypatch: pytest.MonkeyPatch) -> _RecordingKeyring:
    """Swap ``keyring`` inside the keychain module for a call recorder."""
    fake = _RecordingKeyring()
    monkeypatch.setattr(keychain, "keyring", fake)
    return fake


@pytest.fixture
def uninitialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """No install id from any layer -- the fresh-unzip state from the incident."""
    monkeypatch.delenv(INSTALL_ID_ENV, raising=False)
    monkeypatch.setattr(keychain, "install_id", lambda: "")


def test_key_refuses_an_empty_install_id(uninitialized: None) -> None:
    """The constructor raises rather than returning the bare account name."""
    with pytest.raises(ConfigError) as caught:
        keychain._key("google")
    assert "google-token" not in str(caught.value)


def test_refusal_names_the_setup_flow_and_no_secret(uninitialized: None) -> None:
    """The operator is told what to run; nothing token-like is in the text."""
    with pytest.raises(ConfigError) as caught:
        keychain._key("google")
    message = str(caught.value)
    assert "setup" in message
    assert "token" not in message.lower().replace("inbox-zero-connector", "")


def test_key_still_suffixes_when_an_install_id_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The namespacing behaviour is unchanged for every initialized install."""
    monkeypatch.setenv(INSTALL_ID_ENV, "sample-install")
    assert keychain._key("google") == "google-token@sample-install"


def test_load_never_reaches_the_keychain_without_an_install_id(
    recording_keyring: _RecordingKeyring, uninitialized: None
) -> None:
    """THE headline property: the borrowed-token lookup does not happen."""
    with pytest.raises(ConfigError):
        keychain.load_token("google")
    assert recording_keyring.calls == []


def test_store_never_reaches_the_keychain_without_an_install_id(
    recording_keyring: _RecordingKeyring, uninitialized: None
) -> None:
    """The write-back half of the incident: a refreshed token cannot land."""
    with pytest.raises(ConfigError):
        keychain.store_token("google", {"token": "x"})
    assert recording_keyring.calls == []


def test_delete_never_reaches_the_keychain_without_an_install_id(
    recording_keyring: _RecordingKeyring, uninitialized: None
) -> None:
    """Deleting somebody else's credential is the same defect in reverse."""
    with pytest.raises(ConfigError):
        keychain.delete_token("google")
    assert recording_keyring.calls == []


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_every_provider_refuses(
    recording_keyring: _RecordingKeyring, uninitialized: None, provider: str
) -> None:
    """The refusal is a property of the constructor, not of one provider."""
    with pytest.raises(ConfigError):
        keychain.load_token(provider)
    assert recording_keyring.calls == []
