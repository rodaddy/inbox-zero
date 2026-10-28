"""Installer next-steps must name the provider the zip is actually built for.

Each shipped zip is pre-configured for ONE mailbox on ONE provider, so a
``provider=google`` build that closes by telling the customer to run
``auth microsoft`` is a wrong instruction handed to the buyer at the exact
moment he is following the script literally. The installers therefore READ
``config/connector-config.json`` and print the matching ``auth`` verb.

Both installers are driven the way a buyer runs them -- as scripts, through
the shell, against a real config file on disk -- rather than by importing a
helper, because the printed text IS the deliverable and a helper that prints
correctly inside a test proves nothing about what the script emits.

``install.sh`` is sourced with ``NEXT_STEPS_ONLY=1``, which makes it emit the
closing block and exit before touching uv, the network, or the mailbox.
Windows PowerShell is not installed on the build host; ``install.ps1`` carries
the same branch and is asserted here by structure so the two cannot silently
diverge.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]  # driving the shipped script IS the contract
from pathlib import Path

import pytest

CONNECTOR_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = CONNECTOR_ROOT / "install.sh"
INSTALL_PS1 = CONNECTOR_ROOT / "install.ps1"

# Resolved off PATH rather than hardcoded. A literal ``/opt/homebrew/bin/bash``
# is only correct on an Apple-silicon Mac with Homebrew: Linux CI has bash at
# ``/usr/bin/bash`` and Windows has none, and both reported the absence as a
# ``FileNotFoundError`` from ``subprocess`` rather than as a skip. On the Mac
# ``which`` still returns the Homebrew bash whenever it leads PATH, so the
# behaviour there is unchanged.
BASH = shutil.which("bash")

NEEDS_BASH = pytest.mark.skipif(
    BASH is None or os.name == "nt",
    reason="install.sh is driven through a POSIX bash; none is available here",
)


def _run_next_steps(tmp_path: Path, provider: str | None) -> str:
    """Run install.sh's next-steps block against a config naming ``provider``."""
    sandbox = tmp_path / "connector"
    sandbox.mkdir()
    shutil.copy(INSTALL_SH, sandbox / "install.sh")
    (sandbox / "install.sh").chmod(0o755)
    config_dir = sandbox / "config"
    config_dir.mkdir()
    payload: dict[str, object] = {"owner_domain": "example.com"}
    if provider is not None:
        payload["provider"] = provider
    (config_dir / "connector-config.json").write_text(json.dumps(payload))

    assert BASH is not None  # guarded by NEEDS_BASH on every caller
    # The inherited PATH with bash's own directory in front, rather than a
    # hardcoded list: the script calls out to `uname`, `sed`, and friends,
    # and their location is not the same on every host either.
    path = os.pathsep.join([str(Path(BASH).parent), os.environ.get("PATH", "")])
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]  # fixed list, repo-owned paths
        [BASH, str(sandbox / "install.sh")],
        capture_output=True,
        text=True,
        cwd=sandbox,
        env={"PATH": path, "NEXT_STEPS_ONLY": "1"},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


@NEEDS_BASH
def test_google_config_prints_only_the_google_auth_verb(tmp_path: Path) -> None:
    out = _run_next_steps(tmp_path, "google")
    assert "auth google" in out
    assert "auth microsoft" not in out


@NEEDS_BASH
def test_microsoft_config_prints_only_the_microsoft_auth_verb(tmp_path: Path) -> None:
    out = _run_next_steps(tmp_path, "microsoft")
    assert "auth microsoft" in out
    assert "auth google" not in out


@NEEDS_BASH
def test_unreadable_provider_falls_back_to_printing_both(tmp_path: Path) -> None:
    out = _run_next_steps(tmp_path, None)
    assert "auth google" in out
    assert "auth microsoft" in out


@pytest.mark.parametrize("needle", ["auth google", "auth microsoft", "provider"])
def test_powershell_installer_carries_the_same_branch(needle: str) -> None:
    """pwsh is absent on the build host; assert the ps1 branch by structure."""
    assert needle in INSTALL_PS1.read_text()


def _run_install_sh(tmp_path: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run install.sh with ``argv`` in a sandbox, before it can reach uv.

    ``PATH`` is trimmed to bash's own directory plus the standard tool dirs so
    the script's own diagnostics are what is under test; anything that gets as
    far as ``uv`` in this sandbox is a bug in the argument handling, not a
    dependency on the host having uv.
    """
    sandbox = tmp_path / "connector"
    sandbox.mkdir()
    shutil.copy(INSTALL_SH, sandbox / "install.sh")
    (sandbox / "install.sh").chmod(0o755)
    assert BASH is not None  # guarded by NEEDS_BASH on every caller
    path = os.pathsep.join([str(Path(BASH).parent), "/usr/bin", "/bin"])
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]  # fixed list, repo-owned paths
        [BASH, str(sandbox / "install.sh"), *argv],
        capture_output=True,
        text=True,
        cwd=sandbox,
        env={"PATH": path, "HOME": str(tmp_path)},
        check=False,
    )


@NEEDS_BASH
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage_and_exits_zero(tmp_path: Path, flag: str) -> None:
    """``--help`` is a buyer's first reflex; it must not look like a failure."""
    completed = _run_install_sh(tmp_path, [flag])
    assert completed.returncode == 0, completed.stderr
    combined = completed.stdout + completed.stderr
    assert "--owner-domain" in combined
    assert "Usage" in combined


@NEEDS_BASH
def test_owner_domain_without_a_value_prints_the_scripts_own_error(tmp_path: Path) -> None:
    """A raw ``${2:?}`` names the positional index ("2"), not the flag.

    The buyer typed a flag name, so the diagnostic has to name that flag.
    """
    completed = _run_install_sh(tmp_path, ["--owner-domain"])
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 2, combined
    assert "--owner-domain needs a value" in combined
    assert "line " not in combined  # no bash parameter-expansion error text


@NEEDS_BASH
def test_bare_install_refuses_rather_than_leaving_an_unnamespaced_install(
    tmp_path: Path,
) -> None:
    """A bare ``./install.sh`` must NOT exit 0 (#171).

    It used to: the config step was conditional on ``--owner-domain``, so the
    default path skipped it, printed a note, and reported success. But
    ``init-config`` is the surface that mints the install id, and the id names
    the config directory, the data directory, and the keychain account -- so
    that "successful" install had an empty id, and its first ``watch`` was
    refused. The 0.2.10 smoke caught it on both tiers: ``paths`` printed
    ``install id:`` with nothing after it.

    ``init-config`` requires ``--owner-domain``, so the installer cannot mint
    one unattended. Refusing is the only remaining honest outcome, and the
    refusal has to name the flag the buyer is missing.
    """
    completed = _run_install_sh(tmp_path, [])
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, combined
    assert completed.returncode == 2, combined
    assert "--owner-domain" in combined
    # It refused BEFORE spending the buyer's time on a dependency install.
    assert "uv sync" not in combined


@NEEDS_BASH
def test_the_refusal_names_the_interactive_wizard_as_the_other_way(
    tmp_path: Path,
) -> None:
    """``setup`` is the other minting surface, so the refusal must not hide it.

    A buyer who does not want to type flags still has a supported path; a
    refusal that only says "pass --owner-domain" reads as the tool having one
    door when it has two.
    """
    completed = _run_install_sh(tmp_path, [])
    combined = completed.stdout + completed.stderr
    assert "inbox-zero-connector setup" in combined


def test_install_sh_always_reaches_the_minting_surface() -> None:
    """No conditional may re-appear around the ``init-config`` call.

    Structural, because the behavioural test above can only prove the refusal
    branch; this one proves the other half -- that a run which passes the
    guard has no remaining way to skip the mint.
    """
    text = INSTALL_SH.read_text()
    assert "run_init uv run inbox-zero-connector init-config" in text
    assert 'if [[ -n "$OWNER_DOMAIN" ]]; then' not in text


def test_powershell_installer_refuses_a_bare_run_too() -> None:
    """pwsh is absent on the build host; assert the ps1 guard by structure.

    The two installers ship the same contract, and a Windows buyer getting the
    exit-0 un-namespaced install the Mac buyer no longer can is the same defect
    with a narrower blast radius.
    """
    text = INSTALL_PS1.read_text()
    assert "-OwnerDomain is REQUIRED" in text
    assert "exit 2" in text
    # The conditional that skipped the mint is gone on this side as well.
    assert "if ($OwnerDomain) {" not in text
    assert 'Run-Init @("init-config", "--owner-domain", $OwnerDomain)' in text


@NEEDS_BASH
def test_next_steps_names_runbook_steps_6_and_6b(tmp_path: Path) -> None:
    """The numbered list a buyer follows literally cannot jump 5 -> 7.

    Step 6 is the scope manifest that IS the security-questionnaire evidence,
    and 6b is the one-mailbox-per-install contract; a second install on the
    same zip silently overwrites the first mailbox's stored token.
    """
    out = _run_next_steps(tmp_path, "microsoft")
    assert "step 6 --" in out
    assert "show-scopes" in out
    assert "step 6b --" in out
    assert "one mailbox" in out.lower()


@NEEDS_BASH
def test_microsoft_next_step_says_browser_sign_in(tmp_path: Path) -> None:
    """Microsoft sign-in is browser (PKCE) first; device code is the fallback.

    Entra security defaults refuse device code outright (AADSTS530035), so
    naming it as THE flow tells a secured-tenant buyer to expect the one
    thing their tenant will reject.
    """
    out = _run_next_steps(tmp_path, "microsoft")
    assert "browser sign-in" in out
    assert "device-code sign-in" not in out


@NEEDS_BASH
def test_fallback_block_says_browser_sign_in_for_both(tmp_path: Path) -> None:
    out = _run_next_steps(tmp_path, None)
    assert "device-code sign-in" not in out
    assert out.count("browser sign-in") == 2


@NEEDS_BASH
def test_next_steps_step_4_points_at_the_consent_runbook_section(tmp_path: Path) -> None:
    """Step 4 is "consent to the rTech app" -- there is nothing to register.

    The kit ships rTech's multi-tenant public client, so telling the buyer to
    register their own app contradicts the shipped config and sends them into
    a portal they do not need.
    """
    out = _run_next_steps(tmp_path, "microsoft")
    assert "consent to the rTech app" in out
    assert "register the app in YOUR tenant" not in out


@NEEDS_BASH
def test_uv_bootstrap_checks_a_minimum_version(tmp_path: Path) -> None:
    """A year-old uv on PATH takes the "already installed" branch silently.

    ``uv.lock`` is lock revision 3, so an older uv fails ``uv sync`` with an
    unactionable lock-format error instead of "upgrade uv".
    """
    text = INSTALL_SH.read_text()
    assert "UV_MIN_VERSION" in text
    assert "uv self update" in text


@pytest.mark.parametrize(
    "needle",
    [
        "step 6 --",
        "step 6b --",
        "browser sign-in",
        "consent to the rTech app",
        "UV_MIN_VERSION",
        "Unblock-File",
        "InvariantCulture",
    ],
)
def test_powershell_installer_carries_the_same_lines(needle: str) -> None:
    """pwsh is absent on the build host; assert the ps1 branch by structure."""
    assert needle in INSTALL_PS1.read_text()


def test_powershell_installer_does_not_say_device_code_sign_in() -> None:
    assert "device-code sign-in" not in INSTALL_PS1.read_text()


def test_powershell_run_init_trusts_the_exit_code_first() -> None:
    """Run-Init must gate the substring on the exact idempotent exit code.

    The init verbs return exit 1 with the "already exists" line. A different
    nonzero whose output merely contains that substring is a real failure and
    must not be swallowed as an idempotent skip.
    """
    text = INSTALL_PS1.read_text()
    assert "$exit -eq 1" in text


def test_powershell_uv_invocations_are_guarded_against_command_not_found() -> None:
    """``uv --version`` on an unresolvable name raises, never sets $LASTEXITCODE.

    ``$ErrorActionPreference`` stays Continue by design, so the exception is a
    non-terminating error and the stale ``$LASTEXITCODE`` from a previous
    native call is what ``Assert-Ok`` would otherwise read.
    """
    text = INSTALL_PS1.read_text()
    # Resolvability is proven with Get-Command AFTER the bootstrap, not by
    # reading an exit code a raising command never set.
    assert text.count("Get-Command uv -ErrorAction SilentlyContinue") >= 2
    assert "$uvExit = $LASTEXITCODE" in text
    assert "$syncExit = $LASTEXITCODE" in text


@pytest.mark.parametrize("script", ["tier-basic.sh", "tier-ai.sh", "tier-basic.ps1", "tier-ai.ps1"])
def test_tier_examples_point_at_the_current_runbook_step_titles(script: str) -> None:
    """ "step 4 (app registration)" is the retired title and the wrong act."""
    text = (CONNECTOR_ROOT / "examples" / script).read_text()
    assert "step 4 (app registration)" not in text
    assert "step 4 (consent to the rTech app)" in text
