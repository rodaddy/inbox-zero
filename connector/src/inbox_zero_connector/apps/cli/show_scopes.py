"""The ``show-scopes`` subcommand: the scope-manifest renderer.

Lives outside ``main.py`` for the same reason ``desktop.py`` and ``setup.py``
do: that module carries the parser and dispatch table for every verb and sits
against the repo's code-line ceiling, and this verb is a self-contained render
with no shared state.

Its stdout IS its product -- a manifest cannot be delivered through the logger,
which writes to sinks -- and it never runs in the server process, so nothing it
prints can reach the MCP protocol channel.

It is ALSO what the diagnose bundle captures into ``show-scopes.txt``, which is
why it keeps PRINTING rather than returning a string: one renderer, two callers,
and a version that returned text would leave the CLI and the bundle free to
drift into two different manifests.
"""

from __future__ import annotations

from inbox_zero_connector import __version__
from inbox_zero_connector.config import ConnectorConfig, config_path, load_config
from inbox_zero_connector.services.auth import scopes as scopes

PROVIDERS = scopes.PROVIDERS


def _print_posture_prose(config: ConnectorConfig) -> None:
    """Render the closing paragraphs from the COMPUTED layer, per provider.

    Kept separate from ``cmd_show_scopes`` so the manifest loop and the prose
    that summarises it stay under the repo's statement ceiling, and so the one
    rule that matters here is stated in one place: the prose reads
    ``enforcement_for`` -- the same function the header lines print -- rather
    than re-deriving a posture from ``drafts_enabled``. Re-deriving is exactly
    how the paragraph came to announce SCOPE-LAYER under a code-layer header.
    """
    google_drafts = config.drafts_on("google")
    google_layer = scopes.enforcement_for("google", google_drafts)
    on_off = "ON" if google_drafts else "OFF"
    if google_layer == scopes.CODE_LAYER:
        # The layer is COMPUTED, never inferred from the drafts flag: the
        # interactive flow requests gmail.modify with drafts OFF too, and that
        # scope is send-capable, so a drafts-keyed paragraph printed
        # SCOPE-LAYER directly under a code-layer header (#106 follow-up).
        print(f"Gmail drafts are {on_off}. The no-send guarantee is CODE-LAYER:")
        print("the requested scope is send-capable, so the connector never calls a send")
        print("or draft endpoint; the scope manifest above lists what is requested.")
    else:
        print(f"Gmail drafts are {on_off}: the Gmail guarantee is SCOPE-LAYER -- Google's")
        print("own servers refuse a send made with this token.")
    print()
    ms_layer = scopes.enforcement_for("microsoft", config.drafts_on("microsoft"))
    if ms_layer == scopes.SCOPE_LAYER:
        print("Microsoft is SCOPE-LAYER: Mail.ReadWrite covers drafts and is refused by")
        print("both Graph send paths, so Microsoft's own servers refuse a send.")
    else:
        print("Microsoft is CODE-LAYER: a send-capable Graph scope is requested, so the")
        print("guarantee is that the connector never calls a send API.")


def cmd_show_scopes() -> int:
    """Print the LIVE manifest for this deployment, not a generic table.

    The posture depends on drafts_enabled, so a fixed table would be a claim
    rather than a report. Every line here is computed from the installed
    config, which is what makes this command usable as evidence in a security
    questionnaire.
    """
    config = load_config()
    print(f"inbox-zero-connector {__version__} -- scope manifest (Stage 2)\n")
    print(f"config: {config_path()}\n")

    for name in PROVIDERS:
        drafts_on = config.drafts_on(name)
        requested = scopes.scopes_for(name, drafts_on)
        print(f"{name}: drafts={drafts_on}, no-send = {scopes.enforcement_for(name, drafts_on)}")
        for scope in requested:
            print(f"  requested: {scope}")
        excluded = (
            scopes.GMAIL_SEND_WOULD_REQUIRE if name == "google" else scopes.GRAPH_SEND_WOULD_REQUIRE
        )
        # gmail.modify is the interactive flow's one scope, so a send-capable
        # scope can be requested with drafts OFF too — the annotation must
        # track the flag, not assume it (#106).
        reason = "drafts are on" if drafts_on else "interactive baseline scope"
        for scope in excluded:
            if scope in requested:
                print(f"  REQUESTED and send-capable: {scope}   ({reason})")
            else:
                print(f"  NOT requested (send): {scope}")
        print()

    _print_posture_prose(config)
    print()
    print("No send, delete, or forward capability is implemented in this connector.")
    return 0
