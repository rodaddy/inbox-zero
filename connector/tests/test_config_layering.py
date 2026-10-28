"""The four JSON layers, and which one wins.

``settings_customise_sources`` declares ONE ``JsonConfigSettingsSource`` over a
list, lowest precedence first: the committed ``config/connector-config.json``,
the committed per-env file beside it, the installed per-user file, then
``secrets/config.json``. These tests are the proof that order is real, because
the order is the whole security-relevant claim -- ``secrets/`` overlaying its
siblings rather than replacing them is what keeps a client ID and a client
secret in two files without either blanking the other.

The ``_isolate_repo_layers`` fixture in ``conftest.py`` points ``PROJECT_ROOT``
at a tmp directory for every test in the suite, so a test that wants a committed
layer writes one itself. That is deliberate: reading the real checkout would
make these assertions depend on whatever ``config/`` says today.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inbox_zero_connector.config import (
    ENV_PREFIX,
    ConfigError,
    load_settings,
    reset_settings,
)

# The real committed defaults, read once so a test can assert the SHIPPED file
# is what a fresh clone runs on rather than a value invented here.
REAL_COMMITTED = Path(__file__).resolve().parents[1] / "config" / "connector-config.json"

# A fixture value, not a credential. Named rather than inlined so the secrets
# assertion below compares two NAMES -- an inline literal beside
# ``google_client_secret`` trips ruff's hardcoded-password rule, and silencing
# that rule in a config test is exactly the wrong habit to install.
FAKE_SECRET = "GOCSPX-" + "from-secrets"


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


def test_fresh_clone_runs_from_committed_config_alone(repo: Path, tmp_path: Path) -> None:
    """No install step, no env: the committed file is a complete configuration.

    This is the point of the layering. Before it, a clone loaded code defaults
    and the committed file was documentation that nothing read.
    """
    committed = json.loads(REAL_COMMITTED.read_text(encoding="utf-8"))
    _write(repo / "config" / "connector-config.json", committed)

    empty_install = tmp_path / "not-installed" / "connector-config.json"
    reset_settings()
    settings = load_settings(empty_install, configure_logging=False)

    assert not empty_install.exists()
    # Values taken FROM the shipped file, so this fails if the file stops being
    # the thing that is loaded.
    assert settings.classifier.model == committed["classifier"]["model"]
    assert settings.credentials.google_client_id == committed["credentials"]["google_client_id"]


def test_installed_file_overrides_committed_defaults(repo: Path, tmp_path: Path) -> None:
    """Layer 3 beats layer 1: a per-user install outranks the repo default."""
    _write(repo / "config" / "connector-config.json", {"logging": {"level": "INFO"}})
    installed = tmp_path / "installed" / "connector-config.json"
    _write(installed, {"logging": {"level": "ERROR"}})

    reset_settings()
    assert load_settings(installed, configure_logging=False).logging.level == "ERROR"


def test_secrets_overlay_credentials_without_blanking_siblings(repo: Path, tmp_path: Path) -> None:
    """``deep_merge`` is what lets one credential group live in two files.

    Without it the last file to mention ``credentials`` REPLACES the whole
    object, so writing the secret to ``secrets/`` would silently drop the
    client ID the committed file supplied.
    """
    _write(
        repo / "config" / "connector-config.json",
        {"credentials": {"google_client_id": "committed-id.apps.googleusercontent.com"}},
    )
    _write(
        repo / "secrets" / "config.json",
        {"credentials": {"google_client_secret": FAKE_SECRET}},
    )

    reset_settings()
    creds = load_settings(
        tmp_path / "absent" / "connector-config.json", configure_logging=False
    ).credentials

    assert creds.google_client_id == "committed-id.apps.googleusercontent.com"
    assert creds.google_client_secret.get_secret_value() == FAKE_SECRET


def test_env_still_beats_every_file(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding layers did not demote the environment.

    ``folder_prefix`` is a preference, so ordinary precedence applies and the
    operator's variable outranks all four files.
    """
    _write(repo / "config" / "connector-config.json", {"folder_prefix": "Committed/"})
    installed = tmp_path / "installed" / "connector-config.json"
    _write(installed, {"folder_prefix": "Installed/"})
    monkeypatch.setenv(f"{ENV_PREFIX}FOLDER_PREFIX", "FromEnv/")

    reset_settings()
    assert load_settings(installed, configure_logging=False).folder_prefix == "FromEnv/"


def test_a_per_env_layer_overrides_the_base_committed_file(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer 2 beats layer 1, and the layer NAME comes from the same env value.

    Setting ``INBOX_ZERO_ENV`` is now the whole of it. This used to have to
    monkeypatch a ``Settings._env_name`` ClassVar instead, because that value
    was read once at class-definition time -- so the variable an operator
    actually exported could not select the layer after import, and a test that
    only set it would pass against a stale layer name and prove nothing. The
    layer choice resolves per call through ``config_paths.env_name`` now (#163),
    which is what makes the variable and the loaded layer agree.
    """
    _write(repo / "config" / "connector-config.json", {"logging": {"level": "INFO"}})
    _write(repo / "config" / "connector-config.test.json", {"logging": {"level": "DEBUG"}})
    monkeypatch.setenv(f"{ENV_PREFIX}ENV", "test")

    reset_settings()
    assert (
        load_settings(
            tmp_path / "absent" / "connector-config.json", configure_logging=False
        ).logging.level
        == "DEBUG"
    )


def test_posture_validator_reads_the_layered_files(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must see a posture stated in a COMMITTED layer.

    This is the defect the helper exists to prevent: the validator used to read
    the installed file directly, so a posture that lived only in ``config/``
    was invisible to it and the environment could flip it in silence.
    """
    _write(
        repo / "config" / "connector-config.json",
        {"drafts_enabled": {"google": False, "microsoft": False}},
    )
    monkeypatch.setenv(
        f"{ENV_PREFIX}DRAFTS_ENABLED", json.dumps({"google": True, "microsoft": True})
    )

    reset_settings()
    with pytest.raises(ConfigError, match="drafts_enabled disagrees"):
        load_settings(tmp_path / "absent" / "connector-config.json", configure_logging=False)


def test_agreeing_layers_do_not_trip_the_posture_refusal(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control the refusal above needs to mean anything.

    A validator that raised on every environment-set posture would satisfy the
    test above while telling an operator nothing about agreement.
    """
    _write(
        repo / "config" / "connector-config.json",
        {"drafts_enabled": {"google": True, "microsoft": False}},
    )
    monkeypatch.setenv(
        f"{ENV_PREFIX}DRAFTS_ENABLED", json.dumps({"google": True, "microsoft": False})
    )

    reset_settings()
    settings = load_settings(tmp_path / "absent" / "connector-config.json", configure_logging=False)
    assert settings.drafts_enabled == {"google": True, "microsoft": False}
