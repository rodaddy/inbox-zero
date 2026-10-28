"""Tests for the ``service-unit`` CLI subcommand.

The install used to end at a foreground ``watch``: close the terminal, log
out, or reboot, and the mailbox stopped being watched with no signal that it
had. This verb prints the supervisor unit for THIS install -- launchd plist or
systemd --user unit -- with every absolute path already resolved, because the
absolutes are the one thing a human hand-writing a unit reliably gets wrong
and a wrong one fails silently at boot rather than at paste time.

Print only, like ``desktop-config``: the command installs nothing, writes no
file, and loads no service. The operator places it.

No mail, no model, no network, no credential -- the command reads its own file
location, the resolved uv binary, and the configured log directory. Exercised
through ``cli.main`` so parser row, dispatch, and exit code are proven
together.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.apps.cli import service_unit


def _plist(out: str) -> dict[str, object]:
    """Parse the printed plist, proving it is real XML launchd would accept."""
    start = out.index("<?xml")
    end = out.index("</plist>") + len("</plist>")
    return plistlib.loads(out[start:end].encode())  # type: ignore[no-any-return]


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_launchd_plist_parses_and_carries_absolutes(
    provider: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plist parses, is labelled per provider, and every path is absolute."""
    assert cli.main(["service-unit", provider, "--platform", "launchd"]) == 0
    parsed = _plist(capsys.readouterr().out)

    assert parsed["Label"] == f"com.rtech.inbox-zero-connector.{provider}"
    assert parsed["RunAtLoad"] is True

    keep_alive = parsed["KeepAlive"]
    assert isinstance(keep_alive, dict)
    assert keep_alive["SuccessfulExit"] is False

    args = parsed["ProgramArguments"]
    assert isinstance(args, list)
    assert Path(str(args[0])).is_absolute(), "uv must be an absolute path, not $PATH"
    assert args[-1] == "watch"
    assert Path(str(parsed["WorkingDirectory"])).is_absolute()
    assert (Path(str(parsed["WorkingDirectory"])) / "pyproject.toml").is_file()

    env = parsed["EnvironmentVariables"]
    assert isinstance(env, dict)
    assert env["INBOX_ZERO_PROVIDER"] == provider

    for key in ("StandardOutPath", "StandardErrorPath"):
        assert Path(str(parsed[key])).is_absolute()
        assert provider in str(parsed[key])


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_systemd_unit_restarts_and_carries_absolutes(
    provider: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The systemd unit restarts on failure and names absolute paths."""
    assert cli.main(["service-unit", provider, "--platform", "systemd"]) == 0
    out = capsys.readouterr().out

    assert "[Unit]" in out
    assert "[Service]" in out
    assert "[Install]" in out
    assert "Restart=on-failure" in out
    assert "RestartSec=" in out
    assert "WantedBy=default.target" in out
    assert f"Environment=INBOX_ZERO_PROVIDER={provider}" in out

    exec_start = next(line for line in out.splitlines() if line.startswith("ExecStart="))
    binary = exec_start.removeprefix("ExecStart=").split()[0]
    assert Path(binary).is_absolute()
    assert exec_start.rstrip().endswith("watch")

    working = next(line for line in out.splitlines() if line.startswith("WorkingDirectory="))
    assert Path(working.removeprefix("WorkingDirectory=")).is_absolute()


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_schtasks_script_restarts_and_carries_absolutes(
    provider: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Windows script registers an at-logon task that restarts on failure.

    Task Scheduler has no ``KeepAlive``, so the restart policy lives on the
    settings object; and a task does not inherit the interactive shell's PATH,
    which is the same absolutes trap the plist and the unit avoid.
    """
    assert cli.main(["service-unit", provider, "--platform", "schtasks"]) == 0
    out = capsys.readouterr().out

    assert "Register-ScheduledTask" in out
    assert "-AtLogOn" in out
    assert "-RestartInterval" in out
    assert "-RestartCount" in out
    assert "-Force" in out, "re-running the installer must replace, not fail"
    assert f"com.rtech.inbox-zero-connector.{provider}" in out
    assert f"'{provider}', 'User'" in out, "the provider env var must be set"

    action = next(line for line in out.splitlines() if "New-ScheduledTaskAction" in line)
    # cmd.exe is the executable (it owns the log redirect); uv is the first
    # quoted token inside the argument string it runs.
    binary = action.split("-Argument '/c ")[1].split('"')[1]
    assert Path(binary).is_absolute(), "uv must be an absolute path, not $PATH"
    assert "inbox-zero-connector watch" in action
    working = action.split("-WorkingDirectory ")[1].strip("'")
    assert Path(working).is_absolute()
    assert (Path(working) / "pyproject.toml").is_file()
    assert f"watch-{provider}.out.log" in action
    assert f"watch-{provider}.err.log" in action


def test_prints_only_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command is a printer: it places no unit file anywhere on disk.

    The unit exists only on stdout. Asserting the whole tree is unchanged would
    be asserting about ``cli.main``'s log sinks, which open on every command;
    the claim under test is narrower and is the one that matters -- no plist,
    no ``.service``, no LaunchAgents entry appears because this ran.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert cli.main(["service-unit", "google", "--platform", "launchd"]) == 0
    assert "<?xml" in capsys.readouterr().out

    written = [p for p in tmp_path.rglob("*") if p.suffix in {".plist", ".service"}]
    assert written == []
    assert not (tmp_path / "Library" / "LaunchAgents").exists()


def test_refuses_when_uv_is_unresolvable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No uv on PATH means refuse with a pointed error, never a broken unit.

    A unit naming a bare ``uv`` looks correct in the terminal and then fails at
    boot, where launchd's PATH is not the shell's. Refusing is the honest
    outcome: the operator learns at paste time, not at the next reboot.
    """
    monkeypatch.setattr(service_unit.shutil, "which", lambda _name: None)

    assert cli.main(["service-unit", "google", "--platform", "launchd"]) == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "uv" in combined
    assert "<?xml" not in combined, "a refusal must not also print a unit"


def test_rejects_unknown_provider() -> None:
    """argparse refuses a provider outside the choices row."""
    with pytest.raises(SystemExit):
        cli.main(["service-unit", "yahoo", "--platform", "launchd"])


def test_rejects_unknown_platform() -> None:
    """argparse refuses a platform outside the choices row."""
    with pytest.raises(SystemExit):
        cli.main(["service-unit", "google", "--platform", "upstart"])
