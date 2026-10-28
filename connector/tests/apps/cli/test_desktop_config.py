"""Tests for the ``desktop-config`` CLI subcommand.

The verb exists so INSTALL-RUNBOOK step 8 stops being a hand-edited JSON
block: the connector prints its own ``mcpServers`` entry with the absolute
project directory already resolved, and the human (or the installing agent)
merges it. Deterministic output is the whole point, so the tests parse the
JSON out of the captured stdout rather than eyeballing prose.

No mail, no model, no network, no credential — the command reads nothing but
its own file location. Exercised through ``cli.main`` like the other CLI
tests, proving parser row, dispatch, and exit code together.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inbox_zero_connector.apps.cli import main as cli
from inbox_zero_connector.apps.cli.desktop import _DESKTOP_ENV_VARS


def _json_block(out: str) -> dict[str, object]:
    """Extract and parse the one JSON object embedded in the output."""
    return json.loads(out[out.index("{") : out.rindex("}") + 1])  # type: ignore[no-any-return]


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_entry_is_valid_json_with_absolute_directory(
    provider: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The printed entry parses, points at a real absolute dir, and serves."""
    assert cli.main(["desktop-config", provider]) == 0
    entry = _json_block(capsys.readouterr().out)
    servers = entry["mcpServers"]
    assert isinstance(servers, dict)
    server = servers["inbox-zero"]
    assert isinstance(server, dict)
    assert server["command"] == "uv"
    args = server["args"]
    assert isinstance(args, list)
    directory = Path(str(args[args.index("--directory") + 1]))
    assert directory.is_absolute()
    assert (directory / "pyproject.toml").is_file()
    assert args[-1] == "serve"
    env = server["env"]
    assert isinstance(env, dict)
    assert env["INBOX_ZERO_PROVIDER"] == provider


def test_rejects_unknown_provider() -> None:
    """argparse refuses a provider outside the choices row."""
    with pytest.raises(SystemExit):
        cli.main(["desktop-config", "yahoo"])


def test_unset_credential_env_is_omitted_not_placeholdered(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset slot is left OUT of ``env``, and the operator is told to fill it.

    A ``"<paste your ...>"`` string is not a placeholder once it is pasted into
    ``claude_desktop_config.json`` -- it is a live environment value, and the
    settings loader ranks environment above the shipped config file. So the
    template that was meant to be inert was the thing that OVERRODE rTech's
    shipped multi-tenant client id with a literal angle-bracket string, and
    every sign-in failed until a human noticed.
    """
    for name in _DESKTOP_ENV_VARS["microsoft"]:
        monkeypatch.delenv(name, raising=False)

    assert cli.main(["desktop-config", "microsoft"]) == 0
    out = capsys.readouterr().out
    env = _json_block(out)["mcpServers"]["inbox-zero"]["env"]  # type: ignore[index]

    assert "<paste" not in out
    for name in _DESKTOP_ENV_VARS["microsoft"]:
        assert name not in env, f"{name} is unset and must not be emitted"
        assert name in out, f"{name} must still be NAMED so the operator can fill it"


def test_set_credential_env_is_omitted_too_never_echoed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live value is not copied into the block either -- names only, still."""
    monkeypatch.setenv("INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID", "real-id-do-not-print")

    assert cli.main(["desktop-config", "microsoft"]) == 0
    out = capsys.readouterr().out
    env = _json_block(out)["mcpServers"]["inbox-zero"]["env"]  # type: ignore[index]

    assert "real-id-do-not-print" not in out
    assert "INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID" not in env


def test_provider_env_is_still_emitted(capsys: pytest.CaptureFixture[str]) -> None:
    """Omission is scoped to CREDENTIAL slots; the provider switch still ships."""
    assert cli.main(["desktop-config", "google"]) == 0
    env = _json_block(capsys.readouterr().out)["mcpServers"]["inbox-zero"]["env"]  # type: ignore[index]
    assert env == {"INBOX_ZERO_PROVIDER": "google"}
