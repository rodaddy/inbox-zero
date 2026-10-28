"""Tests for the ``uninstall`` CLI subcommand -- the guided offboarder (#68).

The connector had no offboarding story: an install could be stood up in one
scripted command and could only be torn down from memory. That asymmetry is
not cosmetic on a shared mailbox -- the one-watcher-per-mailbox rule is broken
in practice by an old machine nobody uninstalled, whose supervisor keeps
restarting a watch against a mailbox somebody else is now filing.

WHAT THIS VERB IS, AND WHAT THE TESTS THEREFORE ASSERT. It is a GUIDED
offboarder, not an uninstaller: it does the ONE piece of teardown that is
deterministic and reversible-by-re-consent (the keychain credential), and it
PRINTS everything else with the exact command or URL. So the tests come in two
halves, and the second half matters more than the first:

* the credential delete really happens, for the provider named and no other;
* and NOTHING ELSE happens. No file is removed, no service is booted out, no
  revocation is attempted. Each of those is asserted as an absence, because an
  offboarder that quietly deleted a config directory would still pass every
  test that only checked its printed text.

The printed text is asserted for the three facts an operator cannot reconstruct
themselves: the supervisor unit's exact label, the provider's revocation URL,
and the statement that filed mail STAYS. The last one is the honest half of the
product's posture -- the connector files mail into the owner's own mailbox and
uninstalling it does not un-file anything -- and a silent uninstall would leave
an operator believing the mailbox had been restored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.apps.cli import uninstall


@pytest.fixture
def deletions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which providers ``delete_token`` was called for, deleting nothing.

    The real function reaches the machine-wide OS keychain, which no
    environment variable isolates (``services/auth/keychain`` says so at
    length), so a test that called it would delete the developer's own live
    credential. Recording the argument is the whole assertion anyway: the
    defect worth catching is uninstalling the WRONG provider.
    """
    seen: list[str] = []

    def _fake_delete(provider: str) -> bool:
        seen.append(provider)
        return True

    monkeypatch.setattr(uninstall.auth, "delete_token", _fake_delete)
    return seen


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_the_named_provider_credential_is_deleted(
    provider: str, deletions: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Exactly one delete, for exactly the provider named on the command line."""
    assert cli.main(["uninstall", provider, "--yes"]) == 0
    assert deletions == [provider], "uninstall must delete one credential: the one named"


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_the_printout_names_the_unit_the_revocation_and_what_stays(
    provider: str, deletions: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The three facts an operator cannot reconstruct from the command alone."""
    assert cli.main(["uninstall", provider, "--yes"]) == 0
    out = capsys.readouterr().out

    assert f"com.rtech.inbox-zero-connector.{provider}" in out, "must name the launchd label"
    assert f"inbox-zero-connector-{provider}.service" in out, "must name the systemd unit"
    assert "launchctl bootout" in out
    assert "systemctl --user disable --now" in out

    assert uninstall.REVOCATION_URLS[provider] in out, "must print the revocation URL"
    assert "cannot revoke" in out.lower(), "must be honest that server-side revocation is theirs"

    assert "stay" in out.lower() or "remain" in out.lower(), "must state that filed mail stays"


def test_the_labels_come_from_the_service_unit_module() -> None:
    """The unit names are REUSED, not retyped.

    Two copies of a launchd label drift the moment one is edited, and the
    failure is an operator running ``bootout`` against a label that does not
    exist and reading the error as "already gone".
    """
    from inbox_zero_connector.apps.cli import service_unit

    assert uninstall.LABEL_PREFIX is service_unit._LABEL_PREFIX


def test_nothing_on_disk_is_removed(
    deletions: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config and data files survive. The verb PRINTS the rm; it never runs one.

    This is the product's posture and the repo's law agreeing: an agent does
    not delete a human's files. The check is a real directory with real files
    in it, pointed at by the same environment variables the connector resolves
    its paths from, asserted present after the command returns.
    """
    config = tmp_path / "config"
    data = tmp_path / "data"
    # ``exist_ok`` because the suite's autouse isolation fixture already points
    # both directories somewhere under ``tmp_path`` and creates them.
    config.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    (config / "taxonomy.json").write_text("{}", encoding="utf-8")
    (data / "ledger.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("INBOX_ZERO_CONFIG_DIR", str(config))
    monkeypatch.setenv("INBOX_ZERO_DATA_DIR", str(data))

    assert cli.main(["uninstall", "google", "--yes"]) == 0

    assert (config / "taxonomy.json").is_file(), "uninstall must not delete config files"
    assert (data / "ledger.jsonl").is_file(), "uninstall must not delete data files"
    assert config.is_dir() and data.is_dir()


def test_a_non_interactive_run_without_yes_is_refused(
    deletions: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No TTY and no ``--yes`` means no delete, same contract as ``logout``.

    Uninstalling the wrong provider on a two-mailbox machine costs the owner a
    fresh interactive browser consent, so the consent to do it is stated
    rather than assumed.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["uninstall", "google"]) == 1
    assert deletions == [], "a refused uninstall must not touch the keychain"
