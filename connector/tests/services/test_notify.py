"""``notify`` builds the right argv per platform, and never reaches a shell.

``subprocess.run`` is patched throughout: the assertion is about the COMMAND
this module constructs, and a test that actually popped a desktop notification
would be asserting the OS works. The argv shape is the security property worth
pinning -- a list, never a string, and never ``shell=True`` -- because the title
and body are interpolated into a script in both branches.
"""

from __future__ import annotations

import subprocess  # ruff: ignore[suspicious-subprocess-import] -- patched CompletedProcess type
from typing import Any

import pytest

from inbox_zero_connector.services import notify as notify_module


class _Recorder:
    """Stand in for ``subprocess.run``, keeping the call for assertion."""

    def __init__(self, returncode: int = 0) -> None:
        self.argv: list[str] | None = None
        self.kwargs: dict[str, Any] = {}
        self.returncode = returncode

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.argv = argv
        self.kwargs = kwargs
        return subprocess.CompletedProcess(argv, self.returncode)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Patch the module's subprocess boundary and hand back the recorder."""
    recorded = _Recorder()
    monkeypatch.setattr(notify_module.subprocess, "run", recorded)
    return recorded


def test_macos_calls_osascript_with_a_display_notification_script(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """Darwin gets the OS-shipped ``osascript``, one argv element per token."""
    monkeypatch.setattr(notify_module.platform, "system", lambda: "Darwin")

    assert notify_module.notify("Inbox Zero", "watcher failing") is True
    assert recorder.argv is not None
    assert recorder.argv[:2] == ["osascript", "-e"]
    assert recorder.argv[2] == 'display notification "watcher failing" with title "Inbox Zero"'
    assert "shell" not in recorder.kwargs


def test_windows_calls_powershell_with_the_winrt_toast_api(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """Windows gets the built-in toast manager -- no BurntToast module."""
    monkeypatch.setattr(notify_module.platform, "system", lambda: "Windows")

    assert notify_module.notify("Inbox Zero", "re-auth needed") is True
    assert recorder.argv is not None
    assert recorder.argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    script = recorder.argv[4]
    assert "[Windows.UI.Notifications.ToastNotificationManager]" in script
    assert "'Inbox Zero'" in script
    assert "'re-auth needed'" in script
    assert "shell" not in recorder.kwargs


def test_linux_runs_nothing_and_reports_undelivered(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """No native notifier is claimed on Linux, and no subprocess is spawned."""
    monkeypatch.setattr(notify_module.platform, "system", lambda: "Linux")

    assert notify_module.notify("Inbox Zero", "watcher failing") is False
    assert recorder.argv is None


def test_quotes_are_stripped_so_the_script_literal_cannot_be_closed(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """A quote in the body is content, and must not become script structure."""
    monkeypatch.setattr(notify_module.platform, "system", lambda: "Darwin")

    notify_module.notify("Inbox Zero", 'say "boo" \\ then')

    assert recorder.argv is not None
    assert recorder.argv[2] == 'display notification "say boo  then" with title "Inbox Zero"'


def test_a_nonzero_exit_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """A notifier that fails must never take the watcher down with it."""
    monkeypatch.setattr(notify_module.platform, "system", lambda: "Darwin")
    recorder.returncode = 1

    assert notify_module.notify("Inbox Zero", "watcher failing") is False
