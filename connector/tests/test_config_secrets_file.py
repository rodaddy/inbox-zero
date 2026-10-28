"""The per-user secrets file: where a credential VALUE is allowed to live.

The layering suite next door proves the ORDER of the JSON layers. This module
proves the layer added for the wizard: ``config_dir()/secrets.json``, a file a
human fills in by hand and no code path ever writes a real value into. It sits
above the installed ``connector-config.json`` and below the repo's
``secrets/config.json``, so an operator can keep a client secret out of the file
the installer rewrites without giving up the repo-level override a developer
uses.

The security-relevant claims, each with a test below:

* Identifiers (client IDs, tenant IDs) may be written by the installer;
  ``write_config`` REFUSES a secret-named key rather than accepting it into
  ``connector-config.json``.
* ``write_secrets_skeleton`` creates an EMPTY file once and never clobbers a
  hand-edited one.
* A value planted in that file never reaches stdout or the logs -- redaction in
  ``utils.logging_config`` is name-based and already covers these names, so this
  suite is the proof, not the implementation.

``conftest.py`` redirects ``INBOX_ZERO_CONFIG_DIR`` and ``PROJECT_ROOT`` at a
tmp path for every test in the suite, so ``secrets_path()`` here resolves inside
tmp and the real user config is never touched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from inbox_zero_connector.apps.cli import main as cli_main
from inbox_zero_connector.config import (
    ClassifierSettings,
    ConfigError,
    config_dir,
    default_config,
    load_settings,
    reset_settings,
    secrets_path,
    write_config,
    write_secrets_skeleton,
)

# Fixture values, not credentials. Named rather than inlined for the same reason
# ``test_config_layering.py`` names its own: an inline literal beside
# ``google_client_secret`` trips ruff's hardcoded-password rule, and silencing
# that rule in a config test installs exactly the wrong habit.
FAKE_SECRET = "GOCSPX-" + "from-user-secrets"
FAKE_REPO_SECRET = "GOCSPX-" + "from-repo-secrets"
FAKE_ENV_SECRET = "GOCSPX-" + "from-env"


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


def test_secrets_path_is_in_the_config_dir() -> None:
    """The file lives beside the installed config, not in the repo or the data dir."""
    assert secrets_path() == config_dir() / "secrets.json"


def test_user_secrets_file_loads_credentials(repo: Path, tmp_path: Path) -> None:
    """The whole point: a secret in this file reaches ``settings.credentials``."""
    _write(secrets_path(), {"credentials": {"google_client_secret": FAKE_SECRET}})

    reset_settings()
    settings = load_settings(tmp_path / "absent" / "connector-config.json", configure_logging=False)

    assert settings.credentials.google_client_secret.get_secret_value() == FAKE_SECRET


def test_user_secrets_overlay_installed_identifiers(repo: Path, tmp_path: Path) -> None:
    """Deep merge, not replace: the ID in the installed file survives the overlay.

    This is the arrangement the wizard produces -- identifiers written by the
    installer, the secret typed in by the human -- so a shallow merge here would
    blank whichever file was written second.
    """
    installed = tmp_path / "installed" / "connector-config.json"
    _write(
        installed, {"credentials": {"google_client_id": "installed-id.apps.googleusercontent.com"}}
    )
    _write(secrets_path(), {"credentials": {"google_client_secret": FAKE_SECRET}})

    reset_settings()
    creds = load_settings(installed, configure_logging=False).credentials

    assert creds.google_client_id == "installed-id.apps.googleusercontent.com"
    assert creds.google_client_secret.get_secret_value() == FAKE_SECRET


def test_repo_secrets_layer_beats_the_user_secrets_file(repo: Path, tmp_path: Path) -> None:
    """``secrets/config.json`` stays the top file layer, above the new one."""
    _write(secrets_path(), {"credentials": {"google_client_secret": FAKE_SECRET}})
    _write(
        repo / "secrets" / "config.json",
        {"credentials": {"google_client_secret": FAKE_REPO_SECRET}},
    )

    reset_settings()
    creds = load_settings(
        tmp_path / "absent" / "connector-config.json", configure_logging=False
    ).credentials

    assert creds.google_client_secret.get_secret_value() == FAKE_REPO_SECRET


def test_env_beats_the_user_secrets_file(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a fifth file layer did not demote the environment."""
    _write(secrets_path(), {"credentials": {"google_client_secret": FAKE_SECRET}})
    monkeypatch.setenv("INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_SECRET", FAKE_ENV_SECRET)

    reset_settings()
    creds = load_settings(
        tmp_path / "absent" / "connector-config.json", configure_logging=False
    ).credentials

    assert creds.google_client_secret.get_secret_value() == FAKE_ENV_SECRET


def test_identifiers_load_from_the_installed_config_file(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-secret identifiers are allowed in the file the installer rewrites.

    The two ``delenv`` calls are not tidiness. This asserts what the FILE layer
    produces, and the environment layer outranks it by design -- so on a
    developer machine that exports a real ``INBOX_ZERO_CREDENTIALS__*`` for
    hand-testing, this test reads that operator's own client ID and fails while
    the loader is behaving exactly as specified. Clearing them makes the test
    measure the layer it names. ``test_env_beats_the_user_secrets_file`` above
    is where the environment's precedence is asserted, deliberately.
    """
    monkeypatch.delenv("INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID", raising=False)
    monkeypatch.delenv("INBOX_ZERO_CREDENTIALS__MICROSOFT_TENANT_ID", raising=False)
    installed = tmp_path / "installed" / "connector-config.json"
    _write(
        installed,
        {
            "credentials": {
                "microsoft_client_id": "ms-client-id",
                "microsoft_tenant_id": "ms-tenant-id",
            }
        },
    )

    reset_settings()
    creds = load_settings(installed, configure_logging=False).credentials

    assert creds.microsoft_client_id == "ms-client-id"
    assert creds.microsoft_tenant_id == "ms-tenant-id"


def test_write_config_persists_identifiers(tmp_path: Path) -> None:
    """Identifiers round-trip, and a later write with ``None`` leaves them alone.

    The second call is the real assertion. The wizard rewrites this file for
    unrelated reasons (posture, routing), and a rewrite that dropped the
    credentials block would silently un-configure the OAuth client.
    """
    target = tmp_path / "installed" / "connector-config.json"

    write_config(
        default_config(),
        target,
        identifiers={"microsoft_client_id": "ms-client-id", "microsoft_tenant_id": "ms-tenant-id"},
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["credentials"] == {
        "microsoft_client_id": "ms-client-id",
        "microsoft_tenant_id": "ms-tenant-id",
    }

    write_config(default_config(), target)
    preserved = json.loads(target.read_text(encoding="utf-8"))
    assert preserved["credentials"] == payload["credentials"]


def test_write_config_refuses_secret_named_identifiers(tmp_path: Path) -> None:
    """A secret offered to the identifier allowlist is a REFUSAL, not a write.

    Accepting it would put a client secret in the file the installer rewrites
    and the operator is told is safe to keep -- the exact failure the separate
    secrets file exists to prevent.
    """
    target = tmp_path / "installed" / "connector-config.json"

    with pytest.raises(ConfigError) as excinfo:
        write_config(default_config(), target, identifiers={"google_client_secret": "x"})

    assert "google_client_secret" in str(excinfo.value)


def test_write_config_persists_classifier(tmp_path: Path) -> None:
    """The vendor switch is installer-writable, and it carries no key field."""
    target = tmp_path / "installed" / "connector-config.json"
    classifier = ClassifierSettings(
        base_url="http://localhost:1234/v1", model="m", requires_key=True
    )

    write_config(default_config(), target, classifier=classifier)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["classifier"]["base_url"] == "http://localhost:1234/v1"
    assert payload["classifier"]["model"] == "m"

    reset_settings()
    settings = load_settings(target, configure_logging=False)
    assert settings.classifier.base_url == "http://localhost:1234/v1"
    assert settings.classifier.model == "m"
    assert settings.classifier.requires_key is True


def test_secrets_skeleton_created_once() -> None:
    """Created empty on the first call; a hand-edited value survives the second."""
    path, created = write_secrets_skeleton()

    assert path == secrets_path()
    assert created is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["credentials"]["google_client_secret"] == ""
    assert payload["credentials"]["classifier_api_key"] is None

    payload["credentials"]["google_client_secret"] = FAKE_SECRET
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    path_again, created_again = write_secrets_skeleton()

    assert path_again == path
    assert created_again is False
    reread = json.loads(path.read_text(encoding="utf-8"))
    assert reread["credentials"]["google_client_secret"] == FAKE_SECRET


def test_planted_secret_never_reaches_stdout_or_logs(
    repo: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """The ``paths`` command names the new file and never prints what is in it.

    Redaction is name-based and lives in ``utils.logging_config``; nothing here
    changes it. This test is the PROOF that the name-based rule already covers
    the names this file introduces.
    """
    _write(secrets_path(), {"credentials": {"google_client_secret": FAKE_SECRET}})

    reset_settings()
    with caplog.at_level(logging.DEBUG):
        cli_main.main(["paths"])

    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out
    assert FAKE_SECRET not in captured.err
    assert not [record for record in caplog.records if FAKE_SECRET in record.getMessage()]
    assert "secrets.json" in captured.out
