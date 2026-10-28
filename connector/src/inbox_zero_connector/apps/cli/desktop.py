"""The ``desktop-config`` subcommand: print this install's Claude Desktop entry.

Exists so the runbook's step 8 stops being a hand-edited JSON block: the
absolute ``--directory`` is resolved from this file's own location, which is
the one fact a human (or an installing agent) reliably gets wrong.

CREDENTIAL SLOTS ARE OMITTED FROM ``env``, NOT PLACEHOLDERED. Two reasons, and
the second is the one that bit:

* A live value must never be echoed — names only, so the command runs in any
  context (including an AI-driven install) without a redaction question ever
  arising. The operator fills the values in the file they own.
* A ``"<paste your ...>"`` string STOPS BEING A PLACEHOLDER the moment it is
  pasted into ``claude_desktop_config.json``. It is then a live environment
  value, and the settings loader ranks environment ABOVE the shipped config
  file — so the inert-looking template overrode rTech's shipped multi-tenant
  client id with a literal angle-bracket string and every sign-in failed. An
  absent key falls through to the shipped config, which is the correct default
  for most installs; the names are printed as prose beneath the block so an
  operator who DOES need to override still knows what to add.

Lives outside ``main.py`` because that module carries the parser and dispatch
table for every verb and sits against the repo's code-line ceiling; this verb
is a self-contained renderer with no shared state, so it is the natural cut.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

#: Credential env var NAMES per provider, for the config template.
_DESKTOP_ENV_VARS: Final[dict[str, tuple[str, ...]]] = {
    "google": (
        "INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_ID",
        "INBOX_ZERO_CREDENTIALS__GOOGLE_CLIENT_SECRET",
    ),
    "microsoft": (
        "INBOX_ZERO_CREDENTIALS__MICROSOFT_CLIENT_ID",
        "INBOX_ZERO_CREDENTIALS__MICROSOFT_TENANT_ID",
    ),
}


def cmd_desktop_config(provider: str) -> int:
    """Print the ``mcpServers`` entry, or refuse when the project dir is unresolvable.

    The directory resolution assumes the editable install ``uv sync`` makes
    (source under ``<project>/src/``); if the walk does not land on a
    ``pyproject.toml`` the command says so and exits 1, rather than printing a
    path that will not serve.
    """
    project_dir = Path(__file__).resolve().parents[4]
    if not (project_dir / "pyproject.toml").is_file():
        print(
            f"Could not resolve the project directory (looked at {project_dir}). "
            "Run this from the connector checkout via 'uv run inbox-zero-connector "
            "desktop-config' and substitute the --directory path by hand from 'paths'."
        )
        return 1
    entry = {
        "mcpServers": {
            "inbox-zero": {
                "command": "uv",
                "args": ["--directory", str(project_dir), "run", "inbox-zero-connector", "serve"],
                "env": {"INBOX_ZERO_PROVIDER": provider},
            }
        }
    }
    print("Merge this into claude_desktop_config.json (top-level mcpServers key):")
    print()
    print(json.dumps(entry, indent=2))
    print()
    print("The file lives at:")
    print(r"  Windows: %APPDATA%\Claude\claude_desktop_config.json")
    print("  macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json")
    print("Restart Claude Desktop after merging.")
    print()
    print("Most installs need nothing else: the shipped config already carries the")
    print("app registration. ONLY if you are using your OWN registration")
    print("(INSTALL-RUNBOOK.md step 4), add these keys to the 'env' block above and")
    print("paste your real values in — a placeholder string here is a LIVE value and")
    print("will override the shipped one:")
    for name in _DESKTOP_ENV_VARS[provider]:
        print(f"  {name}")
    return 0
