"""Scope survives the round trip to disk -- the installer payload and its keys.

Split out of ``test_mailbox_scope.py`` on 2026-08-19 (500-code-line ceiling).
Its siblings test ``MailboxScope`` as a PURE function held in memory; this file
owns the part that touches a FILESYSTEM, which is a different failure mode and
a different fixture need (every test here takes ``tmp_path``).

The question this file answers is whether the answer a client gave at install
time is still the answer after ``write_config`` -> disk -> ``load_config``:

* the round trip preserves fields AND behaviour, not merely field equality;
* both providers appear in the written payload, so the file can be audited;
* scope is keyed PER PROVIDER, so configuring one mailbox never widens the
  other -- the silent data-exposure change these tests exist to prevent;
* a config predating the scope model, an absent file, an unknown provider key,
  and an environment override all resolve to a narrow, per-provider answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# The named scopes and the config surface come from the harness so the halves of
# this split cannot drift into probing differently-constructed scopes that share
# a name.
from _mailbox_scope_harness import (
    DEFAULT_SCOPES,
    GOOGLE,
    MICROSOFT,
    PROVIDER_NAMES,
    ConfigError,
    ConnectorConfig,
    MailboxScope,
    ValidationError,
    default_scope,
    load_config,
    write_config,
)

# ---------------------------------------------------------------------------
# Round trip through the installer JSON payload
# ---------------------------------------------------------------------------


def test_scope_round_trips_through_the_installer_payload(tmp_path: Path) -> None:
    """write_config -> disk -> load_config preserves fields AND behaviour.

    Field equality alone would pass if ``covers`` stopped consulting a field,
    so the reloaded scope is asked the question a watcher actually asks.
    """
    target = tmp_path / "connector-config.json"
    scope = MailboxScope(
        mode="all_except",
        excluded_folders=("Client Matters", "Shared Mailbox"),
        include_junk=True,
    )
    other = MailboxScope(mode="only_listed", included_folders=("Billing",))
    write_config(ConnectorConfig(scope={GOOGLE: scope, MICROSOFT: other}), target)

    loaded = load_config(target)
    assert loaded.mailbox_scope(GOOGLE) == scope
    assert loaded.mailbox_scope(MICROSOFT) == other
    assert loaded.mailbox_scope(GOOGLE).covers("Client Matters") is False
    assert loaded.mailbox_scope(GOOGLE).covers("Junk Email") is True
    assert loaded.mailbox_scope(MICROSOFT).covers("billing") is True
    assert loaded.mailbox_scope(MICROSOFT).covers("Client Matters") is False


def test_the_written_payload_names_every_provider_scope(tmp_path: Path) -> None:
    """A human opens this file to audit what the connector reads.

    Both providers must appear even when one was never configured -- a value
    only ever supplied by a default is a value the file cannot be audited for.
    """
    target = tmp_path / "connector-config.json"
    write_config(ConnectorConfig(scope={GOOGLE: MailboxScope(mode="all_folders")}), target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["mailbox_scope"][GOOGLE]["mode"] == "all_folders"
    assert payload["mailbox_scope"][MICROSOFT]["mode"] == "inbox_only"


def test_an_invalid_scope_on_disk_fails_the_load(tmp_path: Path) -> None:
    """The empty-list contradiction must be caught on the disk path too."""
    target = tmp_path / "connector-config.json"
    target.write_text(
        json.dumps({"mailbox_scope": {GOOGLE: {"mode": "all_except", "excluded_folders": []}}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="excluded_folders is empty"):
        load_config(target)


# ---------------------------------------------------------------------------
# PER-PROVIDER SCOPE
#
# Scope is keyed by provider and it is the setting most likely to differ:
# Gmail labels are non-exclusive, Graph folders are exclusive, and the two
# mailboxes do not share a folder tree. The failure these tests prevent is the
# silent one -- configuring one provider must never change what the OTHER
# provider reads, because widening by side effect is a data-exposure change
# nobody consented to.
# ---------------------------------------------------------------------------


def test_each_provider_keeps_its_own_scope_through_the_payload(tmp_path: Path) -> None:
    """The whole point: a wide Outlook answer must not reach the Gmail mailbox."""
    target = tmp_path / "connector-config.json"
    write_config(
        ConnectorConfig(
            scope={
                GOOGLE: MailboxScope(mode="only_listed", included_folders=("Billing",)),
                MICROSOFT: MailboxScope(mode="all_except", excluded_folders=("Client Matters",)),
            }
        ),
        target,
    )
    loaded = load_config(target)
    assert loaded.mailbox_scope(GOOGLE).mode == "only_listed"
    assert loaded.mailbox_scope(MICROSOFT).mode == "all_except"
    assert loaded.mailbox_scope(GOOGLE).covers("Client Matters") is False
    assert loaded.mailbox_scope(MICROSOFT).covers("Client Matters") is False
    assert loaded.mailbox_scope(MICROSOFT).covers("Billing") is True


def test_naming_one_provider_leaves_the_other_at_its_safe_default(tmp_path: Path) -> None:
    """The upgrade hazard. A wide answer for one mailbox must not widen the other."""
    target = tmp_path / "connector-config.json"
    target.write_text(
        json.dumps({"mailbox_scope": {MICROSOFT: {"mode": "all_folders"}}}), encoding="utf-8"
    )
    loaded = load_config(target)
    assert loaded.mailbox_scope(MICROSOFT).mode == "all_folders"
    assert loaded.mailbox_scope(GOOGLE) == DEFAULT_SCOPES[GOOGLE]
    assert loaded.mailbox_scope(GOOGLE).mode == "inbox_only"
    assert loaded.mailbox_scope(GOOGLE).covers("Client Matters") is False
    # The absent provider must fill from the TABLE, so Gmail keeps its nesting
    # answer -- a universal fill would leave the hole open on exactly the
    # mailboxes the nesting fix was written for.
    assert loaded.mailbox_scope(GOOGLE).names_encode_nesting is True


def test_a_config_predating_the_scope_model_stays_narrow_and_per_provider(
    tmp_path: Path,
) -> None:
    """THE INSTALLS ALREADY IN THE FIELD. An old file must not widen or lose nesting."""
    target = tmp_path / "connector-config.json"
    target.write_text(json.dumps({"drafts_enabled": {GOOGLE: False}}), encoding="utf-8")
    loaded = load_config(target)
    for provider in PROVIDER_NAMES:
        assert loaded.mailbox_scope(provider) == DEFAULT_SCOPES[provider]
        assert loaded.mailbox_scope(provider).mode == "inbox_only"
    assert loaded.mailbox_scope(GOOGLE).names_encode_nesting is True
    assert loaded.mailbox_scope(MICROSOFT).names_encode_nesting is False


@pytest.mark.parametrize("provider", PROVIDER_NAMES)
def test_a_missing_config_file_loads_the_narrow_default(tmp_path: Path, provider: str) -> None:
    assert load_config(tmp_path / "absent.json").mailbox_scope(provider).mode == "inbox_only"


def test_the_provider_accessor_fills_absent_keys_and_refuses_unknown_ones() -> None:
    """The fallback lives in the DATA, so no caller has to remember it.

    A typo must be an error rather than a silently ignored key sitting on a
    default scope; an uninstalled provider NAME answers Inbox-only instead.
    """
    scope = MailboxScope(mode="all_folders")
    config = ConnectorConfig(scope={GOOGLE: scope})
    assert config.mailbox_scope(GOOGLE) is scope
    assert set(config.scope) == set(PROVIDER_NAMES)
    assert config.scope[MICROSOFT] == DEFAULT_SCOPES[MICROSOFT]
    assert ConnectorConfig().mailbox_scope("nobody-installed-this") == default_scope()

    with pytest.raises(ValidationError, match="unknown provider"):
        ConnectorConfig(scope={"yahoo": MailboxScope(mode="all_folders")})


def test_an_unknown_provider_key_on_disk_fails_the_load(tmp_path: Path) -> None:
    target = tmp_path / "connector-config.json"
    target.write_text(
        json.dumps({"mailbox_scope": {"yahoo": {"mode": "all_folders"}}}), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="unknown provider"):
        load_config(target)


@pytest.mark.parametrize(
    ("field", "value", "read"),
    [
        pytest.param("MODE", "inbox_only", lambda s: s.mode == "inbox_only", id="env-mode"),
        pytest.param(
            "NAMES_ENCODE_NESTING",
            "false",
            lambda s: s.names_encode_nesting is False,
            id="env-nesting",
        ),
    ],
)
def test_the_environment_can_override_one_providers_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    read: object,
) -> None:
    """Env precedence survives the per-provider key, one nesting level deeper.

    The override must reach ONE provider: the untouched one stays at its own
    default, or the env layer has widened a mailbox nobody named.
    """
    target = tmp_path / "connector-config.json"
    write_config(
        ConnectorConfig(scope={GOOGLE: MailboxScope(mode="all_folders")}),
        target,
    )
    monkeypatch.setenv(f"INBOX_ZERO_MAILBOX_SCOPE__{GOOGLE.upper()}__{field}", value)
    loaded = load_config(target)
    assert read(loaded.mailbox_scope(GOOGLE))  # type: ignore[operator]
    assert loaded.mailbox_scope(MICROSOFT) == DEFAULT_SCOPES[MICROSOFT]
