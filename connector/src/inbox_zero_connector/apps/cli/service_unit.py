"""The ``service-unit`` subcommand: print this install's supervisor unit.

The install used to end at a foreground ``watch``. That is fine for the first
five minutes and wrong forever after: closing the terminal, logging out, or
rebooting stops the watch, and nothing says so -- the mailbox simply stops
being filed and the operator finds out days later from the backlog. This verb
closes that gap with the smallest correct thing: it prints a launchd plist or a
systemd ``--user`` unit, or a Windows ``Register-ScheduledTask`` script for
THIS install, restart policy included, and a human places it.

WHY THE ABSOLUTES ARE RESOLVED HERE AND NOT LEFT TO THE OPERATOR. A supervisor
does not run under the operator's login shell, so ``uv`` off ``PATH`` and a
relative working directory both resolve differently (or not at all) at boot
than they did in the terminal where the unit was written. That failure is
silent and delayed: the unit loads, the process exits, and the only evidence is
in a log nobody is reading yet. So every path in the printed unit is resolved
at print time from the same functions the connector itself uses, and if one
cannot be resolved the command REFUSES rather than printing a unit that will
fail at the next reboot.

PRINT ONLY, like ``desktop-config``. The command installs nothing, writes no
file, and loads no service. Placing a unit is a decision about the operator's
own machine -- which user, which login session, whether a supervisor is wanted
at all -- and the product's posture is that the human makes it. INSTALL-RUNBOOK
carries the exact ``launchctl`` / ``systemctl`` lines for the placement.
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import sys
from pathlib import Path
from typing import Final

from inbox_zero_connector.config import load_settings

#: launchd reverse-DNS label prefix; the provider is appended so a machine
#: watching two mailboxes gets two distinct, separately loadable services.
_LABEL_PREFIX: Final = "com.rtech.inbox-zero-connector"

#: Seconds systemd waits before restarting a failed watch. Long enough that a
#: provider outage does not become a restart storm against their API, short
#: enough that a transient failure self-heals before the next mail arrives.
_RESTART_SEC: Final = 30


def _project_dir() -> Path | None:
    """The install root, or ``None`` when the editable-install layout does not hold."""
    project_dir = Path(__file__).resolve().parents[4]
    return project_dir if (project_dir / "pyproject.toml").is_file() else None


def _log_dir() -> Path:
    """The connector's own log directory, from config -- not a guessed path."""
    return Path(load_settings().logging.directory)


def _launchd(provider: str, uv: Path, project_dir: Path, log_dir: Path) -> str:
    """Render the launchd plist.

    ``KeepAlive.SuccessfulExit = false`` is the restart policy that matches the
    watch's contract: a clean exit is deliberate (an operator stopped it) and
    is left alone, while a crash or a killed process is restarted. A bare
    ``KeepAlive = true`` would fight the operator's own ``launchctl bootout``.
    """
    unit: dict[str, object] = {
        "Label": f"{_LABEL_PREFIX}.{provider}",
        "ProgramArguments": [str(uv), "run", "inbox-zero-connector", "watch"],
        "WorkingDirectory": str(project_dir),
        "EnvironmentVariables": {"INBOX_ZERO_PROVIDER": provider},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(log_dir / f"watch-{provider}.out.log"),
        "StandardErrorPath": str(log_dir / f"watch-{provider}.err.log"),
    }
    return plistlib.dumps(unit).decode().rstrip()


def _systemd(provider: str, uv: Path, project_dir: Path) -> str:
    """Render the systemd ``--user`` unit.

    Output goes to the journal rather than the connector's log directory:
    systemd already captures stdout/stderr per unit, and pointing the unit at
    files as well would split one stream across two places for no gain.
    ``journalctl --user -u`` is the read path, named in the runbook.
    """
    return "\n".join((
        "[Unit]",
        f"Description=Inbox Zero connector watch ({provider})",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={project_dir}",
        f"Environment=INBOX_ZERO_PROVIDER={provider}",
        f"ExecStart={uv} run inbox-zero-connector watch",
        "Restart=on-failure",
        f"RestartSec={_RESTART_SEC}",
        "",
        "[Install]",
        "WantedBy=default.target",
    ))


def _schtasks(provider: str, uv: Path, project_dir: Path, log_dir: Path) -> str:
    """Render the PowerShell ``Register-ScheduledTask`` script.

    Task Scheduler has no ``KeepAlive``/``Restart=on-failure``; the equivalent
    is a restart policy on the task SETTINGS object, so the script sets
    ``RestartInterval``/``RestartCount`` to the same 30-second spacing the
    systemd unit uses. ``-AtLogOn`` matches launchd's ``RunAtLoad`` for a user
    service: the watch belongs to the mailbox owner's session, not to the
    machine, so it must not run as SYSTEM with no keychain access.

    The task name carries ``_LABEL_PREFIX`` and the provider for the same
    reason the plist Label does -- two mailboxes on one machine are two
    separately registerable tasks. ``-Force`` is what makes the installer's
    registration idempotent: re-running replaces the task rather than failing
    on a duplicate name.

    Stdout and stderr are redirected to the connector's own log directory,
    because a scheduled task's console output otherwise goes nowhere at all.
    """
    task = f"{_LABEL_PREFIX}.{provider}"
    out_log = log_dir / f"watch-{provider}.out.log"
    err_log = log_dir / f"watch-{provider}.err.log"
    # cmd.exe is the redirect: Task Scheduler runs one executable with an
    # argument string and has no shell of its own to redirect through.
    command = f'"{uv}" run inbox-zero-connector watch >> "{out_log}" 2>> "{err_log}"'
    return "\n".join((
        f'$action = New-ScheduledTaskAction -Execute "$env:ComSpec" '
        f"-Argument '/c {command}' -WorkingDirectory '{project_dir}'",
        "$trigger = New-ScheduledTaskTrigger -AtLogOn",
        "$settings = New-ScheduledTaskSettingsSet "
        "-RestartInterval (New-TimeSpan -Seconds "
        f"{_RESTART_SEC}) -RestartCount 999 -StartWhenAvailable "
        "-DontStopIfGoingOnBatteries -AllowStartIfOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Seconds 0)",
        "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        "-LogonType Interactive -RunLevel Limited",
        f"Register-ScheduledTask -TaskName '{task}' -Action $action "
        "-Trigger $trigger -Settings $settings -Principal $principal "
        f"-Description 'Inbox Zero connector watch ({provider})' -Force",
        f"[Environment]::SetEnvironmentVariable('INBOX_ZERO_PROVIDER', '{provider}', 'User')",
    ))


def _emit(destination: str, unit: str, load_header: str, load_lines: list[str]) -> None:
    """Print one unit in the shape all three platforms share.

    Every platform's output is the same three beats -- where to save it, the
    unit itself, and the command that loads it -- so the shape lives here once
    rather than three times in the dispatch, which is what pushed
    ``cmd_service_unit`` over the statement ceiling.

    Args:
        destination: Where the operator saves the printed unit.
        unit: The rendered unit text.
        load_header: The line introducing the load commands.
        load_lines: The commands that load or confirm the service.
    """
    print(f"Save this as {destination}:")
    print()
    print(unit)
    print()
    print(load_header)
    for line in load_lines:
        print(line)


def cmd_service_unit(provider: str, platform: str) -> int:
    """Print the unit for ``provider`` on ``platform``, or refuse and exit 1."""
    uv_path = shutil.which("uv")
    if uv_path is None:
        print(
            "Could not resolve an absolute path to 'uv' (not on PATH). A unit naming "
            "a bare 'uv' loads and then fails at boot, because a supervisor does not "
            "run under your login shell. Install uv, or run this command from a shell "
            "where 'which uv' answers, then re-run.",
            file=sys.stderr,
        )
        return 1

    project_dir = _project_dir()
    if project_dir is None:
        print(
            "Could not resolve the project directory (no pyproject.toml above this "
            "file). Run this from the connector checkout via 'uv run "
            "inbox-zero-connector service-unit', and take the directory from 'paths' "
            "if it still cannot resolve.",
            file=sys.stderr,
        )
        return 1

    uv = Path(uv_path)
    label = f"{_LABEL_PREFIX}.{provider}"
    if platform == "launchd":
        _emit(
            f"~/Library/LaunchAgents/{label}.plist",
            _launchd(provider, uv, project_dir, _log_dir()),
            "Then load it:",
            [f"  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist"],
        )
    elif platform == "schtasks":
        _emit(
            f"register-{provider}.ps1 and run it in PowerShell",
            _schtasks(provider, uv, project_dir, _log_dir()),
            "Then confirm it registered:",
            [f"  Get-ScheduledTask -TaskName '{label}'"],
        )
    else:
        unit_name = f"inbox-zero-connector-{provider}.service"
        _emit(
            f"~/.config/systemd/user/{unit_name}",
            _systemd(provider, uv, project_dir),
            "Then load it:",
            [
                "  systemctl --user daemon-reload",
                f"  systemctl --user enable --now {unit_name}",
            ],
        )
    return 0


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``service-unit`` verb on the CLI's subparser action.

    The row lives here rather than inline in ``build_parser`` for the same
    reason ``list_folders.add_parser`` does: ``main.py`` sits against the
    repo's file and function code-line ceilings, and a verb that owns its own
    module can own its own row.

    Args:
        sub: The subparser action every verb is registered on.
    """
    parser = sub.add_parser(
        "service-unit",
        help="Print the launchd/systemd/Task Scheduler unit that keeps 'watch' running.",
        description=(
            "Print a supervisor unit for this install so the watch survives logout "
            "and reboot. Prints only -- it installs nothing and writes no file."
        ),
    )
    parser.add_argument("provider", choices=["google", "microsoft"])
    parser.add_argument(
        "--platform",
        choices=["launchd", "systemd", "schtasks"],
        required=True,
        help="launchd (macOS), systemd --user (Linux), or schtasks (Windows).",
    )
