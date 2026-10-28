"""Platform-native desktop notification, and nothing else (#64, decision D9).

WHY NATIVE AND NOT A LOG LINE. The failure this module exists for is a watcher
that stops working on an unattended machine. A log line is only read by
somebody who already suspects something is wrong, which is the state the
notification is supposed to CREATE. The digest cannot carry it either: a
watcher that has stopped is a watcher that is not producing digests.

NO DEPENDENCY, ON EITHER PLATFORM. macOS gets ``osascript``, which ships with
the OS. Windows gets the WinRT toast API through ``powershell``, which also
ships with the OS -- deliberately NOT BurntToast, because a notification path
that requires the operator to install a PowerShell module first is a path that
silently does nothing on the machines that need it most. Linux logs and returns:
this kit's installers target macOS and Windows, and guessing between
``notify-send``, ``kdialog``, and a headless box is worse than saying so.

NEVER ``shell=True``. The title and body are interpolated into a script in both
branches, so the shell would be a second interpreter over text this module does
not fully control. ``subprocess.run`` with an argument LIST hands the script to
the interpreter as one argv element with no word splitting, no globbing, and no
metacharacter interpretation by anything between here and it. The quote
stripping below closes the remaining hole -- the script languages' own string
literals.

FAILURE IS NEVER FATAL. A notifier that raises would kill the watcher it exists
to report on, which is the exact inversion of its purpose. Every failure is
logged and swallowed, and the function returns whether it believes it delivered
so a caller can record that without having to catch anything.
"""

from __future__ import annotations

import platform
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- the notifier IS a subprocess call
from typing import Final

from loguru import logger

NOTIFY_TIMEOUT_SECONDS: Final = 10.0
"""Ceiling on one notification. A hung ``powershell`` must not stall a round."""

_POWERSHELL_TEMPLATE: Final = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
    " ContentType=WindowsRuntime] > $null;"
    "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(0);"
    "$t = $x.GetElementsByTagName('text');"
    "$t.Item(0).AppendChild($x.CreateTextNode('{title}')) > $null;"
    "$t.Item(1).AppendChild($x.CreateTextNode('{body}')) > $null;"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'inbox-zero-connector').Show("
    "[Windows.UI.Notifications.ToastNotification]::new($x))"
)
"""Template 0 is the two-line text toast, which is the shape this kit sends."""


def _sanitize(text: str) -> str:
    """Strip the characters that would end a string literal in either script.

    Both branches embed the text inside a quoted literal -- AppleScript's double
    quotes, PowerShell's single quotes -- so a quote in a title is the one
    character that changes the script's structure rather than its content.
    Removed rather than escaped, because the two languages escape differently
    and a notification title is not worth a per-language escaper. Backslashes go
    with them: AppleScript treats one as an escape introducer.
    """
    return "".join(char for char in text if char not in "\"'\\" and char >= " ")


def _argv(system: str, title: str, body: str) -> list[str] | None:
    """Return the command for ``system``, or ``None`` when there is no notifier."""
    safe_title, safe_body = _sanitize(title), _sanitize(body)
    if system == "Darwin":
        script = f'display notification "{safe_body}" with title "{safe_title}"'
        return ["osascript", "-e", script]
    if system == "Windows":
        script = _POWERSHELL_TEMPLATE.format(title=safe_title, body=safe_body)
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    return None


def notify(title: str, body: str) -> bool:
    """Show one desktop notification. Returns whether it was delivered.

    Args:
        title: The notification's heading. Quotes and control characters are
            stripped rather than escaped; see ``_sanitize``.
        body: The notification's one-line message, sanitised the same way.

    Returns:
        True when the platform notifier ran and exited 0. False on Linux, on a
        missing notifier binary, on a non-zero exit, and on a timeout -- every
        one of which is logged. This function raises nothing by design.
    """
    system = platform.system()
    argv = _argv(system, title, body)
    if argv is None:
        logger.info(f"NOTIFY: no native notifier on this platform system={system} title={title}")
        return False
    try:
        completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- list argv, no shell
            argv, capture_output=True, timeout=NOTIFY_TIMEOUT_SECONDS, check=False
        )
    except OSError, subprocess.SubprocessError:
        logger.opt(exception=True).error(f"NOTIFY: notifier failed system={system}")
        return False
    if completed.returncode != 0:
        logger.error(f"NOTIFY: notifier exited nonzero system={system} code={completed.returncode}")
        return False
    return True
