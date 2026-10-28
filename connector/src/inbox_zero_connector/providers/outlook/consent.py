"""The two interactive Microsoft consent flows, browser first, device code second.

WHY THIS EXISTS
    Entra SECURITY DEFAULTS block the device-code flow outright (AADSTS530035,
    seen live 2026-08-22 on the trial tenant, issue #112), and security
    defaults are ON in every new tenant. The browser flow (authorization code
    + PKCE on a loopback redirect) is what a default-secured tenant accepts,
    so it runs first. Device code stays for a headless install and is reached
    only when the browser flow cannot complete -- and on a default-secured
    tenant it will be refused there too, which the printed fallback line says.

WHY THIS IS ITS OWN MODULE
    ``provider.py`` sits at the 500-code-line ceiling; the consent flows are
    CLI-path-only control flow the provider merely dispatches to, the same
    split ``reauth.py`` made for the 401 retry.

Both flows print to STDOUT, deliberately and only here: they run on the CLI
path, in a terminal a human is looking at. The MCP server never reaches them
(``OutlookProvider._interactive_token`` refuses when ``interactive`` is
False), so they cannot corrupt the protocol stream. T20 is exempted for this
file in pyproject for exactly these calls.

Key Components:
    - browser_token: msal ``acquire_token_interactive``; returns ``{}`` when
      the flow fell through so the caller tries device code.
    - device_code_token: msal device flow; the code is printed, then polled.
    - BROWSER_AUTH_TIMEOUT_S: how long the browser sign-in waits.

Pattern/Convention:
    A flow that fails returns an EMPTY dict, never raises, so the next flow
    gets its turn; the reason is printed because on a default-secured tenant
    the missing ``http://localhost`` redirect (AADSTS500113) is the one thing
    the installer must go fix. Token values are never logged or printed.

Example:
    >>> from inbox_zero_connector.providers.outlook import consent
    >>> consent.BROWSER_AUTH_TIMEOUT_S
    180

See Also:
    - inbox_zero_connector.providers.outlook.provider: the dispatcher.
    - INSTALL-RUNBOOK.md: the app-registration step that adds the redirect.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from inbox_zero_connector.providers.base import AuthError
from inbox_zero_connector.services.auth.scopes import GRAPH_SCOPES

PROVIDER = "microsoft"
BROWSER_AUTH_TIMEOUT_S = 180
"""How long the browser sign-in waits before the device-code fallback runs."""


def browser_token(app: Any) -> dict[str, Any]:
    """Run msal's interactive browser flow; an empty dict means "fell through"."""
    print("Opening your browser to sign in (Microsoft)...", flush=True)
    try:
        result = dict(
            app.acquire_token_interactive(
                list(GRAPH_SCOPES), prompt="select_account", timeout=BROWSER_AUTH_TIMEOUT_S
            )
            or {}
        )
    except Exception as exc:  # ruff: ignore[blind-except] - any failure means "try device code"
        logger.warning(
            f"PROVIDER: browser auth fell through provider={PROVIDER} error={type(exc).__name__}"
        )
        print(f"Browser sign-in did not complete ({type(exc).__name__}); trying a device code.")
        return {}
    if "access_token" not in result:
        detail = result.get("error_description", "no access token returned")
        logger.warning(
            f"PROVIDER: browser auth refused provider={PROVIDER} error={result.get('error', '')}"
        )
        print(f"Browser sign-in refused: {detail}")
        print("Trying a device code (security defaults may refuse it too).")
        return {}
    return result


def device_code_token(app: Any) -> dict[str, Any]:
    """Run msal's device-code flow: print the code, poll until it resolves."""
    flow = app.initiate_device_flow(scopes=list(GRAPH_SCOPES))
    if "user_code" not in flow:
        detail = flow.get("error_description", "")
        raise AuthError(f"Could not start the device flow: {detail}")
    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)
    return dict(result or {})
