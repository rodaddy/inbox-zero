"""Tests for ``write_config``'s merge-over-installed contract (#165, #148).

Two properties are proven here and they pull in opposite directions, which is
why they share a file:

* PRESERVE what the caller does not own. ``init-config --force`` re-runs with a
  narrower argument set than the ``setup`` wizard used, so a rebuild-from-fixed-
  payload write silently reverted an install to rule-only classification. The
  installer is the only writer of this file, so a key it does not state must
  survive its own rewrite.
* DROP what does not belong. The ``credentials`` block is held to
  ``IDENTIFIER_FIELDS`` on the way IN and, since #148, on the way OUT as well:
  a hand-edited ``google_client_secret`` in ``connector-config.json`` is a
  secret VALUE in the file the installer rewrites and support requests quote,
  so a rewrite is the moment to evict it and say where it belongs.

No mail, no model, no network, no credential. ``PLANTED_SECRET`` is a canary
this suite writes and then proves absent, never a real value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inbox_zero_connector.config import (
    ClassifierSettings,
    ConfigError,
    config_path,
    load_settings,
    reset_settings,
    write_config,
)

# Not a credential -- a canary string this suite plants and then proves absent.
PLANTED_SECRET = "GOCSPX-PLANTED-writer-xyz"  # ruff: ignore[hardcoded-password-string]


def _seed(payload: dict[str, object]) -> Path:
    """Write an installed ``connector-config.json`` by hand and return its path."""
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    reset_settings()
    return target


def _installed() -> dict[str, object]:
    """Re-read the installed file as plain JSON."""
    return json.loads(config_path().read_text(encoding="utf-8"))  # type: ignore[no-any-return]


BASE: dict[str, object] = {
    "drafts_enabled": {"google": False, "microsoft": False},
    "posture": "file_only",
}


def test_rewrite_preserves_a_key_the_caller_does_not_state() -> None:
    """A ``classifier`` block written by ``setup`` survives an ``init-config`` rewrite.

    This is the #165 P1: ``--force`` without the classifier flags used to blank
    the block, dropping the install to rule-only with nothing said about it.
    """
    _seed({
        **BASE,
        "classifier": {
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "local-m",
            "requires_key": False,
        },
    })
    settings = load_settings(configure_logging=False)
    assert settings.classifier.base_url is not None

    write_config(settings.connector_config())

    reset_settings()
    assert load_settings(configure_logging=False).classifier.base_url is not None
    assert _installed()["classifier"]["model"] == "local-m"  # type: ignore[index]


def test_caller_stated_block_still_wins_over_the_installed_one() -> None:
    """Merging preserves; it does not freeze. An explicit block replaces."""
    _seed({**BASE, "classifier": {"base_url": "http://old/v1", "model": "old-m"}})
    settings = load_settings(configure_logging=False)

    write_config(
        settings.connector_config(),
        classifier=ClassifierSettings(base_url="http://new/v1", model="new-m"),
    )

    assert _installed()["classifier"]["model"] == "new-m"  # type: ignore[index]


def test_logging_block_survives_a_rewrite_that_does_not_state_it() -> None:
    """Every key the installer owns but this call does not name is preserved.

    ``classifier`` is the one that bit (#165); this pins the general property on
    a second, unrelated block so a future writer cannot fix one and re-break the
    rest. ``Settings`` is ``extra=forbid``, so the block must be a real field --
    an invented key would fail to LOAD, not to merge.
    """
    _seed({**BASE, "logging": {"level": "DEBUG"}})
    settings = load_settings(configure_logging=False)
    assert settings.logging.level == "DEBUG"

    write_config(settings.connector_config())

    assert _installed()["logging"] == {"level": "DEBUG"}


def test_non_identifier_credential_is_dropped_on_rewrite(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hand-placed ``google_client_secret`` does not survive the next write (#148).

    ``IDENTIFIER_FIELDS`` is an allowlist on the caller's keys; before this it
    was not applied to what the FILE already said, so a secret typed into the
    wrong file persisted through every wizard rewrite and shipped in the
    diagnose bundle.
    """
    _seed({
        **BASE,
        "credentials": {
            "google_client_id": "id-123.apps.googleusercontent.com",
            "google_client_secret": PLANTED_SECRET,
        },
    })
    settings = load_settings(configure_logging=False)

    write_config(settings.connector_config())

    credentials = _installed()["credentials"]
    assert isinstance(credentials, dict)
    assert credentials["google_client_id"] == "id-123.apps.googleusercontent.com"
    assert "google_client_secret" not in credentials
    assert PLANTED_SECRET not in config_path().read_text(encoding="utf-8")


def test_dropping_a_non_identifier_warns_and_never_quotes_the_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The eviction is announced by KEY and says where the value belongs."""
    from loguru import logger

    records: list[str] = []
    sink = logger.add(records.append, level="WARNING")
    try:
        _seed({**BASE, "credentials": {"google_client_secret": PLANTED_SECRET}})
        write_config(load_settings(configure_logging=False).connector_config())
    finally:
        logger.remove(sink)

    warning = "".join(records)
    assert "google_client_secret" in warning
    assert "secrets.json" in warning
    assert PLANTED_SECRET not in warning


def test_caller_passing_a_non_identifier_is_still_refused_before_any_write() -> None:
    """The inbound allowlist is unchanged: a bad key raises, nothing is written."""
    target = _seed(BASE)
    before = target.read_text(encoding="utf-8")

    config = load_settings(configure_logging=False).connector_config()
    with pytest.raises(ConfigError, match="google_client_secret"):
        write_config(config, identifiers={"google_client_secret": PLANTED_SECRET})

    assert target.read_text(encoding="utf-8") == before
