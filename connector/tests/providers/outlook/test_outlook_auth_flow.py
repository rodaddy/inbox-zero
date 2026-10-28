"""Interactive-first Microsoft auth: browser PKCE flow before device code.

Entra security defaults block the device-code flow on default-secured tenants
(AADSTS530035, observed live 2026-08-22 on the trial tenant, issue #112), so
the CLI path must try ``acquire_token_interactive`` first and fall back to
the device flow only when the browser flow cannot complete.
"""

from __future__ import annotations

from typing import Any

import pytest

from inbox_zero_connector.providers.base import AuthError
from inbox_zero_connector.providers.outlook.provider import OutlookProvider

FROM_BROWSER = "from-browser"
FROM_DEVICE = "from-device"


class _App:
    """Records which MSAL entry points were called, in order."""

    def __init__(self, interactive: Any, device: dict[str, Any] | None = None) -> None:
        self.calls: list[str] = []
        self._interactive = interactive
        self._device = device or {"access_token": FROM_DEVICE}

    def acquire_token_interactive(self, scopes: list[str], **_: Any) -> dict[str, Any]:
        self.calls.append("interactive")
        if isinstance(self._interactive, Exception):
            raise self._interactive
        return dict(self._interactive)

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, Any]:
        self.calls.append("device-initiate")
        return {"user_code": "ABCD", "message": "enter ABCD"}

    def acquire_token_by_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("device-acquire")
        return dict(self._device)


def test_interactive_browser_flow_is_tried_first_and_wins() -> None:
    app = _App(interactive={"access_token": FROM_BROWSER})
    result = OutlookProvider._interactive_token(OutlookProvider(), app, interactive=True)
    assert result["access_token"] == FROM_BROWSER
    assert app.calls == ["interactive"]


def test_device_flow_is_the_fallback_when_browser_flow_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = _App(interactive=RuntimeError("no browser"))
    result = OutlookProvider._interactive_token(OutlookProvider(), app, interactive=True)
    assert result["access_token"] == FROM_DEVICE
    assert app.calls == ["interactive", "device-initiate", "device-acquire"]
    assert "enter ABCD" in capsys.readouterr().out


def test_device_flow_is_the_fallback_when_browser_flow_returns_no_token() -> None:
    app = _App(interactive={"error": "invalid_request", "error_description": "AADSTS500113"})
    result = OutlookProvider._interactive_token(OutlookProvider(), app, interactive=True)
    assert result["access_token"] == FROM_DEVICE
    assert app.calls == ["interactive", "device-initiate", "device-acquire"]


def test_non_interactive_still_refuses_without_touching_msal() -> None:
    app = _App(interactive={"access_token": "x"})
    with pytest.raises(AuthError, match="auth microsoft"):
        OutlookProvider._interactive_token(OutlookProvider(), app, interactive=False)
    assert app.calls == []
