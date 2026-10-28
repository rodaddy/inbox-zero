"""Per-install isolation: one machine, two mailboxes, zero shared state.

THE FAILURE THIS PREVENTS
    Two managed installs on one laptop resolve the SAME OS-standard config
    directory, the same data directory, and the same keychain account. The
    second ``auth`` overwrites the first mailbox's token under an identical
    account name, and the symptom is the connector filing one client's mail
    into another client's folders -- a mailbox-level data exposure that looks
    like a triage bug rather than a packaging one. ``install_id`` is the
    namespace that makes the collision impossible.

WHY THE DEFAULT MUST BE BYTE-IDENTICAL
    Every install that already exists has no id. If the empty case appended so
    much as an empty segment, every one of them would resolve a NEW directory
    on upgrade, find no taxonomy and no learned rules, and behave like a fresh
    install holding somebody's live mailbox. ``test_empty_install_id_is_byte_
    identical`` compares against the raw platformdirs value for exactly that
    reason -- it is not a tautology, it is the upgrade-safety property.

WHY THE KEYCHAIN DOES NOT GET THAT SAME DEFAULT (#171)
    The byte-identical rule is a FILESYSTEM rule and stops at the filesystem.
    A default directory belongs to this install; the default un-namespaced
    keychain account belongs to whichever install on the machine authenticated
    first, because the OS keychain is one store per user and no
    ``INBOX_ZERO_*`` variable scopes it. So the empty id resolves the historical
    directories exactly and REFUSES to produce an account name.

WHY THESE TESTS FIGHT THE CONFTEST
    ``conftest.isolated_dirs`` sets ``INBOX_ZERO_CONFIG_DIR`` for every test in
    the suite, and an explicit override deliberately BYPASSES the install-id
    branch (an operator who names a directory means that directory). So every
    test here that exercises the platformdirs branch must ``monkeypatch.delenv``
    it first; without that they would pass while testing nothing at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from inbox_zero_connector import config_paths as paths_module
from inbox_zero_connector.config import (
    ConfigError,
    Settings,
    config_dir,
    data_dir,
    install_id,
)
from inbox_zero_connector.config_paths import (
    CONFIG_DIR_ENV,
    DATA_DIR_ENV,
    INSTALL_ID_ENV,
    reset_install_id_cache,
)
from inbox_zero_connector.services.auth import keychain


@pytest.fixture(autouse=True)
def _fresh_install_id_cache() -> Any:
    """The layer-1 read is cached per process, so every test starts from cold."""
    reset_install_id_cache()
    yield
    reset_install_id_cache()


@pytest.fixture
def platformdirs_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drop the conftest directory overrides and pin platformdirs at ``tmp_path``.

    Both halves are required. Dropping the overrides is what reaches the
    install-id branch at all; pinning platformdirs is what keeps the assertions
    off the real user config directory on the machine running the suite.
    """
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    base_config = tmp_path / "pd-config"
    base_data = tmp_path / "pd-data"
    monkeypatch.setattr(paths_module, "user_config_path", lambda *a, **k: base_config)
    monkeypatch.setattr(paths_module, "user_data_path", lambda *a, **k: base_data)
    return tmp_path


def _write_layer_one(monkeypatch: pytest.MonkeyPatch, root: Path, payload: object) -> None:
    """Write ``config/connector-config.json`` into a fake PROJECT_ROOT.

    Patched on ``config_paths``, not on ``config``. Both bind the name from
    ``config_constants``, and the resolver under test reads its OWN module
    global -- patching the re-export site would leave the real checkout in
    play and quietly assert against whatever this repo's committed config says
    today, which is a test that passes without testing anything.
    """
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "connector-config.json").write_text(
        json.dumps(payload) if not isinstance(payload, str) else payload,
        encoding="utf-8",
    )
    monkeypatch.setattr(paths_module, "PROJECT_ROOT", root)


def test_empty_install_id_is_byte_identical(platformdirs_branch: Path) -> None:
    """No id anywhere means the historical path, exactly. The upgrade guarantee."""
    assert install_id() == ""
    assert config_dir() == platformdirs_branch / "pd-config"
    assert data_dir() == platformdirs_branch / "pd-data"


def test_env_install_id_namespaces_both_directories(
    platformdirs_branch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``INBOX_ZERO_INSTALL_ID`` appends one segment to config AND data."""
    monkeypatch.setenv(INSTALL_ID_ENV, "sample-install")
    assert install_id() == "sample-install"
    assert config_dir() == platformdirs_branch / "pd-config" / "sample-install"
    assert data_dir() == platformdirs_branch / "pd-data" / "sample-install"


def test_install_id_read_from_layer_one_file(
    platformdirs_branch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no env var, the committed layer-1 config supplies the id.

    This is the path every shipped custom zip takes: the id travels inside the
    artifact, so an install is namespaced without the operator setting anything.
    """
    _write_layer_one(monkeypatch, platformdirs_branch / "repo", {"install_id": "acme"})
    assert install_id() == "acme"
    assert config_dir() == platformdirs_branch / "pd-config" / "acme"


def test_env_install_id_beats_the_file(
    platformdirs_branch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment is the higher layer, here as everywhere else."""
    _write_layer_one(monkeypatch, platformdirs_branch / "repo", {"install_id": "from-file"})
    monkeypatch.setenv(INSTALL_ID_ENV, "from-env")
    assert install_id() == "from-env"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"install_id": ""},
        {"install_id": 17},
        {"install_id": None},
        "{ not json at all",
        [1, 2, 3],
    ],
    ids=["absent", "empty", "not-a-string", "null", "malformed", "not-an-object"],
)
def test_unusable_layer_one_falls_back_to_no_namespace(
    platformdirs_branch: Path, monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """A broken or silent layer 1 must not break path resolution.

    Every one of these is a live install on somebody's machine. Raising here
    would take the connector down over a file that has no bearing on whether
    it can read mail; the safe answer is the un-namespaced default.
    """
    _write_layer_one(monkeypatch, platformdirs_branch / "repo", payload)
    assert install_id() == ""
    assert config_dir() == platformdirs_branch / "pd-config"


def test_missing_layer_one_file_falls_back_to_no_namespace(
    platformdirs_branch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout with no committed config resolves the historical path."""
    monkeypatch.setattr(paths_module, "PROJECT_ROOT", platformdirs_branch / "nothing-here")
    assert install_id() == ""
    assert config_dir() == platformdirs_branch / "pd-config"


@pytest.mark.parametrize(
    "bad",
    ["../escape", "a/b", "has space", "tab\there", "x" * 65, "semi;colon", "at@sign"],
    ids=["traversal", "separator", "space", "tab", "too-long", "semicolon", "at-sign"],
)
def test_unusable_install_id_is_refused(
    platformdirs_branch: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Anything a path or a keychain account would read as structure raises.

    Refused rather than sanitised: a rewritten id silently points one install
    at ANOTHER install's directory and token, which is the exact collision the
    namespace exists to prevent. ``..`` is the sharp case -- it would resolve
    the parent, so a traversal id un-namespaces the install it claimed to isolate.
    """
    monkeypatch.setenv(INSTALL_ID_ENV, bad)
    with pytest.raises(ValueError, match="install_id"):
        install_id()
    with pytest.raises(ValueError, match="install_id"):
        config_dir()


def test_explicit_config_dir_override_ignores_the_install_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named directory is used verbatim -- no id segment appended.

    The suite itself depends on this: ``conftest`` isolates every test with the
    override, so an id leaking into that branch would relocate the fixtures.
    """
    named = tmp_path / "named"
    monkeypatch.setenv(CONFIG_DIR_ENV, str(named))
    monkeypatch.setenv(DATA_DIR_ENV, str(named))
    monkeypatch.setenv(INSTALL_ID_ENV, "sample-install")
    assert config_dir() == named
    assert data_dir() == named


def test_settings_accepts_and_validates_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """The field exists, defaults empty, and refuses the same shapes the resolver does."""
    assert Settings().install_id == ""
    assert Settings(install_id="sample-install-2").install_id == "sample-install-2"
    with pytest.raises(ValueError, match="install_id"):
        Settings(install_id="../escape")


class _FakeKeyring:
    """A keychain that records account names instead of touching the OS one."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[service, account] = value

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    """Swap ``keyring`` inside the keychain module for an in-memory recorder."""
    fake = _FakeKeyring()
    monkeypatch.setattr(keychain, "keyring", fake)
    return fake


def test_keychain_refuses_to_key_without_an_install_id(
    fake_keyring: _FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTRACT REVERSED BY #171. This test used to assert the opposite.

    It previously required the un-namespaced account ``google-token``, on the
    reasoning that an existing install must keep finding the token it already
    stored -- the same upgrade-safety argument that correctly governs the
    DIRECTORY defaults above. The argument does not transfer, and the 0.2.9
    release smoke is why: a directory that already exists is THIS install's
    directory, but the bare keychain account is shared by every install on the
    machine, so "keep finding what is there" means "adopt whichever mailbox got
    there first". A scratch-pinned fresh unzip authenticated against a real
    mailbox and filed real mail through exactly this path.

    So the empty case now RAISES, and the directory tests above are untouched:
    the filesystem default stays byte-identical, and only the global store
    refuses.
    """
    monkeypatch.delenv(INSTALL_ID_ENV, raising=False)
    with pytest.raises(ConfigError):
        keychain.store_token("google", {"token": "x"})
    assert fake_keyring.store == {}


def test_keychain_account_carries_the_install_id(
    fake_keyring: _FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an id, the account is suffixed so two installs cannot collide."""
    monkeypatch.setenv(INSTALL_ID_ENV, "sample-install")
    keychain.store_token("google", {"token": "x"})
    assert (keychain.SERVICE_NAME, "google-token@sample-install") in fake_keyring.store
    assert (keychain.SERVICE_NAME, "google-token") not in fake_keyring.store


def test_two_installs_do_not_share_a_token(
    fake_keyring: _FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE headline property, asserted end to end through store and load.

    Same provider, same process, two ids: the second write must not be
    readable as the first one's token.
    """
    monkeypatch.setenv(INSTALL_ID_ENV, "one")
    keychain.store_token("google", {"token": "first"})
    monkeypatch.setenv(INSTALL_ID_ENV, "two")
    keychain.store_token("google", {"token": "second"})
    assert keychain.load_token("google") == {"token": "second"}
    monkeypatch.setenv(INSTALL_ID_ENV, "one")
    assert keychain.load_token("google") == {"token": "first"}
