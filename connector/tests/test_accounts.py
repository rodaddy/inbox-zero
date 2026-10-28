"""Tests for the account list: domain-to-provider inference and the config round trip.

Two behaviours, one test each, and both are about a claim the wizard makes
rather than a convenience:

* **Inference is certain or absent.** ``infer_provider`` answers only for the
  consumer domains that state the provider unambiguously, and returns ``None``
  for a custom domain -- which is what makes the wizard ask instead of opening
  the wrong consent screen.
* **The list survives the write and comes back the same.** Persisting per-account
  posture and drafts (D11) is only worth anything if a load reads them back, and
  the FIRST account staying first is what keeps every existing single-account
  caller pointed at the same mailbox.

No mail, no model, no network, no credential.
"""

from __future__ import annotations

import json

import pytest

from inbox_zero_connector.accounts import AccountSettings, infer_provider
from inbox_zero_connector.config import config_path, default_config, load_settings, write_config


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("someone@gmail.com", "google"),
        ("someone@googlemail.com", "google"),
        ("Someone@GMAIL.COM", "google"),
        ("someone@outlook.com", "microsoft"),
        ("someone@hotmail.com", "microsoft"),
        ("someone@live.com", "microsoft"),
        # A custom domain is served by Google Workspace, Microsoft 365, or
        # neither, and the address cannot tell them apart. None is the honest
        # answer and it is what makes the wizard ask.
        ("partner@lawfirm.com", None),
        ("", None),
    ],
)
def test_provider_is_inferred_only_when_the_domain_states_it(
    address: str, expected: str | None
) -> None:
    """Known consumer domains infer; anything else returns None so the wizard asks."""
    assert infer_provider(address) == expected


def test_accounts_round_trip_through_the_installed_config() -> None:
    """A written account list loads back intact, first account still first.

    Proves the D11 split concretely: the two accounts differ on BOTH posture and
    drafts, so a round trip that collapsed them onto one shared answer would
    fail here rather than at a customer's install.
    """
    accounts = (
        AccountSettings(
            address="owner@lawfirm.com",
            provider="microsoft",
            owner_domain="lawfirm.com",
            posture="file_read_draft",
            drafts=True,
        ),
        AccountSettings(
            address="owner@gmail.com",
            provider="google",
            owner_domain="gmail.com",
            posture="file_only",
            drafts=False,
        ),
    )
    written = write_config(default_config(), accounts=accounts)

    # The key really is on disk, not merely in the object we handed over.
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert [entry["address"] for entry in payload["accounts"]] == [
        "owner@lawfirm.com",
        "owner@gmail.com",
    ]

    # The sanctioned constructor: it is what points the JSON layer at the file
    # just written, and `_config_file` is a ClassVar rather than a field.
    loaded = load_settings(config_path(), configure_logging=False)
    assert len(loaded.accounts) == 2

    first, second = loaded.accounts
    # Order preserved: the first entry is the install's default account.
    assert first.address == "owner@lawfirm.com"
    assert first.provider == "microsoft"
    assert first.posture == "file_read_draft"
    assert first.drafts is True

    # Per account, not shared -- the second differs on both fields (D11).
    assert second.address == "owner@gmail.com"
    assert second.posture == "file_only"
    assert second.drafts is False
