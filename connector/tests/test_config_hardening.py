"""What a config layer is ALLOWED to say, and what a config VALUE is allowed to be.

The layering suite next door proves the ORDER of the JSON layers and the
secrets suite proves the credential file works. This module proves the
boundaries the 0.2.8 ship gauntlet found missing -- four separate ways an
attacker-or-accident-authored config value reached somewhere it had no business
being:

* **#156** ``secrets.json`` is a CREDENTIAL file, not a full config layer. It
  sat at full precedence, so a hand-edited one flipped ``drafts_enabled`` and
  ``posture`` -- and because ``_file_posture`` read it as "the installed
  statement", its presence SUPPRESSED the env-versus-file guard that exists to
  make exactly that disagreement loud.
* **#163** ``install_id``, ``INBOX_ZERO_ENV``, and ``folder_prefix`` all become
  filesystem or provider-API structure. Each admitted separators, traversal, or
  control characters.
* **#164** the shipped ``connector-config.test.json`` carried a posture with no
  ``Posture`` member, so ``INBOX_ZERO_ENV=test`` refused to start.
* **#148 (part)** ``google_client_secret`` was a plain ``str``, so it rendered
  in ``repr()`` and ``model_dump_json()`` -- the two surfaces that reach a log.

``conftest.py`` redirects ``INBOX_ZERO_CONFIG_DIR`` and ``PROJECT_ROOT`` at a
tmp path for every test here, so ``secrets_path()`` resolves inside tmp and the
real user config is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from inbox_zero_connector import config as config_module
from inbox_zero_connector.config import (
    ENV_PREFIX,
    ConfigError,
    Posture,
    Settings,
    load_settings,
    reset_settings,
    secrets_path,
)
from inbox_zero_connector.config_paths import INSTALL_ID_ENV, install_id, reset_install_id_cache

# Fixture values, not credentials. Named rather than inlined for the reason the
# sibling suites give: an inline literal beside ``google_client_secret`` trips
# ruff's hardcoded-password rule, and silencing that rule in a config test
# installs exactly the wrong habit.
FAKE_SECRET = "GOCSPX-" + "hardening-fixture"

SHIPPED_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The tmp checkout ``_isolate_repo_layers`` already pointed PROJECT_ROOT at."""
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    return root


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# #156 -- secrets.json is a credential file, not a posture file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"drafts_enabled": {"google": True}}, id="drafts_enabled"),
        pytest.param({"posture": "file_read_draft"}, id="posture"),
        pytest.param({"classifier": {"endpoint": "https://attacker.example/v1"}}, id="classifier"),
    ],
)
def test_secrets_file_refuses_non_credential_keys(
    repo: Path, tmp_path: Path, payload: dict[str, object]
) -> None:
    """A posture or endpoint key in ``secrets.json`` is a startup refusal (#156).

    This file is the human's credential VALUES and nothing else. Letting it
    carry ``drafts_enabled`` gave the Gmail compose-scope decision a second
    home, in the one layer no audit reads -- and letting it carry a classifier
    endpoint redirects every mail body to a host of the file author's choosing.
    """
    _write(secrets_path(), payload)

    reset_settings()
    with pytest.raises(ConfigError) as excinfo:
        load_settings(tmp_path / "absent" / "connector-config.json", configure_logging=False)

    message = str(excinfo.value)
    assert "secrets.json" in message
    assert next(iter(payload)) in message


def test_secrets_file_still_carries_credentials(repo: Path, tmp_path: Path) -> None:
    """The legitimate use is untouched: ``credentials.*`` still loads (#156).

    The refusal above is worthless if it also broke the wizard's whole reason
    for this file, so this is the guard on the guard.
    """
    _write(secrets_path(), {"credentials": {"google_client_secret": FAKE_SECRET}})

    reset_settings()
    settings = load_settings(tmp_path / "absent" / "connector-config.json", configure_logging=False)

    assert settings.credentials.google_client_secret.get_secret_value() == FAKE_SECRET


def test_secrets_file_cannot_mask_the_posture_guard(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_env_may_not_flip_the_posture`` reads the AUDITED files only (#156).

    The guard's job is to refuse when the environment contradicts the installed
    posture. Sourcing its "installed" answer from ``secrets.json`` meant a
    hand-edited credential file could AGREE with the environment and silence
    the refusal, while the connector-config file a buyer actually audits still
    said something else. The guard's answer must not be maskable from there.
    """
    installed = tmp_path / "installed" / "connector-config.json"
    _write(installed, {"drafts_enabled": {"google": False, "microsoft": False}})
    # The masking value, planted where the guard used to read it from. A
    # credential layer can no longer STATE this key at all, so this file would
    # be refused outright on the loading path -- which is why the assertion
    # below interrogates the guard's source function directly. That is the
    # thing the defect was in: `_file_posture` walked every layer, so this file
    # became "the installed statement", agreed with the environment, and the
    # refusal never fired while the audited file above still said false.
    _write(secrets_path(), {"drafts_enabled": {"google": True, "microsoft": False}})
    monkeypatch.setenv(f"{ENV_PREFIX}DRAFTS_ENABLED", '{"google": true, "microsoft": false}')

    reset_settings()
    config_module.Settings._config_file = installed
    stated = config_module._file_posture()

    assert stated is not None
    value, source = stated
    assert source == installed
    assert value == {"google": False, "microsoft": False}


# ---------------------------------------------------------------------------
# #163 -- values that become filesystem or provider-API structure.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(".", id="dot"),
        pytest.param("..", id="dotdot"),
        pytest.param("a/b", id="forward-slash"),
        pytest.param("a\\b", id="backslash"),
    ],
)
def test_install_id_refuses_path_structure(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """An id that reads as a path is refused, not sanitised (#163).

    ``.`` collides with the un-namespaced default install and ``..`` collapses
    the config and data directories onto the Application Support root -- both
    of which point one install at another install's config and keychain, the
    precise collision the install id exists to prevent.
    """
    monkeypatch.setenv(INSTALL_ID_ENV, value)
    reset_install_id_cache()

    with pytest.raises(ValueError, match="install_id"):
        install_id()


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("client-a", id="plain"),
        pytest.param("client_a.2", id="dot-inside"),
    ],
)
def test_install_id_still_accepts_boring_names(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrowing the pattern did not break the names every custom zip ships."""
    monkeypatch.setenv(INSTALL_ID_ENV, value)
    reset_install_id_cache()

    assert install_id() == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("../escaped", id="traversal"),
        pytest.param("a/b", id="separator"),
        pytest.param("bad name", id="space"),
        pytest.param("bad\nname", id="newline"),
        pytest.param(".", id="dot"),
    ],
)
def test_env_name_refuses_anything_that_is_not_a_token(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``INBOX_ZERO_ENV`` is interpolated into a FILENAME, so it is validated (#163).

    It names the committed per-env config layer and the log file. Unvalidated,
    ``../escaped`` walked the log sink out of the log directory entirely.

    ``ValueError`` rather than ``ConfigError`` because this lives in
    ``config_paths`` beside ``install_id``, which raises the same way for the
    same reason; ``load_settings`` is what wraps either as a ``ConfigError``
    naming the file, and ``test_env_name_refusal_reaches_the_operator`` below
    is the proof that the operator-facing wrapping still happens.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}ENV", value)

    with pytest.raises(ValueError, match=f"{ENV_PREFIX}ENV"):
        config_module.env_name()


def test_env_name_refusal_reaches_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad ``INBOX_ZERO_ENV`` fails the LOAD, not just the helper (#163).

    The validation is worthless if the refusal never reaches the path an
    operator actually runs, so this asserts the whole way out to
    ``load_settings`` -- which is where the traversal previously escaped.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}ENV", "../escaped")

    reset_settings()
    with pytest.raises(ConfigError, match=f"{ENV_PREFIX}ENV"):
        load_settings(tmp_path / "absent" / "connector-config.json", configure_logging=False)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("prod", id="prod"),
        pytest.param("test", id="test"),
        pytest.param("dev_2", id="underscore"),
        pytest.param("staging-eu", id="dash"),
    ],
)
def test_env_name_accepts_safe_tokens(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Alphanumerics, dash, and underscore are the whole allowed vocabulary."""
    monkeypatch.setenv(f"{ENV_PREFIX}ENV", value)

    assert config_module.env_name() == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("A/B/", id="interior-slash"),
        pytest.param("/lead", id="leading-slash"),
        pytest.param("x//", id="double-trailing-slash"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("a\nb", id="newline"),
        pytest.param("a\tb", id="tab"),
        pytest.param("a\x00b", id="nul"),
    ],
)
def test_folder_prefix_refuses_unbounded_structure(value: str) -> None:
    """The prefix is pasted in front of a folder name in the owner's mailbox (#163).

    On Gmail a slash is real hierarchy, so a prefix carrying MORE than one level
    silently re-parents every taxonomy folder under labels nobody named -- and
    because ``names_encode_nesting`` is True for Google, an exclusion naming a
    parent then covers folders the installer never intended. A control character
    reaches the provider API concatenated and unvalidated.
    """
    with pytest.raises(ValidationError, match="folder_prefix"):
        Settings(folder_prefix=value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("InboxZero-", id="trailing-dash"),
        pytest.param("Inbox Zero ", id="spaces"),
        pytest.param("", id="empty-default"),
        pytest.param("InboxZero/", id="documented-gmail-hierarchy"),
        pytest.param("rTech/", id="shipped-install-prefix"),
    ],
)
def test_folder_prefix_still_accepts_real_prefixes(value: str) -> None:
    """Trailing punctuation and spaces are the POINT of a prefix; keep them.

    The two slash cases are load-bearing, not incidental. ``InboxZero/`` is the
    value ``DEFAULT_FOLDER_PREFIX`` documents as restoring the historical Gmail
    label hierarchy, and ``rTech/`` is what a real install ships -- so a blanket
    slash ban would have fixed the traversal by breaking a shipped, documented
    capability. ONE trailing level is the boundary.
    """
    assert Settings(folder_prefix=value).folder_prefix == value


# ---------------------------------------------------------------------------
# #164 -- every shipped env layer must actually construct a Settings.
# ---------------------------------------------------------------------------


def _shipped_env_layers() -> list[Path]:
    """Every committed ``connector-config.<env>.json`` beside the defaults."""
    return sorted(
        path
        for path in SHIPPED_CONFIG_DIR.glob("connector-config.*.json")
        if path.name != "connector-config.json"
    )


def test_there_is_at_least_one_shipped_env_layer() -> None:
    """Guard on the guard below: a glob that matches nothing proves nothing."""
    assert _shipped_env_layers()


@pytest.mark.parametrize("layer", _shipped_env_layers(), ids=lambda path: path.name)
def test_every_shipped_env_layer_constructs_settings(
    layer: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``INBOX_ZERO_ENV=<env>`` must START for every env file this kit ships (#164).

    The test layer shipped a ``posture`` with no ``Posture`` member, so anybody
    who followed the README and set ``INBOX_ZERO_ENV=test`` got a refusal at
    load. This walks the real committed ``config/`` directory rather than a
    fixture, because the defect was in the SHIPPED bytes.
    """
    env = layer.name.removeprefix("connector-config.").removesuffix(".json")
    monkeypatch.setattr(config_module, "PROJECT_ROOT", SHIPPED_CONFIG_DIR.parent)
    monkeypatch.setenv(f"{ENV_PREFIX}ENV", env)
    # The layer choice is resolved per call, not frozen at import, so the
    # environment variable an operator actually sets is the one that selects
    # the file. Asserting it here keeps this test honest: without it a stale
    # import-time value would silently load the prod layer and pass.
    assert config_module.env_name() == env

    reset_settings()
    settings = load_settings(tmp_path / "absent" / "connector-config.json", configure_logging=False)

    assert isinstance(settings.posture, Posture)


# ---------------------------------------------------------------------------
# #148 (part) -- the client secret does not render itself.
# ---------------------------------------------------------------------------


def test_google_client_secret_is_a_secret_str(repo: Path, tmp_path: Path) -> None:
    """``repr`` and ``model_dump_json`` are the two surfaces that reach a log (#148).

    A plain ``str`` prints in full from both, and from any exception whose
    message quotes the settings object. Same reasoning as
    ``classifier_api_key``; this field was simply missed.
    """
    _write(secrets_path(), {"credentials": {"google_client_secret": FAKE_SECRET}})

    reset_settings()
    settings = load_settings(tmp_path / "absent" / "connector-config.json", configure_logging=False)

    assert isinstance(settings.credentials.google_client_secret, SecretStr)
    assert FAKE_SECRET not in repr(settings)
    assert FAKE_SECRET not in settings.model_dump_json()
    assert settings.credentials.google_client_secret.get_secret_value() == FAKE_SECRET


def test_require_google_still_returns_the_usable_values(repo: Path, tmp_path: Path) -> None:
    """The unwrap happens at the use site, so ``keychain`` is unchanged (#148).

    ``google_client_config`` builds an OAuth client dict that the Google SDK
    reads; handing it a ``SecretStr`` would serialise the wrapper's repr into
    the client config and fail authentication at runtime rather than at load.
    """
    _write(
        secrets_path(),
        {
            "credentials": {
                "google_client_id": "installed-id.apps.googleusercontent.com",
                "google_client_secret": FAKE_SECRET,
            }
        },
    )

    reset_settings()
    creds = load_settings(
        tmp_path / "absent" / "connector-config.json", configure_logging=False
    ).credentials

    assert creds.require_google() == ("installed-id.apps.googleusercontent.com", FAKE_SECRET)


def test_require_google_still_names_both_variables_when_unset(repo: Path, tmp_path: Path) -> None:
    """Operator-facing error text is unchanged by the annotation (#148)."""
    reset_settings()
    creds = load_settings(
        tmp_path / "absent" / "connector-config.json", configure_logging=False
    ).credentials

    with pytest.raises(ConfigError) as excinfo:
        creds.require_google()

    message = str(excinfo.value)
    assert f"{ENV_PREFIX}CREDENTIALS__GOOGLE_CLIENT_ID" in message
    assert f"{ENV_PREFIX}CREDENTIALS__GOOGLE_CLIENT_SECRET" in message
